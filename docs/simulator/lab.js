// The simulator page's shell: loads Pyodide, writes the bundled Python (bundle.js) into its
// file system, and drives world.World. Every decision is made in Python; this file draws the
// rows and passes the controls through.
"use strict";

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v314.0.8/full/";
const HOME = "/home/pyodide/simulator";
const MODES = ["off", "heat", "cool", "fan_only"];
const FANS = ["auto", "quiet", "low", "medium", "middle", "high"];
const ORDINALS = ["1st", "2nd", "3rd"];

const $ = (id) => document.getElementById(id);
const lab = {
  py: null, module: null, world: null, preset: "mixed", speed: 10, playing: true, last: null, drawn: 0,
};

function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${r}` : `${m}:${r}`;
}

// A length of time, as a countdown reads: 9m 45s, or 40s.
function span(seconds) {
  const s = Math.max(0, Math.ceil(seconds));
  return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}

const degrees = (value) => (value === null || value === undefined ? "–" : `${Number(value).toFixed(1)} °F`);
const words = (mode) => (mode === "fan_only" ? "fan only" : mode);
// A head in heat or cool runs to a setpoint; in off or fan only it has none to set.
const runsTo = (mode) => mode === "heat" || mode === "cool";

function fail(error) {
  lab.playing = false;
  $("play").textContent = "Play";
  const status = $("status");
  status.classList.add("error");
  const last = String(error.message || error).split("\n").filter(Boolean).slice(-1)[0];
  status.textContent = `The simulation stopped: ${last}. Press Replay to start it again.`;
  console.error(error);
}

function run(action) {
  try {
    action();
  } catch (error) {
    fail(error);
  }
  draw(true);
}

function element(tag, props = {}, children = []) {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of children) node.append(child);
  return node;
}

function select(options, value, label) {
  const node = element("select");
  node.setAttribute("aria-label", label);
  for (const option of options) node.append(element("option", { value: option, textContent: words(option) }));
  node.value = value;
  return node;
}

// A number input over [low, high]. The room temperatures take 50 to 90 °F; a head's set-to
// takes its own range; MXZ's target number takes the range the row's grid gives it.
function number(value, label, [low, high] = [50, 90]) {
  const node = element("input", { type: "number", min: low, max: high, step: 1, value });
  node.setAttribute("aria-label", label);
  return node;
}

function check(checked, label) {
  const box = element("input", { type: "checkbox", checked });
  return [box, element("label", {}, [box, ` ${label}`])];
}

// A head's set-to: off in off and fan only, with the reason beside it.
function setTo(value, label, range) {
  const input = number(value, label, range);
  const hint = element("span", { className: "hint", textContent: "Set mode to heat or cool first", hidden: true });
  return { input, hint, cell: element("span", { className: "set-to" }, [input, " °F ", hint]) };
}

function allowSetTo({ input, hint }, mode) {
  input.disabled = !runsTo(mode);
  hint.hidden = runsTo(mode);
}

// The setpoint a head runs to in its mode, or the room's target when it runs to none.
const setpoint = (room, fallback) => (room.mode === "heat" ? room.low : room.mode === "cool" ? room.high : fallback);

// What a head is doing, in words, where its mode alone does not say it.
function headLine(room, outdoor) {
  if (room.mode === "off") return "off";
  if (room.mode === "fan_only") return room.drying ? "drying coil (fan only)" : `fan only · fan ${room.fan}`;
  const to = setpoint(room, null);
  const parts = [words(room.mode) + (to === null || to === undefined ? "" : ` to ${Math.round(to)} °F`)];
  if (room.doing === "standby") {
    parts.push(`waiting in standby (outdoor unit running ${outdoor})`);
  } else {
    parts.push(`fan ${room.fan}`);
    if (room.doing === "heating" || room.doing === "cooling" || room.doing === "idle") parts.push(room.doing);
  }
  return parts.join(" · ");
}

// What MXZ's plan says for the room, from the plan's engage and the shared mode.
function planLine(room, mode) {
  if (!room.enable || room.engage === "off") return "Room off";
  if (room.engage === "satisfied") return "At target";
  if (room.engage === mode) return mode === "cool" ? "Cooling" : "Heating";
  if (room.engage === "heat" || room.engage === "cool") return `Wants ${room.engage} · shared mode is ${mode}`;
  return "–";
}

// One command to a head, in words: "Mode → fan only", "Target range → 68–70 °F".
function command(data) {
  const parts = [];
  if (data.hvac_mode !== undefined) parts.push(`mode → ${words(data.hvac_mode)}`);
  if (data.target_temp_low !== undefined && data.target_temp_high !== undefined) {
    parts.push(`target range → ${data.target_temp_low}–${data.target_temp_high} °F`);
  }
  if (data.temperature !== undefined) parts.push(`target → ${data.temperature} °F`);
  if (data.fan_mode !== undefined) parts.push(`fan → ${data.fan_mode}`);
  const text = parts.join(", ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// -- the rooms' cards, built once per start --------------------------------------------------
function buildTemps(view) {
  const temps = $("temps");
  temps.replaceChildren();
  for (const room of view.stock.rooms) {
    const value = number("", `${room.name} temperature`);
    value.placeholder = "°F";
    const set = element("button", { type: "button", textContent: "Set" });
    set.addEventListener("click", () => {
      if (value.value === "") return;
      run(() => lab.world.set_temperature(room.key, Number(value.value)));
      value.value = "";
    });
    temps.append(element("div", { className: "temp-set" }, [element("span", { textContent: room.name }), value, set]));
  }
}

function stockCard(room, view) {
  const mode = select(MODES, room.mode, `${room.name} stock head mode`);
  const to = setTo(Math.round(setpoint(room, 70)), `${room.name} stock head set to`, view.head_range);
  const fan = select(FANS, room.fan, `${room.name} stock head fan`);
  allowSetTo(to, room.mode);
  const send = () => {
    allowSetTo(to, mode.value);
    run(() => lab.world.stock_head(room.key, mode.value, Number(to.input.value), fan.value));
  };
  for (const control of [mode, to.input, fan]) control.addEventListener("change", send);
  return card(room, "stock", element("details", { className: "controls" }, [
    element("summary", { textContent: "Change this head" }),
    element("div", { className: "form" }, ["Mode", mode, "Set to", to.cell, "Fan", fan]),
  ]));
}

function mxzCard(room, view, index) {
  const target = number(room.target, `${room.name} target`, view.mxz.target_bounds);
  const refused = element("p", { className: "refused", role: "alert" });
  target.addEventListener("change", () => run(() => {
    refused.textContent = lab.world.mxz_target(room.key, Number(target.value)) ?? "";
  }));
  const [comfort, comfortLabel] = check(room.comfort, "Comfort priority");
  comfort.addEventListener("change", () => run(() => lab.world.mxz_comfort(room.key, comfort.checked)));
  const [enable, enableLabel] = check(room.enable, "Room on");
  enable.addEventListener("change", () => run(() => lab.world.mxz_enable(room.key, enable.checked)));
  const [auto, autoLabel] = check(!room.fan_held, "Fan auto");
  auto.addEventListener("change", () => run(() => lab.world.mxz_fan_auto(room.key, auto.checked)));
  const mode = select(MODES, room.mode, `${room.name} wall remote mode`);
  mode.addEventListener("change", () => run(() => lab.world.mxz_remote(room.key, mode.value, undefined, undefined)));
  const to = setTo(Math.round(setpoint(room, room.target)), `${room.name} wall remote set to`, view.head_range);
  to.input.addEventListener("change", () => run(() => lab.world.mxz_remote(room.key, undefined, Number(to.input.value), undefined)));
  const fan = select(FANS, room.fan, `${room.name} wall remote fan`);
  fan.addEventListener("change", () => run(() => lab.world.mxz_remote(room.key, undefined, undefined, fan.value)));
  const more = element("details", { className: "controls" }, [
    element("summary", { textContent: "Room switches and wall remote" }),
    element("div", { className: "form" }, [
      element("span", { className: "wide" }, [enableLabel, " ", autoLabel]),
      element("p", { className: "legend", textContent: "Wall remote (overrides at the head)" }),
      "Mode", mode, "Set to", to.cell, "Fan", fan,
    ]),
  ]);
  more.open = view.preset.remote === room.key;
  const node = card(room, "mxz", element("div", {}, [
    element("div", { className: "form" }, ["Target", element("span", {}, [target, " °F ", comfortLabel])]),
    refused,
    more,
  ]), ORDINALS[index] ? `${ORDINALS[index]} in room order` : "");
  node.controls = { target, enable, comfort, auto, mode, to, fan };
  return node;
}

function buildRooms(view) {
  buildTemps(view);
  $("stock-rooms").replaceChildren(...view.stock.rooms.map((room) => stockCard(room, view)));
  $("mxz-rooms").replaceChildren(...view.mxz.rooms.map((room, index) => mxzCard(room, view, index)));
}

function card(room, side, controls, order = "") {
  return element("article", { className: "room", id: `${side}-${room.key}` }, [
    element("h3", {}, [element("span", { textContent: room.name }), element("span", { className: "order", textContent: order }),
      element("span", { className: "badge" })]),
    element("p", { className: "temp" }),
    element("p", { className: "head" }),
    ...(side === "mxz" ? [element("p", { className: "plan" })] : []),
    controls,
  ]);
}

// -- drawing ---------------------------------------------------------------------------------
function keep(input, value) {
  if (document.activeElement !== input && String(input.value) !== String(value)) input.value = value;
}

function draw(now = false) {
  if (!lab.world) return;
  const stamp = performance.now();
  if (!now && stamp - lab.drawn < 100) return;
  lab.drawn = stamp;
  let view;
  try {
    view = JSON.parse(lab.world.view());
  } catch (error) {
    fail(error);
    return;
  }
  const mxz = view.mxz;
  $("time").textContent = clock(view.now);
  $("try").textContent = view.preset.try_this;
  $("stock-outdoor").textContent = view.stock.outdoor ?? "off";
  $("mxz-outdoor").textContent = mxz.outdoor ?? "off";
  $("mxz-mode").textContent = mxz.mode;
  $("mxz-mode").className = `mode-${mxz.mode}`;
  $("mxz-hold").textContent = mxz.hold_left > 0 ? `Mode switch wait: ${span(mxz.hold_left)}` : "Mode switch: ready";
  const why = [];
  if (mxz.standoff) why.push("Standoff: one room wants heat and another wants cool.");
  if (mxz.by_comfort) why.push("Comfort priority decided the shared mode.");
  if (mxz.restarts) why.push(`Home Assistant restarted ${mxz.restarts} time${mxz.restarts === 1 ? "" : "s"}.`);
  $("mxz-why").textContent = why.join(" ");
  $("mxz-quiet").textContent = mxz.quiet_for === null || !mxz.quiet_shown ? ""
    : `Home Assistant just started, so MXZ does not take wall-remote changes for another ${span(mxz.quiet_for)}.`;
  for (const side of ["stock", "mxz"]) {
    for (const room of view[side].rooms) {
      const node = $(`${side}-${room.key}`);
      node.querySelector(".temp").textContent = degrees(room.temperature);
      const head = node.querySelector(".head");
      head.textContent = headLine(room, view[side].outdoor);
      head.className = `head doing-${room.drying ? "drying" : room.doing}`;
      if (side === "mxz") drawMxz(node, room, mxz.mode);
    }
  }
  const calls = mxz.calls.slice().reverse();
  $("mxz-calls-none").hidden = calls.length > 0;
  $("mxz-calls").replaceChildren(...calls.map((call) =>
    element("li", { textContent: `${clock(call.t)} ${call.room}: ${command(call.data)}` })));
}

function drawMxz(node, room, mode) {
  const badges = [];
  if (room.fan_held) badges.push("Manual fan");
  if (room.adopting) badges.push("Taking the remote's change…");
  node.querySelector(".badge").textContent = badges.join(" · ");
  node.querySelector(".plan").textContent = planLine(room, mode);
  const c = node.controls;
  keep(c.target, room.target);
  c.enable.checked = room.enable;
  c.comfort.checked = room.comfort;
  c.auto.checked = !room.fan_held;
  keep(c.mode, room.mode);
  keep(c.fan, room.fan);
  keep(c.to.input, Math.round(setpoint(room, room.target)));
  allowSetTo(c.to, room.mode);
}

// -- the clock -------------------------------------------------------------------------------
function frame(stamp) {
  if (lab.world && lab.playing) {
    if (lab.last !== null) {
      const seconds = Math.min(0.25, (stamp - lab.last) / 1000) * lab.speed;
      try {
        lab.world.advance(lab.world.now + seconds);
      } catch (error) {
        fail(error);
      }
    }
    draw();
  }
  lab.last = stamp;
  requestAnimationFrame(frame);
}

function start(name) {
  run(() => {
    if (lab.world) lab.world.destroy();
    lab.preset = name;
    lab.world = lab.module.World(name);
    for (const button of $("presets").querySelectorAll("button")) {
      button.setAttribute("aria-pressed", String(button.dataset.preset === name));
    }
    const status = $("status");
    status.classList.remove("error");
    status.textContent = "";
    buildRooms(JSON.parse(lab.world.view()));
    lab.playing = true;
    $("play").textContent = "Pause";
  });
}

function wire() {
  const presets = JSON.parse(lab.module.presets());
  const groups = [element("span", { className: "buttons" }), element("span", { className: "buttons more" }, [
    element("span", { className: "label", textContent: "More:" })])];
  for (const preset of presets) {
    const button = element("button", { type: "button", textContent: preset.title });
    button.dataset.preset = preset.name;
    button.addEventListener("click", () => start(preset.name));
    groups[preset.more ? 1 : 0].append(button);
  }
  $("presets").replaceChildren(...groups);
  $("play").addEventListener("click", () => {
    lab.playing = !lab.playing;
    $("play").textContent = lab.playing ? "Pause" : "Play";
  });
  $("replay").addEventListener("click", () => start(lab.preset));
  for (const button of document.querySelectorAll("[data-speed]")) {
    button.addEventListener("click", () => {
      lab.speed = Number(button.dataset.speed);
      for (const other of document.querySelectorAll("[data-speed]")) other.setAttribute("aria-pressed", String(other === button));
    });
  }
  $("skip").addEventListener("click", () => run(() => lab.world.advance(lab.world.now + 60)));
  $("restart").addEventListener("click", () => run(() => lab.world.restart()));
  for (const button of document.querySelectorAll("button:disabled")) button.disabled = false;
}

async function boot() {
  if (typeof loadPyodide !== "function") {
    const status = $("status");
    status.classList.add("error");
    status.textContent = "Could not load the simulator's Python from cdn.jsdelivr.net. Check your "
      + "connection or ad blocker, then reload the page.";
    return;
  }
  try {
    lab.py = await loadPyodide({ indexURL: PYODIDE });
    for (const [path, text] of Object.entries(window.MXZ_SIMULATOR_FILES)) {
      const full = `${HOME}/${path}`;
      lab.py.FS.mkdirTree(full.slice(0, full.lastIndexOf("/")));
      lab.py.FS.writeFile(full, text);
    }
    lab.py.runPython(`import sys; sys.path.insert(0, "${HOME}")`);
    lab.module = lab.py.pyimport("world");
    wire();
    start(lab.preset);
    requestAnimationFrame(frame);
  } catch (error) {
    fail(error);
  }
}

boot();
