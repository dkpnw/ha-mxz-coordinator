"""Stack a demo scene's filmed rows (scenes.py) into one looping image per theme.

    python3 verification/instance/compose.py <scene> <out> <run> <frames> [<run> <frames> ...]

One run and one frames folder per row, top to bottom. A run is a journey's
evidence directory (it holds transcript.json); frames are what film.py wrote.
Each row's clock starts at its journey's start step, and every row advances on
that clock in fixed steps, sped up evenly; no time is cut from one row or one
beat. Each row keeps one height for the whole GIF. The band above the rows says
what this is, the speed, and the time into the run. Needs Pillow. Nothing here reads or changes a state.
"""

from __future__ import annotations

import argparse
import json
from bisect import bisect_left
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from scenes import SCENES, Scene

EVERY = 5.0  # seconds of the run between frames, as filmed
HOLD = 3.0  # seconds the last frame stays
MARGIN = 16
BAND = 56
THEMES = {"light": ("#fafafa", "#212121", "#616161"), "dark": ("#111111", "#e1e1e1", "#9e9e9e")}
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


class WrongRun(ValueError):
    """The runs given do not match the scene's rows."""


def started(run: Path, feature: str, start_step: str) -> float:
    """When the journey's start step was recorded, in epoch seconds.

    Refuses another feature's run, so a row is never filmed from the wrong journey.
    """
    transcript = json.loads((run / "transcript.json").read_text())
    if transcript["feature"] != feature:
        raise WrongRun(f"{run} is a {transcript['feature']} run; this row is {feature}")
    step = next(step for step in transcript["steps"] if step["action"] == start_step)
    return datetime.fromisoformat(step["time"]).timestamp()


def frames(folder: Path) -> list[tuple[float, Path]]:
    """film.py's frames, oldest first, by the epoch millisecond in each name."""
    return sorted((int(path.stem) / 1000, path) for path in folder.glob("*.png"))


def at(shots: list[tuple[float, Path]], moment: float) -> Path:
    """The first frame taken at or after ``moment``; the last one if none was.

    At or after, so the first frame already shows the start step's numbers.
    """
    index = bisect_left([taken for taken, _ in shots], moment)
    return shots[min(index, len(shots) - 1)][1]


def timeline(window: float, every: float = EVERY) -> list[float]:
    """Seconds into the run of each frame, from the start step to the window's end."""
    return [step * every for step in range(int(window // every) + 1)]


def slots(rows: list[list[Path]]) -> tuple[int, list[int]]:
    """The width of every frame and each row's height: the widest and tallest shot shown.

    A card grows when a line appears on it (the stock card's standby warning), so each row
    keeps the height of its tallest shot and every frame of the GIF is the same size.
    """
    width, heights = 0, []
    for shown in rows:
        sizes = []
        for path in set(shown):
            with Image.open(path) as shot:
                sizes.append(shot.size)
        width = max(width, *(w for w, _ in sizes))
        heights.append(max(h for _, h in sizes))
    return width + 2 * MARGIN, heights


def frame(rows: list[Path], seconds: float, theme: str, speed: int, size: tuple[int, list[int]]) -> Image.Image:
    back, text, quiet = THEMES[theme]
    shots = [Image.open(row).convert("RGB") for row in rows]
    width, heights = size
    canvas = Image.new("RGB", (width, BAND + sum(height + MARGIN for height in heights)), back)
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(FONT, 22)
    draw.text((MARGIN, BAND // 2), f"Simulation · invented rooms · sped up {speed}×",
              fill=quiet, font=font, anchor="lm")
    clock = f"{int(seconds) // 60}:{int(seconds) % 60:02d} into the run"
    draw.text((width - MARGIN, BAND // 2), clock, fill=text, font=font, anchor="rm")
    top = BAND
    for shot, height in zip(shots, heights, strict=True):
        canvas.paste(shot, (MARGIN, top))
        top += height + MARGIN
    return canvas


def compose(scene: Scene, out: Path, rows: list[tuple[Path, Path]]) -> list[Path]:
    """Write the scene's GIF and end still per theme; return the GIFs."""
    if len(rows) != len(scene.rows):
        raise WrongRun(f"{scene.name} has {len(scene.rows)} rows; got {len(rows)} run and frames pairs")
    zero = [started(run, row.feature, scene.start_step) for (run, _), row in zip(rows, scene.rows, strict=True)]
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for theme in THEMES:
        shots = [frames(folder / theme) for _, folder in rows]
        shown = [[at(row, start + t) for row, start in zip(shots, zero, strict=True)] for t in timeline(scene.window)]
        size = slots([list(row) for row in zip(*shown, strict=True)])
        images = [frame(paths, t, theme, scene.speed, size) for paths, t in zip(shown, timeline(scene.window), strict=True)]
        durations = [round(1000 * EVERY / scene.speed)] * len(images)
        durations[-1] = round(1000 * HOLD)
        gif = out / f"demo-{scene.name}-{theme}.gif"
        images[0].save(gif, save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True)
        images[-1].save(out / f"demo-{scene.name}-end-{theme}.png", optimize=True)
        written.append(gif)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scene", choices=sorted(SCENES))
    parser.add_argument("out", type=Path)
    parser.add_argument("pairs", type=Path, nargs="+", metavar="run frames")
    args = parser.parse_args()
    if len(args.pairs) % 2:
        parser.error("give a run and a frames folder for each row")
    rows = list(zip(args.pairs[::2], args.pairs[1::2], strict=True))
    for gif in compose(SCENES[args.scene], args.out, rows):
        with Image.open(gif) as image:
            print(f"{gif} {image.n_frames} frames {gif.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
