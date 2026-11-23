"""Film one row of a demo scene (scenes.py) from the instance's own frontend.

    verification/instance/mxz-instance exec -- python3 verification/instance/film.py demo-stock <out>
    verification/instance/mxz-instance exec -- python3 verification/instance/film.py demo-coordinated <out>

The row is named by its journey's feature; its card (cards.py) is the one the
scene gives it. Runs inside the instance's namespace, through `exec`, with a
Python that has Playwright and its Chromium (installed outside the namespace; no
lane has them). It logs in as the instance's invented owner, saves a one-card
dashboard whose markdown is rendered by Home Assistant from the live states, and
screenshots that card in a light and a dark browser every few seconds until
stopped. Each file is named by the wall-clock millisecond it was taken, so
compose.py can line the rows up on their journeys' start steps. Nothing here
sets a state.
"""

from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path

from cards import CARDS
from home import PORT, Instance
from playwright.sync_api import Page, sync_playwright
from scenes import ROWS

URL = f"http://127.0.0.1:{PORT}"
DASHBOARD = "mxz-demo"
WIDTH = 440  # CSS pixels; shot at 2x, so 880 wide, the README's column


def login(page: Page, auth: dict[str, str]) -> None:
    page.goto(URL + "/")
    page.wait_for_selector("input[name=username]", timeout=60_000)
    page.fill("input[name=username]", auth["username"])
    page.fill("input[name=password]", auth["password"])
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.querySelector('home-assistant')?.hass?.connected", timeout=60_000)


def call_ws(page: Page, message: dict) -> object:
    return page.evaluate("m => document.querySelector('home-assistant').hass.callWS(m)", message)


def mxz_ids(page: Page) -> dict[str, str]:
    """MXZ's entity ids by unique-id key, as the journeys find them."""
    entries = call_ws(page, {"type": "config_entries/get", "domain": "mxz_coordinator"})
    if len(entries) != 1:
        raise SystemExit(f"film mxz needs one MXZ entry; found {len(entries)}")
    prefix = entries[0]["entry_id"] + "_"
    registry = call_ws(page, {"type": "config/entity_registry/list"})
    return {e["unique_id"].removeprefix(prefix): e["entity_id"] for e in registry
            if e["platform"] == "mxz_coordinator" and e["unique_id"].startswith(prefix)}


def save_dashboard(page: Page, content: str) -> None:
    dashboards = call_ws(page, {"type": "lovelace/dashboards/list"})
    if not any(d["url_path"] == DASHBOARD for d in dashboards):
        call_ws(page, {"type": "lovelace/dashboards/create", "url_path": DASHBOARD, "title": "Demo",
                       "mode": "storage", "show_in_sidebar": False, "require_admin": False})
    call_ws(page, {"type": "lovelace/config/save", "url_path": DASHBOARD, "config": {
        "views": [{"title": "Demo", "path": "row", "cards": [{"type": "markdown", "content": content}]}],
    }})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("feature", choices=sorted(ROWS), help="the row's demo journey")
    parser.add_argument("out", type=Path)
    parser.add_argument("--name", default="demo", help="instance name (default demo)")
    parser.add_argument("--every", type=float, default=5.0, help="seconds between shots (default 5)")
    args = parser.parse_args()
    auth = json.loads(Instance(args.name).auth_file.read_text())
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
    signal.signal(signal.SIGINT, lambda *_: stop.append(True))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        pages = {}
        for theme in ("light", "dark"):
            context = browser.new_context(viewport={"width": WIDTH, "height": 400}, device_scale_factor=2,
                                          color_scheme=theme)
            pages[theme] = context.new_page()
            login(pages[theme], auth)
        first = pages["light"]
        card = ROWS[args.feature].card
        save_dashboard(first, CARDS[card]({} if card == "stock" else mxz_ids(first)))
        for theme, page in pages.items():
            (args.out / theme).mkdir(parents=True, exist_ok=True)
            page.goto(f"{URL}/{DASHBOARD}/row")
            page.wait_for_selector("hui-markdown-card ha-markdown table", timeout=60_000)
            # The frontend's launch screen fades out over the card after it renders.
            page.wait_for_selector("#ha-launch-screen", state="detached", timeout=60_000)
        print(f"FILMING {args.feature} every {args.every:g} s into {args.out}", flush=True)
        while not stop:
            shot = time.time()
            for theme, page in pages.items():
                page.locator("hui-markdown-card").screenshot(path=args.out / theme / f"{int(shot * 1000)}.png")
            time.sleep(max(0.0, shot + args.every - time.time()))
        browser.close()
    print(f"STOPPED {args.feature}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
