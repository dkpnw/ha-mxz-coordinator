"""The filmed demo scenes: which journeys each one films, and how it is composed.

A scene is one looping image per theme, `images/demo-<name>-<theme>.gif`, with a
still of its last frame. Its rows are stacked top to bottom; each row is one
demo journey, filmed by film.py with the named card (cards.py) on its own fresh
instance. compose.py starts every row's clock at the journey's start step and
shows the same window of each, sped up evenly. Stdlib only: film.py, compose.py,
cli.py and the suite all read it.
"""

from __future__ import annotations

from dataclasses import dataclass

from journeys import comfort, demo, mode_wait, satisfied
from journeys import fan_hold as fan


@dataclass(frozen=True)
class Row:
    feature: str  # what `mxz-instance drive` calls the journey
    journey: str  # its module
    card: str  # the dashboard film.py shows for it (cards.CARDS)


@dataclass(frozen=True)
class Scene:
    name: str
    rows: tuple[Row, ...]
    window: float  # seconds of each run shown, from its start step
    start_step: str  # the transcript action each row's clock starts at
    speed: int = 25


SCENES = {scene.name: scene for scene in (
    # The README hook. Its numbers are the ones the published GIF was made with.
    Scene("stock-vs-mxz", (
        Row("demo-stock", "journeys.demo_stock", "stock"),
        Row("demo-coordinated", "journeys.demo_coordinated", "mxz"),
    ), demo.WINDOW, demo.START_STEP),
    Scene("satisfied", (
        Row("satisfied-stock", "journeys.satisfied_stock", "stock"),
        Row("satisfied-coordinated", "journeys.satisfied_coordinated", "mxz-outdoor"),
    ), satisfied.WINDOW, satisfied.START_STEP),
    Scene("fan-hold", (Row("fan-hold", "journeys.fan_hold", "mxz-fan"),), fan.WINDOW, fan.START_STEP),
    Scene("mode-wait", (Row("mode-wait", "journeys.mode_wait", "mxz-wait"),), mode_wait.WINDOW, mode_wait.START_STEP),
    Scene("comfort-priority", (
        Row("comfort-off", "journeys.comfort_off", "mxz-comfort"),
        Row("comfort-on", "journeys.comfort_on", "mxz-comfort"),
    ), comfort.WINDOW, comfort.START_STEP, comfort.SPEED),
)}

ROWS = {row.feature: row for scene in SCENES.values() for row in scene.rows}
