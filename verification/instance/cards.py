"""The one markdown card each filmed row shows, by name (scenes.py names it per row).

Home Assistant renders every value on a card from live states and attributes:
the equipment's (`invented_house`) and MXZ's own entities. Nothing here
computes a result. Stdlib only: film.py saves the card, and the suite reads it.
"""

from __future__ import annotations

from collections.abc import Callable

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
SLUGS = ("primary", "secondary", "zone_3")  # MXZ's room keys, in the same order
OUTDOOR = "outdoor unit: {{ states('sensor.outdoor_unit') }}"

# A column after Head: its title, and its cell from MXZ's entity ids, the room's key, slug and name.
Column = tuple[str, Callable[[dict[str, str], str, str, str], str]]


def stock_card(ids: dict[str, str] | None = None) -> str:
    """The stock row: each head's own setpoint, and the outdoor unit that decides who waits."""
    lines = [HEAD, f"### Stock · {OUTDOOR}", "",
             "| Room | Reads | Head set to | Head |", "|:--|--:|--:|:--|"]
    for key, name in ROOMS:
        head = f"climate.{key}_head"
        setpoint = (f"{{%- set m = states('{head}') -%}}{{{{ (state_attr('{head}', 'target_temp_low') if m == 'heat' "
                    f"else state_attr('{head}', 'target_temp_high')) | int(0) if m in ['heat', 'cool'] else '–' }}}}"
                    f"{{{{ ' °F' if m in ['heat', 'cool'] }}}}")
        lines.append(f"| **{name}** | {{{{ reads('{key}') }}}} | {setpoint} | {{{{ head('{head}', false) }}}} |")
    return "\n".join(lines)


def mxz_card(ids: dict[str, str], header: tuple[str, ...] = (), column: Column | None = None) -> str:
    """The MXZ row: each room's one target, the shared mode, and a running head's fan."""
    title = f"### MXZ Coordinator · shared mode: {{{{ states('{ids['shared_mode']}') }}}}"
    lines = [HEAD, " · ".join((title, *header)), "",
             "| Room | Reads | Target | Head |" + (f" {column[0]} |" if column else ""),
             "|:--|--:|--:|:--|" + (":--|" if column else "")]
    for (key, name), slug in zip(ROOMS, SLUGS, strict=True):
        target = f"{{{{ states('{ids[slug + '_target']}') | int(0) }}}} °F"
        cell = f" {column[1](ids, key, slug, name)} |" if column else ""
        lines.append(f"| **{name}** | {{{{ reads('{key}') }}}} | {target} | {{{{ head('climate.{key}_head', true) }}}} |{cell}")
    return "\n".join(lines)


def mode_wait(ids: dict[str, str]) -> str:
    """The mode-change hold as MXZ's room thermostat publishes it (the same on every room)."""
    thermostat = ids["primary_thermostat"]
    return (f"{{%- set left = state_attr('{thermostat}', 'mode_change_hold_remaining') | int(0) -%}}"
            "{{ 'can switch in %d:%02d' | format(left // 60, left % 60) if left > 0 else 'can switch now' }}")


def fan_auto(ids: dict[str, str], key: str, slug: str, name: str) -> str:
    return f"{{{{ states('{ids[slug + '_fan_auto']}') | capitalize }}}}"


def comfort_priority(ids: dict[str, str]) -> str:
    """Each room with a Comfort priority entity, and its comfort_priority_status from MXZ's plan row."""
    return (f"comfort priority:{{%- for row in state_attr('{ids['plan']}', 'zones') or [] "
            "if row.comfort_priority_status != 'not_set' %} {{ row.name }} {{ row.comfort_priority_status }}"
            "{%- endfor %}")


CARDS: dict[str, Callable[[dict[str, str]], str]] = {
    "stock": stock_card,
    "mxz": mxz_card,
    "mxz-outdoor": lambda ids: mxz_card(ids, header=(OUTDOOR,)),
    "mxz-fan": lambda ids: mxz_card(ids, column=("Fan auto", fan_auto)),
    "mxz-wait": lambda ids: mxz_card(ids, header=(mode_wait(ids),)),
    "mxz-comfort": lambda ids: mxz_card(ids, header=(comfort_priority(ids),)),
}
