------------------------------- MODULE Charts -------------------------------
(***************************************************************************)
(* The user-flow charts in docs/formal/STATECHART.md, drawn by the pstack  *)
(* /statechart skill from MXZCoordinator itself. Each chart's config here  *)
(* cuts the model down to a few hundred states so the whole graph can be   *)
(* drawn; these definitions are the cuts a config can't write. They add no *)
(* step and change no rule. Copy this file, ../tla/MXZCoordinator.tla and  *)
(* the chart's .cfg and .ts into one scratch folder outside the repository *)
(* to run it.                                                              *)
(***************************************************************************)
EXTENDS MXZCoordinator

\* mode.cfg: a room either calls for cool or calls for heat.
Calls == {-3, 3}

\* fan.cfg: a person picks medium, high or auto, never low. Room 1 always reads
\* hot, so MXZ only ever writes medium; low would be a second speed MXZ never writes.
NoLow == fanShown # "r0"

\* adopt.cfg, adopt-restart.cfg: a person presses at the head only values they
\* could also set in Home Assistant.
TwoValues == spShown \in Targets

\* window.cfg: a person at the head turns it to cool or off. Fan only or heat on a
\* paused head is the same takeover as cool.
CoolOrOff == head[1] \in {"off", "cool"}
=============================================================================
