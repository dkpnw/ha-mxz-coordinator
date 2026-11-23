// The simulator page's shell: loads Pyodide, writes the bundled Python (bundle.js) into its
// file system, and drives world.World. Every decision is made in Python; this file draws the
// rows and passes the one set of controls through to both.
"use strict";

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v314.0.8/full/";
const HOME = "/home/pyodide/simulator";
const MODES = ["off", "heat", "cool"];
const FANS = ["auto", "quiet", "low", "medium", "middle", "high"];
const ORDINALS = ["1st", "2nd", "3rd"];

const $ = (id) => document.getElementById(id);
// On window so a headless page check can read lab.world.view().
const lab = window.lab = {
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

function number(value, label, [low, high]) {
  const node = element("input", { type: "number", min: low, max: high, step: 1, value });
  node.setAttribute("aria-label", label);
  return node;
}

// The setpoint a head runs to in its mode; none in off or fan only.
const setpoint = (room) => (room.mode === "heat" ? room.low : room.mode === "cool" ? room.high : null);

// What a head is doing, in words, where its mode alone does not say it.
function headLine(room, outdoor) {
  if (room.mode === "off") return "off";
  if (room.mode === "fan_only") return room.drying ? "drying coil (fan only)" : `fan only · fan ${room.fan}`;
  const to = setpoint(room);
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

// -- the shared controls and the rooms' cards, built once per start ---------------------------
// One line per room. Each control calls one World method, which changes both rows. The
// controls start at what the scenario set for each room: the stock heads, which nothing but
// these controls changes.
function controlLine(room, bounds, refused) {
  const name = room.name;
  const temp = number("", `${name} room temperature`, [50, 90]);
  temp.placeholder = "°F";
  const setTemp = () => {
    if (temp.value === "") return;
    run(() => lab.world.set_temperature(room.key, Number(temp.value)));
    temp.value = "";
  };
  temp.addEventListener("keydown", (event) => { if (event.key === "Enter") setTemp(); });
  const set = element("button", { type: "button", textContent: "Set" });
  set.addEventListener("click", setTemp);
  const mode = select(MODES, room.mode, `${name} mode`);
  // A stock head holds the set-to as both its setpoints, in every mode.
  const to = number(Math.round(room.low), `${name} set to`, bounds);
  to.disabled = !runsTo(room.mode);
  const fan = select(FANS, room.fan, `${name} fan`);
  mode.addEventListener("change", () => {
    to.disabled = !runsTo(mode.value);
    run(() => lab.world.set_mode(room.key, mode.value));
  });
  let taken = to.value;
  to.addEventListener("change", () => run(() => {
    refused.textContent = lab.world.set_to(room.key, Number(to.value)) ?? "";
    if (refused.textContent) to.value = taken;
    else taken = to.value;
  }));
  fan.addEventListener("change", () => run(() => lab.world.set_fan(room.key, fan.value)));
  const cell = (...children) => element("td", {}, children);
  return element("tr", {}, [
    element("th", { scope: "row", textContent: name }),
    cell(temp, " ", set), cell(mode), cell(to, " °F"), cell(fan),
  ]);
}

function buildControls(view) {
  const refused = $("refused");
  refused.textContent = "";
  $("controls").replaceChildren(...view.stock.rooms.map((room) => controlLine(room, view.mxz.target_bounds, refused)));
}

function card(room, side, order = "") {
  return element("article", { className: "room", id: `${side}-${room.key}` }, [
    element("h3", {}, [element("span", { textContent: room.name }), element("span", { className: "order", textContent: order }),
      element("span", { className: "badge" })]),
    element("p", { className: "temp" }),
    element("p", { className: "head" }),
    ...(side === "mxz" ? [element("p", { className: "plan" })] : []),
  ]);
}

function build(view) {
  buildControls(view);
  $("stock-rooms").replaceChildren(...view.stock.rooms.map((room) => card(room, "stock")));
  $("mxz-rooms").replaceChildren(...view.mxz.rooms.map((room, index) =>
    card(room, "mxz", ORDINALS[index] ? `${ORDINALS[index]} in room order` : "")));
}

// -- drawing ---------------------------------------------------------------------------------
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
  if (mxz.standoff) why.push("One room wants heat and another wants cool.");
  if (mxz.restarts) why.push(`Home Assistant restarted ${mxz.restarts === 1 ? "once" : `${mxz.restarts} times`}.`);
  $("mxz-why").textContent = why.join(" ");
  for (const side of ["stock", "mxz"]) {
    for (const room of view[side].rooms) {
      const node = $(`${side}-${room.key}`);
      node.querySelector(".temp").textContent = degrees(room.temperature);
      const head = node.querySelector(".head");
      head.textContent = headLine(room, view[side].outdoor);
      head.className = `head doing-${room.drying ? "drying" : room.doing}`;
      if (side === "mxz") {
        const badges = [];
        if (room.fan_held) badges.push("Manual fan");
        if (room.adopting) badges.push("Taking the remote's change…");
        node.querySelector(".badge").textContent = badges.join(" · ");
        node.querySelector(".plan").textContent = planLine(room, mxz.mode);
      }
    }
  }
  const calls = mxz.calls.slice().reverse();
  $("mxz-calls-none").hidden = calls.length > 0;
  $("mxz-calls").replaceChildren(...calls.map((call) =>
    element("li", { textContent: `${clock(call.t)} ${call.room}: ${command(call.data)}` })));
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
    build(JSON.parse(lab.world.view()));
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
