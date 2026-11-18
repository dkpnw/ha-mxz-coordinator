"""What both arms of the satisfied scene share (feature map section 6.2).

A cold morning shown twice: Studio, Loft and Den all read 66 °F against 70 °F.
Both arms heat every room to 70, and then the rooms are satisfied. The stock
arm's heads stay set to heat and idle; by the model's rule 1 the outdoor unit
keeps running heat while any head is set to it. MXZ turns every head off.
"""

from __future__ import annotations

TARGET = 70
COLD = 66
START = {"studio": COLD, "loft": COLD, "den": COLD}
# Four degrees at the model's 1 °F a minute, MXZ's own delays, and time to show the rest.
WINDOW = 450
START_STEP = "start: Studio, Loft and Den read 66"
SETTLE = 60
