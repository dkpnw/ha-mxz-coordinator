// Labels for reading MXZCoordinator's door/window runs with the pstack /statechart
// skill (docs/formal/STATECHART.md, "User flows"). Words are what a person sees in
// the room: the window, the head, the Window Override switch.
const PHASE: Record<string, string> = {
  closed: "Window closed, room runs",
  opening: "Window open, room runs until the open delay passes",
  paused: "Paused: head off",
  closing: "Window closed, head off until the close delay passes",
  overridden: "Overridden: room runs with the window open",
};
const PAUSED = ["paused", "closing"];

export const title = "A door or window pauses its room";

// A takeover still waiting for MXZ's update shows only on a paused head, where a
// person sees it: elsewhere the room runs either way until that update.
export const state = (v: any) =>
  PHASE[v.winState] + (PAUSED.includes(v.winState) && v.winOverride !== "none" ? ", taken over" : "");

export const group = (v: any) =>
  v.pauseBroken || v.overrideBroken || v.restoreBroken ? "broken"
    : PAUSED.includes(v.winState) ? "paused"
    : v.winState === "overridden" ? "overridden" : "running";

export const colors = { running: "blue", paused: "purple", overridden: "orange", broken: "pink" };

// What MXZ's update does, from what it does to the window's state.
const update = (from: any, to: any) => {
  const a = from.winState, b = to.winState;
  if (a === b) return from.winOverride !== to.winOverride ? "MXZ's update ends the takeover: the window reads closed" : "MXZ's update";
  if (b === "paused" && a === "overridden") return "Window Override is off: MXZ's update counts the window again";
  if (b === "opening" && a === "overridden") return "Window Override is off: MXZ's update counts the window again";
  if (b === "opening") return "MXZ's update sees the window open";
  if (b === "paused" && a === "closing") return "MXZ's update sees it open again";
  if (b === "paused") return "the open delay has passed: MXZ turns the head off";
  if (b === "closing") return "MXZ's update sees the window closed";
  if (b === "closed" && PAUSED.includes(a)) return "the close delay has passed: the room runs again";
  if (b === "closed" && a === "overridden") return "the window reads closed: the override ends";
  if (b === "closed") return "MXZ's update sees it closed before the open delay";
  return "MXZ's update hands the room over to the person";
};

export const step = (action: string, from: any, to: any) => {
  switch (action) {
    case "Decide":
      return update(from, to);
    case "WindowReads":
      return to.winRead === "none" ? "the sensor goes unavailable" : `the window reads ${to.winRead}`;
    case "Tick":
      return "time passes";
    case "PersonMode":
      return to.head[1] === "off" ? "a person turns the head off at its remote" : "a person takes the room over";
    case "OverrideSwitch":
      return to.winOverride === "switch" ? "a person takes the room over" : "a person turns Window Override off";
    case "Reload":
      return "the entry reloads";
    case "Restart":
      return "Home Assistant restarts";
    default:
      return action;
  }
};
