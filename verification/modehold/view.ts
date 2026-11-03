// Labels for reading MXZModeHold's TLC results with the pstack /statechart
// skill (see .claude/skills/verify-mxz-formal/SKILL.md). Words are what a
// person sees: the shared mode, what the heads run, whether the wait holds.
const HOLD = 2; // H in the verification/modehold configs
const NO_TIME = -1000;

const waiting = (v: any) =>
  v.stamp !== NO_TIME && v.real - v.boot - v.stamp < HOLD;

const rooms = (v: any) =>
  Object.keys(v.head)
    .sort()
    .map((k) => {
      const run = v.head[k] || "parked";
      const left = v.latch[k] && v.latch[k] !== v.head[k] ? `, ${v.latch[k]} run paused` : "";
      return `room ${k} ${run}${left}`;
    })
    .join(", ");

export const title = "MXZ shared mode and the mode-change wait";

export const state = (v: any) =>
  `Mode ${v.mode}${waiting(v) ? " (waiting)" : ""}; ${rooms(v)}` +
  (v.vote !== "none" ? `; hands-off head ${v.vote}` : "");

export const group = (v: any) => (v.holdBroken || v.runBroken ? "broken" : v.mode);

export const colors = { cool: "blue", heat: "orange", broken: "pink" };

export const step = (action: string, from: any, to: any) => {
  switch (action) {
    case "Decide":
      return "MXZ decides and commands the heads";
    case "Reading":
      return "a room's temperature changes";
    case "HandsOff":
      return `a person sets the hands-off head to ${to.vote}`;
    case "Tick":
      return "time passes";
    case "ClockStep":
      return "the wall clock is stepped";
    case "Select":
      return `a person picks ${to.mode}`;
    case "Reload":
      return "the entry reloads (an options save)";
    case "Restart":
      return "Home Assistant restarts";
    case "Crash":
      return "Home Assistant dies and starts again";
    case "RestoreDump":
      return "Home Assistant writes its restore data";
    default:
      return action;
  }
};
