"""The evidence one journey leaves: transcript, event stream, log slice, history.

Knows nothing about what a pass means; a journey records its own expectations.
Written pass or fail, under $MXZ_VERIFY_OUT/instance/<feature>/<journey>/,
outside the checkout, and never touched by down or reset.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
INVENTED = "Invented heads on a throwaway Home Assistant. Not a hardware result."


def out_root() -> Path:
    return Path(os.environ.get("MXZ_VERIFY_OUT") or Path.home() / "verify-mxz-evidence" / STAMP) / "instance"


class Transcript:
    """One entry per step: who acted, what they did, and what was then shown."""

    def __init__(self, feature: str, journey: str, run: dict[str, Any]) -> None:
        self.dir = out_root() / feature / journey
        self.dir.mkdir(parents=True, exist_ok=True)
        self.feature = feature
        self.journey = journey
        self.run = run
        self.notes: dict[str, Any] = {}
        self.steps: list[dict[str, Any]] = []

    def step(self, actor: str, action: str, shown: dict[str, Any], calls: list[dict[str, Any]]) -> dict[str, Any]:
        entry = {
            "actor": actor,
            "action": action,
            "time": datetime.now(UTC).isoformat(),
            "shown": shown,
            "calls_since_last_step": calls,
            "expectations": [],
        }
        self.steps.append(entry)
        return entry

    def expect(self, what: str, passed: bool, seen: Any = None) -> None:
        self.steps[-1]["expectations"].append({"what": what, "passed": passed, "seen": seen})

    def save(self, outcome: str, error: str | None = None) -> Path:
        path = self.dir / "transcript.json"
        path.write_text(json.dumps({
            "feature": self.feature,
            "journey": self.journey,
            "stamp": INVENTED,
            "outcome": outcome,
            "error": error,
            "saved": time.time(),
            **self.run,
            "notes": self.notes,
            "steps": self.steps,
        }, indent=2, default=str) + "\n")
        return path

    def attach(self, name: str, content: Any) -> None:
        target = self.dir / name
        if isinstance(content, str):
            target.write_text(content)
        else:
            target.write_text(json.dumps(content, indent=2, default=str) + "\n")


def log_slice(log: Path, offset: int) -> str:
    with log.open("rb") as handle:
        handle.seek(offset)
        return handle.read().decode(errors="replace")


def write_run(record: dict[str, Any]) -> Path:
    root = out_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "run.json"
    path.write_text(json.dumps({"stamp": INVENTED, **record}, indent=2) + "\n")
    return path
