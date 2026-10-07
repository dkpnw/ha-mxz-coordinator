"""Build and bind the additional exact HA 2026.10 test environment.

Only public, hash-locked inputs are accepted. Generated archives get their own
observed hashes; the stock plugin version is not a public release identity.
"""

import argparse
import email
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import sysconfig
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
INPUTS = SOURCE / "requirements/ha2026.10"
PLUGIN = "pytest-homeassistant-custom-component"
RESERVE = 1610612736


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def normalized(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def load_bundle():
    data = json.loads((INPUTS / "bundle.json").read_text())
    validate_bundle(data)
    for filename, names in (("artifacts.lock", {r["name"] for r in data["artifacts"]}),
                            ("build.lock", set(data["build_names"]))):
        expected = "".join(f"{r['name']} @ {r['url']} --hash=sha256:{r['sha256']}\n"
                           for r in data["artifacts"] if r["name"] in names)
        require((INPUTS / filename).read_text() == expected, "lock does not match bundle")
    return data


def validate_bundle(data):
    require(data["schema"] == 1, "unsupported schema")
    p = data["python"]
    require((p["version"], p["platform_version"], p["arch"]) ==
            ("3.14.8", "26.04", "x64"), "unsupported Python/platform")
    require(p["url"] == "https://github.com/actions/python-versions/releases/download/"
            "3.14.8-36806082737/python-3.14.8-linux-26.04-x64.tar.gz", "wrong Python archive")
    require(p["sha256"] == "53eb0aed7bfb9a3d28b0842ada81dc7b1432fd697cb35653b79156b1efd0d3fa",
            "wrong Python archive digest")
    require(data["core"]["version"] == "2026.10.0" and data["core"]["commit"] ==
            "6a811d3359c7b2076dc9e1cf900843a129c044af", "wrong core source")
    require(data["plugin"]["version"] == "0.13.370" and data["plugin"]["commit"] ==
            "aacf4b5e11e16da8f6ccc67fe3729441a51c282e", "wrong plugin source")
    require(data["core"]["repository"] == "https://github.com/home-assistant/core.git" and
            data["plugin"]["repository"] ==
            "https://github.com/MatthewFlamm/pytest-homeassistant-custom-component.git",
            "wrong source origin")
    rows = data["artifacts"]
    require(len(rows) == 157 and len({r["name"] for r in rows}) == 157, "incomplete artifact freeze")
    require(PLUGIN not in {r["name"] for r in rows}, "public beta plugin is forbidden")
    for row in rows:
        require(re.fullmatch(r"https://files\.pythonhosted\.org/packages/[A-Za-z0-9/_.+-]+",
                             row["url"]), "nonpublic or private artifact path")
        require(re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "missing artifact hash")
        require(row["kind"] in ("wheel", "sdist"), "unknown artifact kind")
    core = next(r for r in rows if r["name"] == "homeassistant")
    require(core["version"] == "2026.10.0" and core["sha256"] ==
            "1d6a7dc8e55216425f61915c095cad99b768679d9a829e58d2334333ba5d4446", "wrong core wheel")
    for group in (data["core"]["inputs"], data["plugin"]["stock_files"], data["plugin"]["outputs"]):
        for name, row in group.items():
            require(not Path(name).is_absolute() and ".." not in Path(name).parts, "unsafe source path")
            require(re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "missing source hash")


def platform_errors(system, machine, libc, version, os_id=None, os_version=None, hosted=False):
    errors = []
    if system != "Linux" or machine != "x86_64" or libc != "glibc":
        errors.append("unsupported Linux/x86_64/glibc platform")
    if not re.fullmatch(r"\d+\.\d+", version or "") or tuple(map(int, version.split("."))) < (2, 41):
        errors.append("glibc below selected manylinux_2_41 wheels")
    if hosted and (os_id, os_version) != ("ubuntu", "26.04"):
        errors.append("hosted runner must be Ubuntu 26.04")
    return errors


def space(root):
    s = os.statvfs(root)
    return {"free_bytes": s.f_bavail * s.f_frsize, "free_inodes": s.f_favail}


def admission(root):
    observed = space(root)
    # Remaining prospective peak, including one retained archive, Git/build,
    # installed tree, logs and cache; concurrent growth is a planning allowance.
    require(observed["free_bytes"] >= RESERVE + 3 * 2**30 and
            observed["free_inodes"] >= 350000, "capacity admission refused")
    return observed


def environment(root):
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(root / "home"),
           "TMPDIR": str(root / "tmp"), "XDG_CACHE_HOME": str(root / "cache"),
           "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": str(root / "bytecode"),
           "PIP_CONFIG_FILE": "/dev/null", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
           "PIP_NO_CACHE_DIR": "1", "PIP_INDEX_URL": "https://pypi.org/simple",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8"}
    for key in ("HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "PYTHONPYCACHEPREFIX"):
        Path(env[key]).mkdir(exist_ok=True)
    return env


def run(root, label, argv, cwd=None, extra=None):
    before = admission(root)
    folder = root / "logs" / label
    folder.mkdir(parents=True, exist_ok=False)
    env = environment(root)
    env.update(extra or {})
    record = {"argv": list(map(str, argv)), "cwd": str(cwd or SOURCE), "env": env,
              "parent_pid": os.getpid(), "started": time.time(), "before": before}
    with (folder / "stdout").open("wb") as out, (folder / "stderr").open("wb") as err:
        child = subprocess.Popen(argv, cwd=cwd or SOURCE, env=env, stdout=out, stderr=err,
                                 start_new_session=True)
        record["pid"] = child.pid
        minimum = before.copy()
        while child.poll() is None:
            now = space(root)
            minimum = {k: min(minimum[k], now[k]) for k in now}
            if now["free_bytes"] < RESERVE + 64 * 2**20 or now["free_inodes"] < 251000:
                record["capacity_terminated"] = now
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                break
            time.sleep(.05)
        record.update(rc=child.wait(), minimum_sampled=minimum)
    record.update(finished=time.time(), after=space(root))
    for name in ("stdout", "stderr"):
        record[name] = {"bytes": (folder / name).stat().st_size, "sha256": digest(folder / name)}
    write_json(folder / "receipt.json", record)
    print(f"COMMAND {label} rc={record['rc']}", flush=True)
    require(record["rc"] == 0, f"{label} failed; retain {folder}")
    return (folder / "stdout").read_text()


def fetch(root, row):
    admission(root)
    path = root / "downloads" / row["url"].rsplit("/", 1)[1]
    path.parent.mkdir(exist_ok=True)
    if not path.exists():
        record = {"url": row["url"], "pid": os.getpid(), "started": time.time()}
        try:
            with urllib.request.urlopen(row["url"], timeout=90) as response, path.open("xb") as output:
                record.update(status=response.status, final_url=response.url)
                while block := response.read(1024 * 1024):
                    require(space(root)["free_bytes"] > RESERVE + 64 * 2**20, "download reserve")
                    output.write(block)
        except Exception as exc:
            record["error"] = repr(exc)
            raise
        finally:
            record["finished"] = time.time()
            write_json(path.with_name(path.name + ".http.json"), record)
    require(digest(path) == row["sha256"], f"archive hash mismatch: {path.name}")
    return path


def check_files(base, expected):
    for name, row in expected.items():
        path = base / name
        require(path.is_file() and not path.is_symlink() and digest(path) == row["sha256"],
                f"source/output mismatch: {name}")


def prepare_python(root, data):
    archive = fetch(root, data["python"])
    base = root / "python26"
    admission(root)
    if not base.exists():
        with tarfile.open(archive) as source:
            source.extractall(base, filter="data")
    # Verify actual executable, stdlib and libpython against every regular member,
    # not just the version banner; setup.sh is deliberately never executed.
    with tarfile.open(archive) as source:
        for member in source:
            path = base / member.name
            if member.isfile():
                require(path.is_file() and not path.is_symlink(), "missing Python archive member")
                require(digest(path) == hashlib.sha256(source.extractfile(member).read()).hexdigest(),
                        f"changed Python member: {member.name}")
    raw = base / "bin/python3.14"
    run(root, "create-venv", [raw, "-B", "-m", "venv", "--without-pip", root / "venv"],
        extra={"LD_LIBRARY_PATH": str(base / "lib")})
    # Re-establish the exact library even when an isolated child strips its env.
    # Only links in this newly owned venv are replaced, never shared runtimes.
    bindir = root / "venv/bin"
    for name in ("python", "python3", "python3.14"):
        path = bindir / name
        require(path.is_symlink(), "expected a newly created venv symlink")
        path.unlink()
    wrapper = bindir / "python"
    wrapper.write_text(python_wrapper(root))
    wrapper.chmod(0o755)
    for name in ("python3", "python3.14"):
        (bindir / name).symlink_to("python")
    run(root, "bootstrap-pip", [wrapper, "-m", "ensurepip"])
    return wrapper


def python_wrapper(root):
    # Sanitized test children may drop HOME/TMPDIR as well as LD_LIBRARY_PATH.
    # Rebind every writable destination before Python (including -I) starts.
    env = environment(root)
    exports = {key: value for key, value in env.items() if key not in ("PATH", "LC_ALL")}
    exports["LD_LIBRARY_PATH"] = str(root / "python26/lib")
    return ("#!/bin/bash\n" + "".join("export " + key + "=" + shlex.quote(value) + "\n"
                                      for key, value in exports.items()) +
            "exec -a " + shlex.quote(str(root / "venv/bin/python")) + " " +
            shlex.quote(str(root / "python26/bin/python3.14")) + " -B -X " +
            shlex.quote("pycache_prefix=" + str(root / "bytecode")) + " \"$@\"\n")


def check_runtime_wrapper(root, data):
    wrapper = root / "venv/bin/python"
    require(wrapper.is_file() and not wrapper.is_symlink() and wrapper.read_text() == python_wrapper(root),
            "wrong owned Python wrapper")
    for name, expected in data["python"]["members"].items():
        require(digest(root / "python26" / name) == expected, "wrong exact Python member: " + name)


def checkout(root, name, spec, destination, sparse=None):
    run(root, name + "-init", ["git", "init", destination])
    run(root, name + "-remote", ["git", "remote", "add", "origin", spec["repository"]], destination)
    ref = "refs/tags/2026.10.0:refs/tags/2026.10.0" if name == "core" else spec["commit"]
    run(root, name + "-fetch", ["git", "fetch", "--depth=1", "--filter=blob:none", "origin", ref], destination)
    if sparse:
        run(root, name + "-sparse", ["git", "sparse-checkout", "set", "--no-cone",
                                     *["/" + p for p in sparse]], destination)
    run(root, name + "-checkout", ["git", "checkout", "--detach", spec["commit"]], destination)
    result = run(root, name + "-identity", ["git", "rev-parse", "HEAD", "HEAD^{tree}"], destination)
    require(result.splitlines() == [spec["commit"], spec["tree"]], "wrong fetched source identity")
    if name == "core":
        tags = run(root, "core-tags", ["git", "tag", "--list"], destination)
        require(tags.splitlines() == ["2026.10.0"], "generator tag universe differs")
        target = run(root, "core-tag-target", ["git", "rev-parse", "2026.10.0^{commit}"], destination)
        require(target.strip() == spec["commit"], "wrong core tag")
        check_files(destination, spec["inputs"])
        for path, row in spec["inputs"].items():
            content = (destination / path).read_bytes()
            require(hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
                    == row["git_blob"], "core blob differs")


def wheel_metadata(path, expected_name, expected_version, metadata_hash=None):
    with zipfile.ZipFile(path) as archive:
        members = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA") and n.count("/") == 1]
        require(len(members) == 1, "ambiguous wheel metadata")
        raw = archive.read(members[0])
        meta = email.message_from_bytes(raw)
        require(normalized(meta["Name"]) == expected_name and meta["Version"] == expected_version,
                "wrong wheel identity")
        if metadata_hash:
            require(hashlib.sha256(raw).hexdigest() == metadata_hash, "wrong wheel metadata bytes")
        if expected_name == PLUGIN:
            require("homeassistant==2026.10.0" in meta.get_all("Requires-Dist", []),
                    "plugin requires beta or wrong core")
        return {"name": expected_name, "version": expected_version, "sha256": digest(path),
                "metadata_sha256": hashlib.sha256(raw).hexdigest(), "filename": path.name}


def prepare(root, data):
    os_info = platform.freedesktop_os_release()
    require(not platform_errors(platform.system(), platform.machine(), *platform.libc_ver(),
                                os_info.get("ID"), os_info.get("VERSION_ID")), "unsupported build host")
    root.mkdir(parents=True, exist_ok=True)
    require(not (root / "venv").exists() and not (root / "plugin").exists(), "setup already exists; no overwrite")
    environment(root)
    prepare_python(root, data)
    build(root, data)


def build(root, data):
    require(not (root / "plugin").exists(), "plugin source already exists; no overwrite")
    check_runtime_wrapper(root, data)
    python = root / "venv/bin/python"
    run(root, "build-tools", [python, "-m", "pip", "install", "--require-hashes",
                              "-r", INPUTS / "build.lock"])
    plugin = root / "plugin"
    checkout(root, "plugin", data["plugin"], plugin)
    check_files(plugin, data["plugin"]["stock_files"])
    checkout(root, "core", data["core"], plugin / "tmp_dir", data["core"]["inputs"])
    run(root, "stock-generation", [python, "generate_phacc/generate_phacc.py", "--regen"], plugin,
        {"PYTHONPATH": str(plugin / "src") + os.pathsep + str(plugin)})
    check_files(plugin, data["plugin"]["outputs"])
    check_files(plugin, data["plugin"]["stock_files"])
    check_files(plugin / "tmp_dir", data["core"]["inputs"])
    run(root, "stock-package", [python, "setup.py", "sdist", "bdist_wheel"], plugin)
    wheels = list((plugin / "dist").glob("*.whl"))
    require(len(wheels) == 1, "ambiguous generated wheel")
    generated = wheel_metadata(wheels[0], PLUGIN, data["plugin"]["version"], data["plugin"]["metadata_sha256"])
    with zipfile.ZipFile(wheels[0]) as archive:
        expected = {name.removeprefix("src/"): row for name, row in data["plugin"]["outputs"].items()
                    if name.startswith("src/")}
        require({n for n in archive.namelist() if n.endswith(".py")} == set(expected),
                "generated wheel file set differs")
        for name, row in expected.items():
            require(hashlib.sha256(archive.read(name)).hexdigest() == row["sha256"], "generated wheel bytes differ")
    write_json(root / "generated-artifact.json", generated)
    install(root, data)


def install(root, data):
    check_runtime_wrapper(root, data)
    python = root / "venv/bin/python"
    plugin = root / "plugin"
    check_files(plugin, data["plugin"]["outputs"])
    check_files(plugin, data["plugin"]["stock_files"])
    check_files(plugin / "tmp_dir", data["core"]["inputs"])
    wheels = list((plugin / "dist").glob("*.whl"))
    require(len(wheels) == 1, "ambiguous generated wheel")
    generated = wheel_metadata(wheels[0], PLUGIN, data["plugin"]["version"], data["plugin"]["metadata_sha256"])
    artifacts = []
    for row in data["artifacts"]:
        archive = fetch(root, row)
        if row["kind"] == "wheel":
            wheel_metadata(archive, row["name"], row["version"], row["metadata_sha256"])
        artifacts.append((row, archive))
    run(root, "platform-wheel-tags", [python, __file__, "tags", root])
    # Explicit source builds use the frozen installed backend, offline. They are
    # new local artifacts; their byte hashes are recorded after the real build.
    sources = [(row, path) for row, path in artifacts if row["kind"] == "sdist"]
    source_lock = root / "sources.lock"
    source_lock.write_text("".join(f"{r['name']} @ {p.as_uri()} --hash=sha256:{r['sha256']}\n" for r, p in sources))
    built = root / "built"
    built.mkdir()
    run(root, "source-wheels", [python, "-m", "pip", "wheel", "--no-index", "--find-links",
                                root / "downloads", "--no-build-isolation", "--require-hashes",
                                "--wheel-dir", built, "-r", source_lock])
    actual = []
    for row, path in artifacts:
        if row["kind"] == "sdist":
            # PyRIC retains uppercase in its wheel filename.
            matches = [p for p in built.glob("*.whl") if normalized(p.name.split("-")[0]) == row["name"]]
            require(len(matches) == 1, "missing/ambiguous source-built wheel")
            path = matches[0]
        info = wheel_metadata(path, row["name"], row["version"])
        info.update(input_url=row["url"], input_sha256=row["sha256"], kind=row["kind"], path=str(path))
        actual.append(info)
    generated.update(kind="generated", path=str(wheels[0]))
    actual.append(generated)
    write_json(root / "actual-artifacts.json", actual)
    install_lock = root / "install.lock"
    install_lock.write_text("".join(f"{r['name']} @ {Path(r['path']).as_uri()} --hash=sha256:{r['sha256']}\n" for r in actual))
    run(root, "install", [python, "-m", "pip", "install", "--no-index", "--require-hashes", "-r", install_lock])
    run(root, "pip-check", [python, "-m", "pip", "check"])
    run(root, "binding", [python, __file__, "verify", root])
    # Real nested isolated invocation, with no LD_LIBRARY_PATH/PYTHONPATH inherited.
    run(root, "isolated-binding", [python, "-I", __file__, "verify", root])
    print("SETUP_COMPLETE (not product qualification)")


def tags(root, data):
    from packaging.tags import sys_tags
    from packaging.utils import parse_wheel_filename

    supported = set(sys_tags())
    rows = []
    for row in data["artifacts"]:
        if row["kind"] != "wheel":
            continue
        wheel_tags = parse_wheel_filename(row["url"].rsplit("/", 1)[1])[3]
        require(bool(wheel_tags & supported), "unsupported selected wheel: " + row["name"])
        rows.append({"name": row["name"], "tags": sorted(map(str, wheel_tags)),
                     "matched": sorted(map(str, wheel_tags & supported))})
    write_json(root / "wheel-tags.json", rows)
    print("ALL_SELECTED_WHEEL_TAGS_SUPPORTED", len(rows))


def validate_actual_artifacts(root, data, actual):
    expected = {r["name"]: r for r in data["artifacts"]}
    expected[PLUGIN] = {"version": data["plugin"]["version"], "kind": "generated",
                        "metadata_sha256": data["plugin"]["metadata_sha256"]}
    require(len(actual) == 158 and {r["name"] for r in actual} == set(expected), "wrong actual artifact set")
    for row in actual:
        spec = expected[row["name"]]
        path = Path(row["path"]).resolve()
        require(path.is_relative_to(root) and digest(path) == row["sha256"], "actual archive binding differs")
        require(row["version"] == spec["version"] and row["kind"] == spec["kind"], "actual artifact identity differs")
        if row["kind"] != "generated":
            require(row["input_url"] == spec["url"] and row["input_sha256"] == spec["sha256"], "input origin differs")
            source = root / "downloads" / spec["url"].rsplit("/", 1)[1]
            require(digest(source) == spec["sha256"], "source archive differs")
        if row["kind"] == "wheel":
            require(row["sha256"] == spec["sha256"], "public wheel bytes differ")
        if row["kind"] != "sdist":
            require(row["metadata_sha256"] == spec["metadata_sha256"], "frozen metadata differs")


def verify(root, data):
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet

    check_runtime_wrapper(root, data)
    require(sys.version.split()[0] == "3.14.8", "wrong exact Python")
    require(sys.dont_write_bytecode, "bytecode must be disabled")
    for key, suffix in (("HOME", "home"), ("TMPDIR", "tmp"), ("XDG_CACHE_HOME", "cache"),
                        ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data")):
        require(os.environ.get(key) == str(root / suffix), "wrong owned environment: " + key)
    require(sys.pycache_prefix == str(root / "bytecode"), "wrong bytecode destination")
    for entry in sys.path:
        path = Path(entry).resolve()
        require(any(path.is_relative_to(base) for base in (root / "python26", root / "venv", SOURCE / "tools")),
                "foreign import path: " + str(path))
    require(Path(sys.prefix).resolve() == root / "venv" and
            Path(sys.base_prefix).resolve() == root / "python26", "wrong Python/site binding")
    require(Path(sysconfig.get_path("stdlib")).resolve() == root / "python26/lib/python3.14",
            "wrong standard library")
    require(digest(root / "python26/lib/python3.14/ensurepip/_bundled/pip-26.2.1-py3-none-any.whl") ==
            data["python"]["pip_wheel_sha256"], "wrong bootstrap pip archive")
    actual = json.loads((root / "actual-artifacts.json").read_text())
    validate_actual_artifacts(root, data, actual)
    wanted = {r["name"]: r for r in actual}
    dists = list(importlib.metadata.distributions())
    inventory = {normalized(d.metadata["Name"]): d for d in dists}
    require(len(inventory) == len(dists) and set(inventory) == set(wanted) | {"pip"}, "installed inventory differs")
    require(inventory["pip"].version == data["python"]["pip_version"], "wrong bootstrap pip version")
    site = root / "venv/lib/python3.14/site-packages"
    for name, row in wanted.items():
        dist = inventory[name]
        require(dist.version == row["version"], "wrong installed version: " + name)
        require(Path(dist.locate_file("")).resolve() == site, "wrong installed package path")
        raw = dist.read_text("METADATA").encode()
        require(hashlib.sha256(raw).hexdigest() == row["metadata_sha256"], "installed metadata mismatch: " + name)
        # Bind all unrelocated wheel members, including native modules. RECORD and
        # installation-generated scripts/direct_url are separately reported.
        with zipfile.ZipFile(row["path"]) as archive:
            for member in archive.namelist():
                if member.endswith(("/", ".dist-info/RECORD")) or ".data/" in member:
                    continue
                installed = site / member
                require(installed.is_file() and digest(installed) ==
                        hashlib.sha256(archive.read(member)).hexdigest(), "installed wheel member mismatch: " + member)
    marker_env = default_environment()
    extras = {n: set() for n in inventory}
    changed = True
    while changed:
        changed = False
        for name, dist in inventory.items():
            for raw in dist.requires or []:
                req = Requirement(raw)
                if req.marker and not any(req.marker.evaluate({**marker_env, "extra": e}) for e in extras[name] | {""}):
                    continue
                dependency = normalized(req.name)
                require(dependency in inventory and inventory[dependency].version in req.specifier,
                        "unsatisfied requirement: " + raw)
                before = set(extras[dependency])
                extras[dependency].update(req.extras)
                changed |= before != extras[dependency]
    for dist in dists:
        require(not dist.metadata.get("Requires-Python") or
                platform.python_version() in SpecifierSet(dist.metadata["Requires-Python"]), "Requires-Python mismatch")
    modules = {}
    for name in ("homeassistant.const", "homeassistant.core", "pytest_homeassistant_custom_component.const",
                 "pytest_homeassistant_custom_component.plugins", "pytest_homeassistant_custom_component.common",
                 "aiohttp", "orjson", "numpy", "cryptography", "sqlalchemy", "bluetooth_data_tools",
                 "fnv_hash_fast", "ulid_transform"):
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        require(path.is_relative_to(site), "module outside installed site: " + name)
        modules[name] = {"file": str(path), "sha256": digest(path)}
    for name in ("homeassistant.const", "pytest_homeassistant_custom_component.const"):
        require(sys.modules[name].__version__ == "2026.10.0", "wrong core/plugin module version")
    maps = Path("/proc/self/maps").read_text()
    libraries = sorted({line.split()[-1] for line in maps.splitlines() if "libpython" in line})
    require(libraries == [str(root / "python26/lib/libpython3.14.so.1.0")], "wrong loaded libpython")
    result = {"python": sys.version, "executable": sys.executable, "prefix": sys.prefix,
              "base_prefix": sys.base_prefix, "stdlib": sysconfig.get_path("stdlib"),
              "configured_libdir": sysconfig.get_config_var("LIBDIR"),
              "loaded_native_libraries": sorted({line.split()[-1] for line in maps.splitlines()
                                                 if ".so" in line and line.split()[-1].startswith("/")}),
              "sys_path": sys.path, "isolated": sys.flags.isolated, "dont_write_bytecode": sys.dont_write_bytecode,
              "pycache_prefix": sys.pycache_prefix, "modules": modules, "libpython": libraries,
              "libpython_sha256": digest(libraries[0]), "binary_sha256": digest(root / "python26/bin/python3.14"),
              "inventory": {n: {"version": d.version, "metadata_sha256": hashlib.sha256(d.read_text("METADATA").encode()).hexdigest(),
                                "direct_url": d.read_text("direct_url.json")} for n, d in inventory.items()},
              "environment": {k: os.environ.get(k) for k in ("HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
                                                            "XDG_DATA_HOME", "PYTHONPYCACHEPREFIX", "LD_LIBRARY_PATH")},
              "pid": os.getpid(), "platform": platform.platform(), "libc": platform.libc_ver()}
    print(json.dumps(result, sort_keys=True))


def phase_log_errors(raw, exit_text):
    """Require complete, ordered, ordinary phases and an independently saved exit."""
    errors = []
    if exit_text != "0\n":
        errors.append("nonzero or missing suite exit")
    collected, complete, valid, reports = [], [], [], {}
    try:
        for line in raw.splitlines():
            # pytest progress marks share the teardown line with the unchanged helper.
            line = re.sub(r"^[.FEsxX]+(?=PHASE )", "", line)
            if line.startswith("COLLECTED "):
                collected.append(json.loads(line[10:]))
            elif line.startswith("PHASES_COMPLETE "):
                complete.append(json.loads(line[16:]))
            elif line.startswith("PHASES_VALID="):
                valid.append(line)
            elif line.startswith("PHASE "):
                phase = json.loads(line[6:])
                reports.setdefault(phase["node"], []).append((phase["phase"], phase["outcome"]))
        if (len(collected) != 1 or not isinstance(collected[0], list) or not collected[0] or
                not all(isinstance(node, str) for node in collected[0]) or
                len(set(collected[0])) != len(collected[0])):
            errors.append("missing or repeated collection")
        elif set(reports) != set(collected[0]) or any(
                reports.get(node) != [(p, "passed") for p in ("setup", "call", "teardown")]
                for node in collected[0]):
            errors.append("missing, repeated or unsuccessful phases")
        if (complete != [{"classes": {}, "errors": [], "exit": 0}] or
                type(complete[0]["exit"]) is not int or valid != ["PHASES_VALID=true"]):
            errors.append("incomplete phase completion")
    except (ValueError, KeyError, TypeError):
        errors.append("malformed phase evidence")
    return errors


def export(root, data):
    errors = []
    for label in ("create-venv", "bootstrap-pip", "build-tools", "plugin-identity", "core-identity",
                  "core-tags", "core-tag-target", "stock-generation", "stock-package", "platform-wheel-tags",
                  "source-wheels", "install", "pip-check", "binding", "isolated-binding"):
        folder = root / "logs" / label
        try:
            receipt = json.loads((folder / "receipt.json").read_text())
            require(receipt["rc"] == 0, "nonzero setup command")
            for stream in ("stdout", "stderr"):
                require(digest(folder / stream) == receipt[stream]["sha256"] and
                        (folder / stream).stat().st_size == receipt[stream]["bytes"], "changed command output")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append("invalid setup receipt " + label + ": " + str(exc))
    for name in ("source.txt", "source-files.sha256", "runner.txt", "setup.log", "checks.log",
                 "generated-artifact.json", "actual-artifacts.json", "install.lock", "wheel-tags.json",
                 "sentinel.log", "sentinel.exit", "suite.log", "suite.exit"):
        path = root / name
        if not path.is_file():
            errors.append("missing " + name)
    for mode in ("sentinel", "suite"):
        log, status = root / (mode + ".log"), root / (mode + ".exit")
        if log.is_file() and status.is_file():
            errors.extend(phase_log_errors(log.read_text(), status.read_text()))
    checks = root / "checks.log"
    if checks.is_file():
        errors.extend(phase_log_errors(checks.read_text(), "0\n"))
    # Bounded complete text export, never truncate a log and label it complete.
    paths = [p for p in root.glob("*") if p.is_file()]
    paths += sorted((root / "logs").glob("*/*"))
    for path in sorted(paths):
        limit = {"suite.log": 5 * 2**20, "sentinel.log": 2**20, "checks.log": 2**20}.get(path.name, 6 * 2**20)
        if path.stat().st_size > limit or path.is_symlink():
            errors.append("oversize or symlink " + str(path))
            continue
        print("FILE", path.relative_to(root), "BYTES", path.stat().st_size, "SHA256", digest(path))
        print(path.read_text())
    print("EVIDENCE_ERRORS", json.dumps(errors))
    require(not errors, "incomplete evidence export")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "build", "install", "verify", "tags", "schema", "export"))
    parser.add_argument("root", type=Path, nargs="?")
    args = parser.parse_args()
    data = load_bundle()
    if args.command == "schema":
        print("BUNDLE_SCHEMA_VALID")
        return
    require(args.root is not None, "owned root required")
    root = args.root.resolve()
    require(not root.is_relative_to(SOURCE) and root != SOURCE.parent, "scratch must be outside checkout")
    {"prepare": prepare, "build": build, "install": install, "verify": verify, "tags": tags, "export": export}[args.command](root, data)


if __name__ == "__main__":
    main()
