"""Film one row of the README demo from the instance's own frontend.

    verification/instance/mxz-instance exec -- python3 verification/instance/film.py stock <out>
    verification/instance/mxz-instance exec -- python3 verification/instance/film.py mxz <out>

Runs inside the instance's namespace, through `exec`, with a Python that has
Playwright and its Chromium (installed outside the namespace; no lane has them).
It logs in as the instance's invented owner, saves a one-card dashboard whose
markdown is rendered by Home Assistant from the live states, and screenshots that
card in a light and a dark browser every few seconds until stopped. Each file is
named by the wall-clock millisecond it was taken, so compose.py can line the two
rows up on their journeys' start steps. Nothing here sets a state.
"""

from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path

from home import PORT, Instance
from playwright.sync_api import Page, sync_playwright

URL = f"http://127.0.0.1:{PORT}"
DASHBOARD = "mxz-demo"
WIDTH = 440  # CSS pixels; shot at 2x, so 880 wide, the README's column

HEAD = """{%- macro head(e, fan) -%}
{%- set mode = states(e) -%}{%- set doing = state_attr(e, 'hvac_action') -%}
{%- if mode == 'off' -%}Off
{%- elif mode == 'fan_only' -%}Fan only
{%- elif state_attr(e, 'standby') -%}{{ mode | capitalize }} · waiting
{%- elif doing in ['heating', 'cooling'] -%}{{ doing | capitalize }}{{ ' · fan ' ~ state_attr(e, 'fan_mode') if fan }}
{%- else -%}{{ mode | capitalize }} · idle{%- endif -%}
{%- endmacro -%}
{%- macro reads(key) -%}{{ '%.1f' | format(states('sensor.' ~ key ~ '_temperature') | float(0)) }} °F{%- endmacro -%}
"""

ROOMS = (("studio", "Studio"), ("loft", "Loft"), ("den", "Den"))


def stock_card() -> str:
    """The stock row: each head's own setpoint, and the outdoor unit that decides who waits."""
    lines = [HEAD, "### Stock · outdoor unit: {{ states('sensor.outdoor_unit') }}", "",
             "| Room | Reads | Head set to | Head |", "|:--|--:|--:|:--|"]
    for key, name in ROOMS:
        head = f"climate.{key}_head"
        setpoint = (f"{{%- set m = states('{head}') -%}}{{{{ (state_attr('{head}', 'target_temp_low') if m == 'heat' "
                    f"else state_attr('{head}', 'target_temp_high')) | int(0) if m in ['heat', 'cool'] else '–' }}}}"
                    f"{{{{ ' °F' if m in ['heat', 'cool'] }}}}")
        lines.append(f"| **{name}** | {{{{ reads('{key}') }}}} | {setpoint} | {{{{ head('{head}', false) }}}} |")
    return "\n".join(lines)


def mxz_card(ids: dict[str, str]) -> str:
    """The MXZ row: each room's one target, the shared mode, and Studio's fan while it runs."""
    lines = [HEAD, f"### MXZ Coordinator · shared mode: {{{{ states('{ids['shared_mode']}') }}}}", "",
             "| Room | Reads | Target | Head |", "|:--|--:|--:|:--|"]
    for (key, name), slug in zip(ROOMS, ("primary", "secondary", "zone_3"), strict=True):
        target = f"{{{{ states('{ids[slug + '_target']}') | int(0) }}}} °F"
        lines.append(f"| **{name}** | {{{{ reads('{key}') }}}} | {target} | {{{{ head('climate.{key}_head', true) }}}} |")
    return "\n".join(lines)


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
    parser.add_argument("row", choices=("stock", "mxz"))
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
        save_dashboard(first, stock_card() if args.row == "stock" else mxz_card(mxz_ids(first)))
        for theme, page in pages.items():
            (args.out / theme).mkdir(parents=True, exist_ok=True)
            page.goto(f"{URL}/{DASHBOARD}/row")
            page.wait_for_selector("hui-markdown-card ha-markdown table", timeout=60_000)
        print(f"FILMING {args.row} every {args.every:g} s into {args.out}", flush=True)
        while not stop:
            shot = time.time()
            for theme, page in pages.items():
                page.locator("hui-markdown-card").screenshot(path=args.out / theme / f"{int(shot * 1000)}.png")
            time.sleep(max(0.0, shot + args.every - time.time()))
        browser.close()
    print(f"STOPPED {args.row}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
