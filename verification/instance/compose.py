"""Stack the two filmed rows of the README demo into one looping image per theme.

    python3 verification/instance/compose.py <stock run> <stock frames> <mxz run> <mxz frames> <out>

A run is a journey's evidence directory (it holds transcript.json); frames are
what film.py wrote. Each row's clock starts at its journey's start step, and both
rows advance on that clock in fixed steps, sped up evenly; no time is cut from
one row or one beat. The band above the rows says what this is, the speed, and
the time into the run. Needs Pillow. Nothing here reads or changes a state.
"""

from __future__ import annotations

import argparse
import json
from bisect import bisect_left
from datetime import datetime
from pathlib import Path

from journeys.demo import START_STEP, WINDOW
from PIL import Image, ImageDraw, ImageFont

EVERY = 5.0  # seconds of the run between frames, as filmed
SPEED = 25  # each frame shows for EVERY / SPEED seconds
HOLD = 3.0  # seconds the last frame stays
MARGIN = 16
BAND = 56
THEMES = {"light": ("#fafafa", "#212121", "#616161"), "dark": ("#111111", "#e1e1e1", "#9e9e9e")}
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def started(run: Path) -> float:
    """When the journey's start step was recorded, in epoch seconds."""
    steps = json.loads((run / "transcript.json").read_text())["steps"]
    step = next(step for step in steps if step["action"] == START_STEP)
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


def timeline(window: float = WINDOW, every: float = EVERY) -> list[float]:
    """Seconds into the run of each frame, from the start step to the window's end."""
    return [step * every for step in range(int(window // every) + 1)]


def frame(stock: Path, mxz: Path, seconds: float, theme: str) -> Image.Image:
    back, text, quiet = THEMES[theme]
    top, bottom = Image.open(stock).convert("RGB"), Image.open(mxz).convert("RGB")
    width = max(top.width, bottom.width) + 2 * MARGIN
    canvas = Image.new("RGB", (width, BAND + top.height + MARGIN + bottom.height + MARGIN), back)
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(FONT, 22)
    draw.text((MARGIN, BAND // 2), f"Simulation · invented heads and rooms · {SPEED}× speed",
              fill=quiet, font=font, anchor="lm")
    clock = f"{int(seconds) // 60}:{int(seconds) % 60:02d} into the run"
    draw.text((width - MARGIN, BAND // 2), clock, fill=text, font=font, anchor="rm")
    canvas.paste(top, (MARGIN, BAND))
    canvas.paste(bottom, (MARGIN, BAND + top.height + MARGIN))
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("stock_run", "stock_frames", "mxz_run", "mxz_frames", "out"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    zero = {"stock": started(args.stock_run), "mxz": started(args.mxz_run)}
    for theme in THEMES:
        shots = {"stock": frames(args.stock_frames / theme), "mxz": frames(args.mxz_frames / theme)}
        images = [frame(at(shots["stock"], zero["stock"] + t), at(shots["mxz"], zero["mxz"] + t), t, theme)
                  for t in timeline()]
        durations = [round(1000 * EVERY / SPEED)] * len(images)
        durations[-1] = round(1000 * HOLD)
        gif = args.out / f"demo-stock-vs-mxz-{theme}.gif"
        images[0].save(gif, save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True)
        images[-1].save(args.out / f"demo-stock-vs-mxz-end-{theme}.png", optimize=True)
        print(f"{gif} {len(images)} frames {gif.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
