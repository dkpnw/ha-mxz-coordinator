"""Check the complete installed test inventory against one lane lock, on the lane's Python."""

import json
import re
import sys
from collections import Counter
from hashlib import sha256
from importlib.metadata import distributions
from pathlib import Path


def name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def python_errors(version, lane):
    errors = []
    if lane not in ("3.12.14", "3.13", "3.14") or not version or not (
        version == lane or version.startswith(lane + ".")
    ):
        errors.append("wrong or missing Python lane")
    return errors


def inventory_errors(expected, actual):
    """Keep duplicate names visible; pip is the sole bootstrap exception."""
    errors = []
    if not expected or not actual:
        errors.append("empty inventory")
    for label, rows in (("lock", expected), ("installed", actual)):
        counts = Counter(name(n) for n, _ in rows)
        if any(count != 1 for count in counts.values()):
            errors.append(f"duplicate normalized name in {label}")
    wanted = {name(n): v for n, v in expected}
    installed = {name(n): v for n, v in actual}
    if "pip" not in installed or not installed["pip"]:
        errors.append("missing bootstrap pip")
    installed.pop("pip", None)
    if wanted != installed:
        errors.append(f"lock mismatch: expected={wanted!r}, actual={installed!r}")
    return errors


def main():
    lock, lane = sys.argv[1:]
    raw = Path(lock).read_bytes()
    errors = python_errors(sys.version.split()[0], lane)
    assert not errors, errors
    expected = []
    for line in raw.decode().splitlines():
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s=]+", line), "malformed lock row"
        expected.append(tuple(line.split("==")))
    actual = [(d.metadata["Name"], d.version) for d in distributions()]
    assert all(n and v for n, v in actual), "incomplete distribution metadata"
    print("INSTALLED " + json.dumps(sorted(actual)))
    print("PYTHON " + json.dumps({"executable": sys.executable, "version": sys.version,
                                  "sha256": sha256(Path(sys.executable).read_bytes()).hexdigest()}))
    errors = inventory_errors(expected, actual)
    assert not errors, errors
    print("INVENTORY_COMPLETE")


if __name__ == "__main__":
    main()
