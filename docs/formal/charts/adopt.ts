// Labels for reading MXZCoordinator's changes made at a head (WithAdopt) with the
// pstack /statechart skill (docs/formal/STATECHART.md, "User flows"). Words are what a
// person sees: the room's target in Home Assistant, the setpoint the head shows, and
// whether MXZ has a write on its way to the head.
const onTheWay = (v: any) => Object.keys(v.spQueue ?? {}).length > 0;

export const title = "A change made at the head becomes the room's target";

export const state = (v: any) =>
  (v.holding ? "A change at the head is holding" : v.spShown === v.target ? "Head shows the target" : "Head shows another value") +
  (onTheWay(v) ? ", MXZ's write on its way" : "") +
  (v.quiet ? ", starting up" : "");

export const group = (v: any) =>
  v.adopted.length || v.writeInHold ? "broken" : v.holding ? "holding" : v.quiet ? "starting" : "watching";

export const colors = { watching: "blue", holding: "orange", starting: "purple", broken: "pink" };

export const step = (action: string, from: any, to: any) => {
  switch (action) {
    case "SetTarget":
      return "a person sets a new target in Home Assistant";
    case "WriteSp":
      return "MXZ writes the target to the head";
    case "HeadShows":
      return "the head shows MXZ's write";
    case "PersonPress":
      return "a person presses a value at the head";
    case "AdoptFires":
      return to.target !== from.target ? "the hold ends: the room takes the head's value" : "the hold ends: nothing to take";
    case "QuietEnds":
      return "startup quiet ends";
    case "Reload":
      return "the entry reloads";
    case "Restart":
      return "Home Assistant restarts";
    default:
      return action;
  }
};
