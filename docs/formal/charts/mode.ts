// Labels for reading MXZCoordinator's shared mode and its hold with the pstack
// /statechart skill (docs/formal/STATECHART.md, "User flows"). Words are what a person
// sees: the shared-mode select, whether a change is on hold, and what the room asks for.
const NO_TIME = -1000;
const H = 1; // H in docs/formal/charts/mode.cfg

const held = (v: any) => v.stamp !== NO_TIME && v.real - v.stamp < H;
const call = (v: any) => (v.pos[1] > 0 ? "cool" : "heat");

export const title = "The shared mode changes, then holds";

export const state = (v: any) =>
  `Mode ${v.mode}${held(v) ? " on hold" : ""}, the room wants ${call(v)}`;

export const group = (v: any) =>
  v.flipInDwell || v.flipLate || v.restoreBroken || v.trailing ? "broken" : v.mode;

export const colors = { cool: "blue", heat: "orange", broken: "pink" };

export const step = (action: string, from: any, to: any) => {
  switch (action) {
    case "Decide":
      return to.mode !== from.mode ? `MXZ changes the mode to ${to.mode} and records it` : "MXZ's update keeps the mode";
    case "Reading":
      return `the room starts calling for ${call(to)}`;
    case "Tick":
      return "time passes";
    case "Select":
      return `a person picks ${to.mode}`;
    case "Restart":
      return "Home Assistant restarts";
    case "Reload":
      return "the entry reloads";
    default:
      return action;
  }
};
