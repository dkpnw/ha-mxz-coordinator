"""What both arms of the comfort-priority scene share (feature map section 6.5).

Needs setup-and-enable (5.1) to have run. A person wires Den's Comfort priority
to Den's invented occupancy sensor (Configure, Rooms, Den), and the arms differ
only in what that sensor reads. Then Studio reads 76 and wants cool while Den
reads 66 and wants heat: a standoff. With Den unoccupied, Studio wins by room
order (it is first). With Den occupied, Den's Comfort priority wins it. The
loser's head is never sent its mode in the window. Every beat is checked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from expect import commanded_mode, head_calls, watching
from journeys import filmed

if TYPE_CHECKING:
    from journeys.drive import Drive

TARGET = 70
START = {"studio": 76, "loft": 70, "den": 66}
START_STEP = "start: Studio reads 76, Loft 70, Den 66"
# Long enough for the winner's first minutes; shorter than either winner's run, so
# the loser never gets a turn on screen.
WINDOW = 330
OCCUPANCY = "binary_sensor.den_occupancy"
SETTLE = 60


async def run(drive: Drive, occupied: bool) -> None:
    ids = await drive.existing_entry()
    plan = ids["plan"]
    drive.check("Coordinator enable is on", drive.stream.states[ids["coordinator_enable"]]["state"] == "on")
    await drive.configure_room("Configure, Rooms, Den: Comfort priority is Den's occupancy sensor", "den",
                               comfort_priority_entity=OCCUPANCY)
    await drive.until("Den's row reads its Comfort priority from the occupancy sensor", watching(plan)(
        lambda states: states.get(plan, {}).get("state") not in (None, "unavailable")
        and drive.row("Den").get("comfort_priority_entity") == OCCUPANCY), SETTLE)
    reading = "on" if occupied else "off"
    await drive.room(f"Den's occupancy sensor reads {reading}", "den", occupied=occupied)
    await drive.until(f"Den's comfort_priority_status is {reading}", watching(plan)(
        lambda _: drive.row("Den").get("comfort_priority_status") == reading), SETTLE)
    await filmed.model_on(drive)
    await filmed.parked(drive)

    began = await filmed.start(drive, START, START_STEP)
    marked = drive.stream.mark()
    winner, loser = ("den", "studio") if occupied else ("studio", "den")
    mode = {"studio": "cool", "den": "heat"}
    head = {key: f"climate.{key}_head" for key in mode}
    await drive.until(f"a standoff, and {drive.names[winner]} wins it: the shared mode is {mode[winner]} "
                      f"and its head {'heats' if mode[winner] == 'heat' else 'cools'}",
                      watching(plan, head[winner])(lambda states: states[plan]["state"] == mode[winner]
                                                   and states[plan]["attributes"].get("standoff") is True
                                                   and states[head[winner]]["state"] == mode[winner]), SETTLE)
    by_comfort = drive.stream.states[plan]["attributes"].get("mode_by_comfort_priority")
    drive.check(f"the plan says Comfort priority {'chose' if occupied else 'did not choose'} the mode",
                by_comfort is occupied, by_comfort)
    drive.record("MXZ", f"shared mode {mode[winner]}; {drive.names[winner]} runs")
    await filmed.window_ends(drive, began, WINDOW)

    window = drive.stream.since(marked)
    modes = [e["data"]["new_state"]["state"] for e in window
             if e["event_type"] == "state_changed" and e["data"]["entity_id"] == plan]
    drive.check(f"the shared mode stays {mode[winner]} for the whole window",
                set(modes) <= {mode[winner]}, sorted(set(modes)))
    sent = [commanded_mode(e) for e in head_calls(window, [head[loser]], by_person=False)]
    drive.check(f"MXZ never sends {drive.names[loser]}'s head {mode[loser]} in the window",
                mode[loser] not in sent, sent)
    readings = {key: float(drive.stream.states[f"sensor.{key}_temperature"]["state"]) for key in START}
    drive.transcript.notes["end_readings"] = readings
