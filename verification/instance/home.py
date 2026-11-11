"""One throwaway Home Assistant: its directory, its namespace, its process.

Knows nothing about MXZ's entities or any journey. The instance runs in a
network namespace that has only loopback, so nothing in it can reach the LAN
or the internet, and every command here talks only to the process it started.
Stdlib only.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
EQUIPMENT = HERE / "invented_house"
PRODUCT = REPO / "custom_components" / "mxz_coordinator"
PORT = 18123

# Exactly what the proof needs on top of what bootstrap always loads. Never
# default_config: it brings discovery (zeroconf, ssdp, dhcp, usb, bluetooth),
# cloud and mobile_app.
INTEGRATIONS = (
    "http", "api", "websocket_api", "auth", "onboarding", "config", "recorder",
    "history", "diagnostics", "system_log", "logger", "persistent_notification",
)
# What a browser needs besides the frontend wheel. The frontend lists every
# service when it loads, and listing them imports these components
# (homeassistant.helpers.service._base_components); with one missing
# requirement the list fails and the page never leaves "Loading...".
UI_COMPONENTS = ("frontend", "ai_task", "assist_satellite", "camera")


def ui_requirements(components: Path, roots: tuple[str, ...] = UI_COMPONENTS) -> list[str]:
    """The requirements of ``roots`` and of every component they depend on, in order."""
    found: list[str] = []
    seen: set[str] = set()

    def walk(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        manifest = json.loads((components / name / "manifest.json").read_text())
        found.extend(r for r in manifest.get("requirements", []) if r not in found)
        for dependency in manifest.get("dependencies", []):
            walk(dependency)

    for root in roots:
        walk(root)
    return found


def home() -> Path:
    return Path(os.environ.get("MXZ_INSTANCE_HOME") or Path.home() / "mxz-throwaway-ha")


def render_configuration(site: dict[str, Any], port: int = PORT) -> str:
    """configuration.yaml for an instance: invented site, loopback HTTP, no discovery."""
    lines = ["homeassistant:"]
    lines += [f"  {key}: {json.dumps(value)}" for key, value in site.items()]
    lines += [
        "http:",
        "  server_host: 127.0.0.1",
        f"  server_port: {port}",
        "logger:",
        "  default: warning",
        "  logs:",
        "    custom_components.mxz_coordinator: info",
        "    custom_components.invented_house: info",
        "    homeassistant.components.recorder: info",
        "invented_house:",
    ]
    lines += [f"{name}:" for name in INTEGRATIONS if name not in ("http", "logger")]
    return "\n".join(lines) + "\n"


def revision() -> str:
    rev = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return f"{rev}-dirty" if dirty else rev


def digest(*roots: Path) -> str:
    """One hash over the files an instance runs, so an edit after up shows as stale."""
    total = hashlib.sha256()
    for root in roots:
        for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            total.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return total.hexdigest()


class Refused(Exception):
    """The instance will not do this, and says why."""


@dataclass
class Record:
    """What `up` wrote down about the instance it built and started."""

    name: str
    revision: str
    code: str
    python: str
    port: int
    pid: int | None = None
    started: float | None = None
    last_exit: int | None = None


class Instance:
    def __init__(self, name: str) -> None:
        if not name.isidentifier():
            raise Refused(f"instance name {name!r}: letters, digits and _ only")
        self.name = name
        self.dir = home() / name
        self.config = self.dir / "config"
        self.log = self.config / "home-assistant.log"
        self.auth_file = self.dir / "auth.json"
        self.namespace = f"mxz-{name}"
        self._record_file = self.dir / "instance.json"
        self._lock: int | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.record().port}"

    def record(self) -> Record:
        if not self._record_file.exists():
            raise Refused(f"no instance {self.name} at {self.dir}: run up first")
        return Record(**json.loads(self._record_file.read_text()))

    def _write(self, record: Record) -> None:
        self._record_file.write_text(json.dumps(asdict(record), indent=2) + "\n")

    def hold(self) -> None:
        """One driver per instance: a second is refused, never queued."""
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = os.open(self.dir / "driver.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(f"another driver holds {self.name}") from None

    def namespace_exists(self) -> bool:
        return Path("/run/netns", self.namespace).exists()

    def inside_namespace(self) -> bool:
        if not self.namespace_exists():
            return False
        return os.stat("/proc/self/ns/net").st_ino == os.stat(f"/run/netns/{self.namespace}").st_ino

    def make_namespace(self) -> None:
        if os.geteuid() != 0:
            raise Refused("a loopback-only namespace needs root here; refusing to start without one")
        if not self.namespace_exists():
            subprocess.run(["ip", "netns", "add", self.namespace], check=True)
        subprocess.run(["ip", "-n", self.namespace, "link", "set", "lo", "up"], check=True)

    def enter_namespace(self) -> None:
        """Re-run this command inside the instance's namespace, holding nothing."""
        if self.inside_namespace():
            return
        if not self.namespace_exists():
            raise Refused(f"no namespace {self.namespace}: run up first")
        os.execvp("ip", ["ip", "netns", "exec", self.namespace, sys.executable, "-E", "-s", *sys.argv])

    def build(self, rev: str) -> None:
        """A fresh config directory with this checkout's integration and the equipment."""
        if self.config.exists():
            shutil.rmtree(self.config)
        components = self.config / "custom_components"
        ignore = shutil.ignore_patterns("__pycache__")
        shutil.copytree(PRODUCT, components / "mxz_coordinator", ignore=ignore)
        shutil.copytree(EQUIPMENT, components / "invented_house", ignore=ignore)
        house = json.loads((EQUIPMENT / "house.json").read_text())
        (self.config / "configuration.yaml").write_text(render_configuration(house["site"]))
        self.auth_file.unlink(missing_ok=True)
        self._write(Record(self.name, rev, digest(PRODUCT, EQUIPMENT), sys.executable, PORT))

    def pid(self) -> int | None:
        """The process this instance started, if it is still that process."""
        if not self._record_file.exists():
            return None
        pid = json.loads(self._record_file.read_text()).get("pid")
        if pid is None:
            return None
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            return None
        return pid if str(self.config).encode() in cmdline else None

    def start(self) -> None:
        if self.pid() is not None:
            raise Refused(f"{self.name} is already running")
        if not self.inside_namespace():
            raise Refused("start runs only inside the instance's namespace")
        record = self.record()
        out = (self.dir / "hass.out").open("ab")
        process = subprocess.Popen(
            [record.python, "-m", "homeassistant", "--skip-pip", "-c", str(self.config)],
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=self.dir,
        )
        record.pid, record.started = process.pid, time.time()
        self._write(record)

    def _end(self, sig: signal.Signals, timeout: float) -> int | None:
        pid = self.pid()
        if pid is None:
            return None
        os.kill(pid, sig)
        deadline = time.monotonic() + timeout
        status: int | None = None
        while time.monotonic() < deadline:
            done, raw = os.waitpid(pid, os.WNOHANG) if _is_child(pid) else (0, 0)
            if done:
                status = os.waitstatus_to_exitcode(raw)
                break
            if not Path(f"/proc/{pid}").exists():
                break
            time.sleep(0.2)
        else:
            raise Refused(f"pid {pid} still running {timeout:.0f} s after {sig.name}")
        raw = json.loads(self._record_file.read_text())
        raw.update(pid=None, last_exit=status)
        self._record_file.write_text(json.dumps(raw, indent=2) + "\n")
        return status

    def stop(self) -> int | None:
        return self._end(signal.SIGTERM, 120)

    def reset(self) -> None:
        if self.pid() is not None:
            self.stop()
        if self.namespace_exists():
            subprocess.run(["ip", "netns", "delete", self.namespace], check=True)
        if self.dir.exists():
            shutil.rmtree(self.dir)


def _is_child(pid: int) -> bool:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1]) == os.getpid()
    except (FileNotFoundError, IndexError, ValueError):
        return False
