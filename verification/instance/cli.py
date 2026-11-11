"""verify-mxz's second driver: the real integration on a throwaway Home Assistant.

    verification/instance/mxz-instance env       # once per lane: the instance's own Python environment
    verification/instance/mxz-instance up        # fresh instance, started, onboarded, ready
    verification/instance/mxz-instance doctor    # read-only: isolated, ours, current, clean log
    verification/instance/mxz-instance drive setup-and-enable
    verification/instance/mxz-instance exec -- <command>   # e.g. a browser, inside the namespace
    verification/instance/mxz-instance down      # stop the process this instance started
    verification/instance/mxz-instance reset     # remove the instance and its namespace

Instances live under $MXZ_INSTANCE_HOME (default ~/mxz-throwaway-ha); evidence
under $MXZ_VERIFY_OUT/instance (default ~/verify-mxz-evidence/<UTC stamp>).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from home import (
    EQUIPMENT,
    PRODUCT,
    REPO,
    Instance,
    Refused,
    digest,
    home,
    revision,
    ui_requirements,
)

LANES = ("py312-ha2024.12.5", "py313-ha2026.2.3", "py314-ha2026.9.4")
JOURNEYS = {"setup-and-enable": "journeys.setup_and_enable", "room-targets": "journeys.room_targets"}
# Run only by name, never by a bare `drive`: they film, and take half an hour.
DEMOS = {"demo-stock": "journeys.demo_stock", "demo-coordinated": "journeys.demo_coordinated"}
TEST_NET = ("192.0.2.1", 80)


def build_env(lane: str) -> int:
    """The instance's Python: the lane's lock, plus what its Home Assistant's frontend needs.

    Built outside the namespace with the lane's interpreter; no lane is changed.
    """
    lock = REPO / "requirements" / f"constraints-{lane}.txt"
    target = home() / "env" / lane
    pip = [str(target / "bin" / "python"), "-m", "pip", "install", "--index-url", "https://pypi.org/simple",
           "-c", str(lock)]
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("PIP_", "PYTHON"))}
    clean |= {"PIP_CONFIG_FILE": "/dev/null", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    subprocess.run([sys.executable, "-m", "venv", str(target)], check=True)
    subprocess.run([*pip, "-r", str(lock)], check=True, env=clean)
    where = "import pathlib, homeassistant.components as c; print(pathlib.Path(c.__file__).parent)"
    components = Path(subprocess.run(
        [str(target / "bin" / "python"), "-I", "-c", where], check=True, capture_output=True, text=True
    ).stdout.strip())
    frontend = ui_requirements(components)
    subprocess.run([*pip, *frontend], check=True, env=clean)
    subprocess.run([str(target / "bin" / "python"), "-m", "pip", "check"], check=True, env=clean)
    print(f"ENV_READY {target} ({lane}, {' '.join(frontend)})")
    return 0


async def ready(instance: Instance, timeout: float = 180) -> dict[str, Any]:
    """Usable, not merely listening: onboarded, equipment states present, recorder running."""
    import aiohttp
    from client import Client, onboard

    deadline = time.monotonic() + timeout
    async with aiohttp.ClientSession() as http:
        while True:
            try:
                async with http.get(f"{instance.url}/api/onboarding") as response:
                    if response.status == 200:
                        break
            except aiohttp.ClientError:
                pass
            if time.monotonic() > deadline or instance.pid() is None:
                raise Refused(f"Home Assistant did not answer; see {instance.dir / 'hass.out'} and {instance.log}")
            await asyncio.sleep(1)
    await onboard(instance.url, instance.auth_file)
    async with Client(instance.url, instance.auth_file) as client:
        if (http := await http_config(client)) and http["active_config_type"] == "pending":
            await client.ws("http/config/promote")
        while True:
            states = await client.states()
            equipment = [eid for eid in states if eid.split(".", 1)[1].endswith(("_head", "_temperature", "_window"))]
            recorder = await client.ws("recorder/info")
            config = await client.ws("get_config")
            if config.get("recovery_mode") or config.get("safe_mode"):
                raise Refused(f"Home Assistant started in recovery mode; see {instance.log}")
            if len(equipment) >= 9 and recorder.get("recording") and config["state"] == "RUNNING":
                return {"equipment": sorted(equipment), "recorder": recorder}
            if time.monotonic() > deadline:
                raise Refused(f"not ready: equipment {equipment}, recorder {recorder}")
            await asyncio.sleep(1)


async def http_config(client: Any) -> dict[str, Any] | None:
    """The HTTP config store, or None on a Home Assistant older than its trial.

    From HA 2026.9 the YAML http config is a trial: unconfirmed, Home Assistant
    reverts it after five minutes and restarts, and with no supervisor a restart
    is an exit. ``up`` confirms it, as a person does once it works.
    """
    from client import ApiError

    try:
        return await client.ws("http/config")
    except ApiError as error:
        if "unknown_command" in str(error):
            return None
        raise


def up(instance: Instance) -> int:
    instance.hold()
    if instance.dir.joinpath("instance.json").exists() and instance.pid() is not None:
        raise Refused(f"{instance.name} is already up; run down or reset first")
    instance.build(revision())
    instance.start()
    state = asyncio.run(ready(instance))
    print(f"READY {instance.name} {instance.url} pid={instance.pid()} revision={instance.record().revision}")
    print(f"EQUIPMENT {' '.join(state['equipment'])}")
    return 0


def isolation_problems() -> list[str]:
    problems = []
    links = subprocess.run(["ip", "-o", "link"], check=True, capture_output=True, text=True).stdout
    names = [line.split(":")[1].strip() for line in links.splitlines()]
    if names != ["lo"]:
        problems.append(f"namespace has interfaces {names}, not only lo")
    try:
        socket.create_connection(TEST_NET, timeout=3).close()
        problems.append(f"a TCP connection to {TEST_NET[0]} succeeded")
    except OSError:
        pass
    return problems


def doctor(instance: Instance) -> int:
    from expect import log_errors
    from homeassistant.const import __version__ as ha_version
    from journeys.drive import LOG_NAMES

    record = instance.record()
    current = revision()
    print(f"python        {platform.python_version()} ({sys.executable})")
    print(f"homeassistant {ha_version}")
    print(f"checkout      {current}")
    print(f"instance      {instance.name} built from {record.revision}, {instance.url}")
    print(f"isolation     network namespace {instance.namespace} (loopback only)")
    problems = isolation_problems()
    pid = instance.pid()
    if pid is None:
        problems.append(f"the process {instance.name} started is not running")
    if record.revision != current or record.code != digest(PRODUCT, EQUIPMENT):
        problems.append(f"stale: built from {record.revision}, checkout is {current} with different code")
    if pid is not None:
        try:
            if revert_at := asyncio.run(_answers(instance)):
                problems.append(f"Home Assistant will revert its HTTP config and exit at {revert_at}")
        except Exception as error:  # noqa: BLE001 - any failure to answer is the finding
            problems.append(f"the API does not answer: {error}")
    errors = log_errors(instance.log.read_text(errors="replace"), LOG_NAMES) if instance.log.exists() else []
    problems += [f"log error: {line.splitlines()[0]}" for line in errors]
    for problem in problems:
        print(f"DOCTOR_PROBLEM {problem}")
    print("DOCTOR_FAILED" if problems else "DOCTOR_OK")
    return 1 if problems else 0


async def _answers(instance: Instance) -> str | None:
    from client import Client

    async with Client(instance.url, instance.auth_file) as client:
        await client.rest("GET", "/api/")
        return (await http_config(client) or {}).get("revert_at")


def drive(instance: Instance, features: list[str]) -> int:
    instance.hold()
    if doctor(instance):
        raise Refused("the doctor failed; fix or reset before driving")
    from evidence import write_run

    outcomes = {feature: asyncio.run(_drive_one(instance, feature)) for feature in features}
    status = 0 if all(outcome == "passed" for outcome in outcomes.values()) else 1
    path = write_run({
        "revision": instance.record().revision,
        "instance": instance.name,
        "isolation": f"network namespace {instance.namespace}, loopback only",
        "python": platform.python_version(),
        "homeassistant": _ha_version(),
        "features": outcomes,
        "exit": status,
    })
    print(f"EVIDENCE {path.parent}")
    for feature, outcome in outcomes.items():
        print(f"{outcome.upper()} {feature}")
    return status


def _ha_version() -> str:
    from homeassistant.const import __version__

    return __version__


async def _drive_one(instance: Instance, feature: str) -> str:
    import importlib

    from client import Client
    from evidence import Transcript, log_slice
    from expect import Stream
    from invented_house.devices import load_house
    from journeys.drive import Drive

    journey = importlib.import_module({**JOURNEYS, **DEMOS}[feature])
    house = load_house()
    transcript = Transcript(feature, journey.JOURNEY, {
        "revision": instance.record().revision, "homeassistant": _ha_version(), "instance": instance.name,
    })
    offset = instance.log.stat().st_size
    started = datetime.now(UTC)
    stream = Stream()
    outcome, error = "failed", None
    async with Client(instance.url, instance.auth_file) as client:
        await client.subscribe("state_changed", stream.feed)
        await client.subscribe("call_service", stream.feed)
        stream.states.update(await client.states())
        run = Drive(client, stream, transcript, house)
        try:
            await journey.run(run)
            log = log_slice(instance.log, offset)
            run.every_journey_checks(log)
            outcome = "passed"
        except Exception as failure:  # noqa: BLE001 - a crash is a failed journey, kept with its evidence
            error = f"{type(failure).__name__}: {failure}"
        finally:
            ended = datetime.now(UTC)
            entities = [*run.heads, *run.sensors, *(f"binary_sensor.{k}_window" for k in run.rooms), "sensor.outdoor_unit"]
            if run.entry_id:
                entities += list((await run.find_entities()).values())
                transcript.attach("diagnostics.json", await client.diagnostics(run.entry_id))
            kept = set(entities)
            transcript.attach("events.jsonl", "".join(
                json.dumps(event, sort_keys=True) + "\n" for event in stream.events
                if event["event_type"] == "call_service" or event["data"].get("entity_id") in kept
            ))
            transcript.attach("history.json", await client.history(started.isoformat(), ended.isoformat(), entities))
            transcript.attach("home-assistant.log", log_slice(instance.log, offset))
            transcript.save(outcome, error)
    return outcome


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mxz-instance", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="demo", help="instance name (default demo)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("env").add_argument("--lane", choices=LANES, default="py314-ha2026.9.4")
    commands.add_parser("up")
    commands.add_parser("doctor")
    commands.add_parser("drive").add_argument(
        "features", nargs="*", help=f"default: all of {', '.join(JOURNEYS)}; by name only: {', '.join(DEMOS)}")
    commands.add_parser("exec").add_argument("argv", nargs=argparse.REMAINDER)
    commands.add_parser("down")
    commands.add_parser("reset")
    args = parser.parse_args(argv)
    if args.command == "env":
        return build_env(args.lane)
    instance = Instance(args.name)
    if args.command == "reset":
        instance.hold()
        instance.reset()
        print(f"RESET {instance.name}: {instance.dir} and namespace {instance.namespace} removed; evidence kept")
        return 0
    if args.command == "down":
        instance.hold()
        status = instance.stop()
        print(f"DOWN {instance.name} exit={'unknown (not started by this command)' if status is None else status}")
        return 0
    if args.command == "up" and not instance.inside_namespace():
        instance.make_namespace()
    instance.enter_namespace()
    if args.command == "exec":
        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        os.execvp(command[0], command)
    if args.command == "drive":
        if unknown := [feature for feature in args.features if feature not in {**JOURNEYS, **DEMOS}]:
            raise Refused(f"no journey for {unknown}; known: {', '.join([*JOURNEYS, *DEMOS])}")
        return drive(instance, args.features or list(JOURNEYS))
    return up(instance) if args.command == "up" else doctor(instance)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Refused as refused:
        print(f"REFUSED {refused}", file=sys.stderr)
        sys.exit(2)
