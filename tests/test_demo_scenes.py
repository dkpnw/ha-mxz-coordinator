"""The filmed demo scenes: the registry, the cards and compose.py.

The journeys that film a scene check its beats on a throwaway instance. This
checks what decides which journey and card each row shows, that a card names
only entities that exist, and how compose.py lines the rows up and times them.
Every room and entity here is invented.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

INSTANCE = Path(__file__).parents[1] / "verification" / "instance"
sys.path.append(str(INSTANCE))

import cards
import compose
from journeys.drive import ENTRY_KEYS, ROOM_KEYS, SLUGS
from scenes import SCENES, Row, Scene

HOUSE = json.loads((INSTANCE / "invented_house" / "house.json").read_text())
# What setup-and-enable creates for the invented house's three rooms, by unique-id key.
MXZ_KEYS = [f"{slug}_{key}" for slug in SLUGS for key in ROOM_KEYS] + list(ENTRY_KEYS)
ENTITY = re.compile(r"\b(?:climate|sensor|binary_sensor|switch|number|select)\.[a-z0-9_]+")


def registry_problems(scenes: list[Scene], known_cards: set[str]) -> list[str]:
    """Rows that drive or film.py could not run as the scene says."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    for scene in scenes:
        if not scene.rows:
            problems.append(f"{scene.name} has no rows")
        for row in scene.rows:
            if row.feature in seen:
                problems.append(f"{row.feature} is a row of both {seen[row.feature]} and {scene.name}")
            seen[row.feature] = scene.name
            if row.card not in known_cards:
                problems.append(f"{row.feature} has no card {row.card}")
            module = importlib.import_module(row.journey)
            if getattr(module, "FEATURE", None) != row.feature or not callable(getattr(module, "run", None)):
                problems.append(f"{row.journey} is not the {row.feature} journey")
    return problems


def test_every_row_is_one_journey_with_a_card() -> None:
    assert registry_problems(list(SCENES.values()), set(cards.CARDS)) == []


def test_the_registry_check_reports_each_kind_of_bad_row() -> None:
    """Negative: a feature in two scenes, a card that does not exist, a module of another feature."""
    stock = Row("demo-stock", "journeys.demo_stock", "stock")
    broken = [
        Scene("one", (stock,), 30, "start"),
        Scene("two", (stock, Row("fan-hold", "journeys.fan_hold", "no-such-card"),
                      Row("comfort-on", "journeys.comfort_off", "mxz-comfort")), 30, "start"),
        Scene("three", (), 30, "start"),
    ]
    assert registry_problems(broken, set(cards.CARDS)) == [
        "demo-stock is a row of both one and two",
        "fan-hold has no card no-such-card",
        "journeys.comfort_off is not the comfort-on journey",
        "three has no rows",
    ]


def test_the_hook_keeps_what_its_published_gif_was_made_with() -> None:
    """The README's top demo: a recapture must film and compose it the same way."""
    hook = SCENES["stock-vs-mxz"]
    assert (hook.window, hook.speed, hook.start_step) == (450, 10, "start: Studio reads 76, Loft and Den read 70")
    assert [(row.feature, row.card) for row in hook.rows] == [("demo-stock", "stock"), ("demo-coordinated", "mxz")]
    assert next(iter(SCENES)) == "stock-vs-mxz"


def test_every_scene_s_window_ends_on_a_frame() -> None:
    for scene in SCENES.values():
        assert scene.window % compose.EVERY == 0, scene.name


# --- cards --------------------------------------------------------------------


def unknown_entities(card: str, known: set[str]) -> list[str]:
    """Entity ids a card's templates name that neither the house nor MXZ creates."""
    return sorted(set(ENTITY.findall(card)) - known)


def _house_entities() -> set[str]:
    rooms = [room["key"] for room in HOUSE["rooms"]]
    return {"sensor.outdoor_unit"} | {
        entity for key in rooms for entity in (
            f"climate.{key}_head", f"sensor.{key}_temperature",
            f"binary_sensor.{key}_window", f"binary_sensor.{key}_occupancy",
        )
    }


def _mxz_ids() -> dict[str, str]:
    return {key: f"sensor.mxz_{key}" for key in MXZ_KEYS}


@pytest.mark.parametrize("name", sorted(cards.CARDS))
def test_each_card_names_only_entities_that_exist(name: str) -> None:
    """Built from exactly the ids setup-and-enable creates: an unknown key raises KeyError."""
    ids = _mxz_ids()
    card = cards.CARDS[name](ids)
    assert unknown_entities(card, _house_entities() | set(ids.values())) == []
    assert [line.split("|")[1].strip() for line in card.splitlines() if line.startswith("| **")] == [
        f"**{room['name']}**" for room in HOUSE["rooms"]
    ]


def test_the_entity_check_reports_a_room_the_house_does_not_have() -> None:
    """Negative."""
    card = cards.stock_card() + "\n| **Attic** | {{ states('sensor.attic_temperature') }} |"
    assert unknown_entities(card, _house_entities()) == ["sensor.attic_temperature"]


def render(card: str, states: dict[str, tuple[str, dict]]) -> str:
    """The card as Home Assistant's markdown would get it, over invented states."""
    import jinja2

    env = jinja2.Environment()
    env.globals["states"] = lambda entity: states.get(entity, ("unknown", {}))[0]
    env.globals["state_attr"] = lambda entity, name: states.get(entity, ("unknown", {}))[1].get(name)
    return env.from_string(card).render()


def cells(shown: str) -> list[list[str]]:
    """Each room row of a rendered card, as its stripped cells."""
    return [[cell.strip() for cell in line.split("|")[1:-1]] for line in shown.splitlines() if line.startswith("| **")]


def _stock_states(standby: bool) -> dict[str, tuple[str, dict]]:
    heads = {"studio": ("cool", {"target_temp_high": 70, "standby": standby, "hvac_action": "idle"}),
             "loft": ("heat", {"target_temp_low": 70, "standby": False, "hvac_action": "idle"}),
             "den": ("heat", {"target_temp_low": 70, "standby": False, "hvac_action": "idle"})}
    return {"sensor.outdoor_unit": ("heat", {}),
            **{f"climate.{key}_head": head for key, head in heads.items()},
            **{f"sensor.{key}_temperature": ("76.0" if key == "studio" else "70.0", {}) for key in heads}}


def test_the_stock_card_warns_while_a_head_waits_in_standby() -> None:
    shown = render(cards.stock_card(), _stock_states(standby=True))
    assert cells(shown)[0] == ["**Studio**", "76.0 °F", "70 °F", "Cool · waiting in standby"]
    assert ('<ha-alert alert-type="warning">Studio waits in standby. Its head is set to cool, '
            "and the outdoor unit is running heat.</ha-alert>") in shown
    assert shown.count("<ha-alert") == 1


def test_the_stock_card_has_no_warning_when_no_head_waits() -> None:
    """Negative: the same heads with Studio's standby off."""
    shown = render(cards.stock_card(), _stock_states(standby=False))
    assert "<ha-alert" not in shown
    assert cells(shown)[0] == ["**Studio**", "76.0 °F", "70 °F", "Cool · idle"]


@pytest.mark.parametrize(("idle_action", "studio"), [
    ("off_after_dry", "Fan only · drying the coil"),
    ("fan_only", "Fan only"),  # negative: MXZ circulates; nothing says the coil is drying
])
def test_the_mxz_card_says_drying_only_under_the_idle_action_that_dries(idle_action: str, studio: str) -> None:
    ids = _mxz_ids()
    states = {"climate.studio_head": ("fan_only", {"fan_mode": "auto"}),
              "climate.loft_head": ("off", {}), "climate.den_head": ("off", {}),
              ids["plan"]: ("cool", {"idle_action": idle_action}), ids["shared_mode"]: ("cool", {}),
              **{ids[f"{slug}_target"]: ("70", {}) for slug in SLUGS},
              **{f"sensor.{key}_temperature": ("70.0", {}) for key in ("studio", "loft", "den")}}
    assert [row[3] for row in cells(render(cards.CARDS["mxz"](ids), states))] == [studio, "Off", "Off"]


def test_a_card_built_without_an_entity_it_needs_fails() -> None:
    """Negative: the comfort card reads the plan, so ids without it cannot build one."""
    ids = {key: value for key, value in _mxz_ids().items() if key != "plan"}
    with pytest.raises(KeyError, match="plan"):
        cards.CARDS["mxz-comfort"](ids)


# --- compose ------------------------------------------------------------------

COLOURS = [(220, 40, 40), (40, 160, 40), (40, 40, 220), (220, 160, 0), (0, 160, 160), (160, 0, 160),
           (120, 120, 120), (250, 250, 250), (0, 0, 0)]


def _run(tmp: Path, feature: str, start_step: str, at: float) -> Path:
    run = tmp / f"run-{feature}"
    run.mkdir()
    steps = [{"action": "before", "time": datetime.fromtimestamp(at - 60, UTC).isoformat()},
             {"action": start_step, "time": datetime.fromtimestamp(at, UTC).isoformat()}]
    (run / "transcript.json").write_text(json.dumps({"feature": feature, "steps": steps}))
    return run


def _frames(tmp: Path, feature: str, first: float, size: tuple[int, int]) -> Path:
    """One shot every 5 s from ``first``, shot ``i`` all in COLOURS[i], in both themes."""
    folder = tmp / f"frames-{feature}"
    for theme in compose.THEMES:
        (folder / theme).mkdir(parents=True)
        for index, colour in enumerate(COLOURS):
            Image.new("RGB", size, colour).save(folder / theme / f"{round((first + 5 * index) * 1000)}.png")
    return folder


def _gif_frames(path: Path) -> list[tuple[Image.Image, int]]:
    shown = []
    with Image.open(path) as gif:
        for index in range(gif.n_frames):
            gif.seek(index)
            shown.append((gif.convert("RGB"), gif.info["duration"]))
    return shown


def _near(colour: tuple[int, ...], expected: tuple[int, int, int]) -> bool:
    return all(abs(a - b) <= 12 for a, b in zip(colour, expected, strict=True))


def test_compose_lines_each_row_up_on_its_own_start_step(tmp_path: Path) -> None:
    """Two rows whose runs started at different times; each frame shows each row's first shot at or
    after its start plus that frame's seconds, and the window, speed and hold set the timing."""
    scene = Scene("two-rows", (Row("demo-stock", "journeys.demo_stock", "stock"),
                               Row("demo-coordinated", "journeys.demo_coordinated", "mxz")), 20, "start", speed=25)
    stock_start, mxz_start = 1_000_000.0, 2_000_000.0
    # Stock's shots are taken 3 s before each 5 s mark, MXZ's exactly on it.
    rows = [(_run(tmp_path, "demo-stock", "start", stock_start), _frames(tmp_path, "demo-stock", stock_start - 3, (40, 20))),
            (_run(tmp_path, "demo-coordinated", "start", mxz_start), _frames(tmp_path, "demo-coordinated", mxz_start, (60, 30)))]
    gifs = compose.compose(scene, tmp_path / "out", rows)
    assert [gif.name for gif in gifs] == ["demo-two-rows-light.gif", "demo-two-rows-dark.gif"]
    shown = _gif_frames(gifs[0])
    # 0, 5, 10, 15 and 20 s into the run: 5 frames, 5 s / 25 = 200 ms each, the last held 3 s.
    assert [duration for _, duration in shown] == [200, 200, 200, 200, 3000]
    width, height = 60 + 2 * compose.MARGIN, compose.BAND + (20 + compose.MARGIN) + (30 + compose.MARGIN)
    for second, (image, _) in enumerate(shown):
        assert image.size == (width, height)
        top = image.getpixel((compose.MARGIN + 5, compose.BAND + 5))
        bottom = image.getpixel((compose.MARGIN + 5, compose.BAND + 20 + compose.MARGIN + 5))
        assert _near(top, COLOURS[second + 1]), (second, top)  # at t, stock's first shot at or after is t + 2
        assert _near(bottom, COLOURS[second]), (second, bottom)  # MXZ's shot at t itself
    with Image.open(tmp_path / "out" / "demo-two-rows-end-light.png") as end:
        assert end.size == (width, height)


def test_compose_runs_past_the_last_shot_on_the_last_one(tmp_path: Path) -> None:
    """One row filmed for less than the window: the frames after the last shot repeat it, and
    only the band's clock moves (wide enough to show it, or Pillow merges equal frames)."""
    scene = Scene("one-row", (Row("fan-hold", "journeys.fan_hold", "mxz-fan"),), 60, "start")
    start = 3_000_000.0
    rows = [(_run(tmp_path, "fan-hold", "start", start), _frames(tmp_path, "fan-hold", start, (600, 20)))]
    shown = _gif_frames(compose.compose(scene, tmp_path / "out", rows)[1])
    assert len(shown) == 13
    assert [_near(image.getpixel((compose.MARGIN + 5, compose.BAND + 5)), COLOURS[-1]) for image, _ in shown[8:]] \
        == [True] * 5


def test_compose_keeps_a_row_at_its_tallest_shot(tmp_path: Path) -> None:
    """A card that grows partway (a warning appears): every frame has one size, and the
    shorter shots sit on the background with the row below in the same place."""
    scene = Scene("grows", (Row("demo-stock", "journeys.demo_stock", "stock"),
                            Row("demo-coordinated", "journeys.demo_coordinated", "mxz")), 10, "start")
    start = 4_000_000.0
    short = _frames(tmp_path, "demo-stock", start, (40, 20))
    for theme in compose.THEMES:  # from 5 s on, the stock card is 30 high
        for shot in sorted((short / theme).glob("*.png"))[1:]:
            Image.new("RGB", (40, 30), COLOURS[2]).save(shot)
    rows = [(_run(tmp_path, "demo-stock", "start", start), short),
            (_run(tmp_path, "demo-coordinated", "start", start), _frames(tmp_path, "demo-coordinated", start, (40, 10)))]
    shown = _gif_frames(compose.compose(scene, tmp_path / "out", rows)[0])
    size = (40 + 2 * compose.MARGIN, compose.BAND + (30 + compose.MARGIN) + (10 + compose.MARGIN))
    assert [image.size for image, _ in shown] == [size] * 3
    first = shown[0][0]
    assert _near(first.getpixel((compose.MARGIN + 5, compose.BAND + 25)), (250, 250, 250))  # padding, light theme
    below = compose.BAND + 30 + compose.MARGIN + 5
    assert all(_near(image.getpixel((compose.MARGIN + 5, below)), COLOURS[i]) for i, (image, _) in enumerate(shown))


def test_compose_refuses_a_run_of_another_feature(tmp_path: Path) -> None:
    """Negative: the fan-hold row given the mode-wait run."""
    scene = SCENES["fan-hold"]
    rows = [(_run(tmp_path, "mode-wait", scene.start_step, 1.0), _frames(tmp_path, "mode-wait", 1.0, (8, 8)))]
    with pytest.raises(compose.WrongRun, match="is a mode-wait run; this row is fan-hold"):
        compose.compose(scene, tmp_path / "out", rows)
    assert not (tmp_path / "out").exists()


def test_compose_refuses_a_row_count_the_scene_does_not_have(tmp_path: Path) -> None:
    """Negative: the two-row comfort scene given one run."""
    scene = SCENES["comfort-priority"]
    rows = [(_run(tmp_path, "comfort-off", scene.start_step, 1.0), _frames(tmp_path, "comfort-off", 1.0, (8, 8)))]
    with pytest.raises(compose.WrongRun, match="has 2 rows; got 1"):
        compose.compose(scene, tmp_path / "out", rows)

