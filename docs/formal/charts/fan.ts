// Labels for reading MXZCoordinator's fan runs with the pstack /statechart skill.
// Words are what a person sees: the speed on the head, and whether Fan auto is on.
const SPEED: Record<string, string> = { auto: "auto", r0: "low", r1: "medium", r2: "high" };

const fanAuto = (v: any) => (v.fanRestore !== "none" ? v.fanRestore === "free" : !v.fanLatched);

export const title = "A person's fan pick and Fan auto";

export const state = (v: any) => {
  if (v.fanCmd === "none" && v.fanRestore === "none") return "No fan written yet";
  const shown = SPEED[v.fanShown];
  if (v.fanRestore !== "none") return `Just started, ${fanAuto(v) ? "Fan auto on" : "held"}, head shows ${shown}`;
  if (fanAuto(v)) return `Fan auto on: MXZ drives the fan, head shows ${shown}`;
  return v.fanShown === "auto" ? "Fan auto off, head shows auto: the hold ends at MXZ's next update" : `Held at ${shown}: Fan auto off`;
};

export const group = (v: any) =>
  v.fanOverwritten || v.falseHold ? "broken" : v.fanRestore !== "none" ? "starting" : fanAuto(v) ? "driving" : "held";

export const colors = { driving: "blue", held: "orange", starting: "purple", broken: "pink" };

export const step = (action: string, from: any, to: any) => {
  switch (action) {
    case "Decide":
      if (to.fanLatched && !from.fanLatched) return "MXZ's update reads a speed it didn't write: held";
      if (!to.fanLatched && from.fanLatched) return "MXZ's update reads auto: the hold ends";
      return to.fanShown !== from.fanShown ? `MXZ writes ${SPEED[to.fanShown]}` : "MXZ's update takes the speed as its own";
    case "PersonFan":
      return to.fanShown === "auto" ? "a person picks auto at the remote" : `a person picks ${SPEED[to.fanShown]} at the remote`;
    case "FanAutoOnGesture":
      return "a person turns Fan auto on";
    case "FanAutoOffGesture":
      return "a person turns Fan auto off";
    case "HeadReports":
      return "the head reports MXZ's last speed";
    case "Restart":
      return to.fanShown === from.fanShown ? "Home Assistant restarts"
        : `Home Assistant restarts, the fan moved to ${SPEED[to.fanShown]} while it was down`;
    case "Crash":
      return "Home Assistant dies and starts again";
    case "Dump":
      return "Home Assistant writes its restore data";
    case "Reading":
      return "the room's temperature changes";
    default:
      return action;
  }
};
