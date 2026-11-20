// The simulator page's shell: loads Pyodide, writes the bundled Python (bundle.js) into its
// file system, and drives world.World. Every decision is made in Python; this file draws the
// rows and passes the controls through.
"use strict";

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v314.0.8/full/";
const HOME = "/home/pyodide/simulator";
const MODES = ["off", "heat", "cool", "fan_only"];
const FANS = ["auto", "quiet", "low", "medium", "middle", "high"];
const PRESETS = [
  ["mixed", "One room wants cool"],
  ["satisfied", "Every room at target"],
  ["standoff", "Standoff and Comfort priority"],
  ["mode_wait", "The wait between heat and cool"],
  ["fan_hold", "A fan speed you pick"],
  ["restart", "Restart"],
];

const $ = (id) => document.getElementById(id);
const lab = { py: null, module: null, world: null, speed: 25, playing: true, last: null, drawn: 0, view: null };

function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${r}` : `${m}:${r}`;
}

const degrees = (value) => (value === null || value === undefined ? "–" : `${Number(value).toFixed(1)} °F`);
const words = (mode) => (mode === "fan_only" ? "fan only" : mode);

function fail(error) {
  lab.playing = false;
  const status = $("status");
  status.classList.add("error");
  status.textContent = `The simulation stopped: ${String(error.message || error).split("\n").filter(Boolean).slice(-1)[0]}`;
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

function number(value, label) {
  const node = element("input", { type: "number", min: 50, max: 90, step: 1, value });
  node.setAttribute("aria-label", label);
  return node;
}

function check(checked, label) {
  const box = element("input", { type: "checkbox", checked });
  return [box, element("label", {}, [box, ` ${label}`])];
}

// What a head is doing, where its mode alone does not say it.
const DOING = { heating: "heating", cooling: "cooling", idle: "idle", standby: "waiting in standby" };

function headLine(room) {
  const to = room.mode === "heat" ? room.low : room.mode === "cool" ? room.high : null;
  const parts = [words(room.mode) + (to === null || to === undefined ? "" : ` to ${Math.round(to)} °F`), `fan ${room.fan}`];
  if (DOING[room.doing]) parts.push(DOING[room.doing]);
  return parts.join(" · ");
}

// -- the rooms' cards, built once per start --------------------------------------------------
function buildRooms(view) {
  const temps = $("temps");
  temps.replaceChildren();
  for (const room of view.stock.rooms) {
    const value = number(Math.round(room.temperature), `${room.name} temperature`);
    const set = element("button", { type: "button", textContent: "Set" });
    set.addEventListener("click", () => run(() => lab.world.set_temperature(room.key, Number(value.value))));
    temps.append(element("div", { className: "temp-set" }, [element("span", { textContent: room.name }), value, set]));
  }

  const stock = $("stock-rooms");
  stock.replaceChildren();
  for (const room of view.stock.rooms) {
    const mode = select(MODES, room.mode, `${room.name} stock head mode`);
    const to = number(Math.round(room.mode === "cool" ? room.high ?? 70 : room.low ?? 70), `${room.name} stock head set to`);
    const fan = select(FANS, room.fan, `${room.name} stock head fan`);
    const send = () => run(() => lab.world.stock_head(room.key, mode.value, Number(to.value), fan.value));
    mode.addEventListener("change", send);
    fan.addEventListener("change", send);
    const apply = element("button", { type: "button", className: "wide", textContent: "Set the head" });
    apply.addEventListener("click", send);
    stock.append(card(room, "stock", element("form", {}, [
      element("fieldset", {}, [element("legend", { textContent: "A person at the head" }),
        "Mode", mode, "Set to", to, "Fan", fan, apply]),
    ])));
  }

  const mxz = $("mxz-rooms");
  mxz.replaceChildren();
  for (const room of view.mxz.rooms) {
    const target = number(room.target, `${room.name} target`);
    const setTarget = element("button", { type: "button", textContent: "Set" });
    setTarget.addEventListener("click", () => run(() => lab.world.mxz_target(room.key, Number(target.value))));
    const [enable, enableLabel] = check(room.enable, "Room on");
    enable.addEventListener("change", () => run(() => lab.world.mxz_enable(room.key, enable.checked)));
    const [comfort, comfortLabel] = check(room.comfort, "Comfort priority");
    comfort.addEventListener("change", () => run(() => lab.world.mxz_comfort(room.key, comfort.checked)));
    const [auto, autoLabel] = check(!room.fan_held, "Fan auto");
    auto.addEventListener("change", () => run(() => lab.world.mxz_fan_auto(room.key, auto.checked)));
    const mode = select(MODES, room.mode, `${room.name} remote mode`);
    mode.addEventListener("change", () => run(() => lab.world.mxz_remote(room.key, mode.value, undefined, undefined)));
    const to = number(70, `${room.name} remote set to`);
    const press = element("button", { type: "button", textContent: "Press" });
    press.addEventListener("click", () => run(() => lab.world.mxz_remote(room.key, undefined, Number(to.value), undefined)));
    const fan = select(FANS, room.fan, `${room.name} remote fan`);
    fan.addEventListener("change", () => run(() => lab.world.mxz_remote(room.key, undefined, undefined, fan.value)));
    const node = card(room, "mxz", element("form", {}, [
      element("fieldset", {}, [element("legend", { textContent: "In Home Assistant" }),
        "Target", element("span", {}, [target, " ", setTarget]),
        element("span", { className: "wide" }, [enableLabel, " ", comfortLabel, " ", autoLabel])]),
      element("fieldset", {}, [element("legend", { textContent: "A person at the head's remote" }),
        "Mode", mode, "Set to", element("span", {}, [to, " ", press]), "Fan", fan]),
    ]));
    node.controls = { target, enable, comfort, auto, mode, fan };
    mxz.append(node);
  }
}

function card(room, side, form) {
  const node = element("article", { className: "room", id: `${side}-${room.key}` }, [
    element("h3", {}, [element("span", { textContent: room.name }), element("span", { className: "badge" })]),
    element("p", { className: "temp" }),
    element("p", { className: "head" }),
    ...(side === "mxz" ? [element("p", { className: "plan" })] : []),
    form,
  ]);
  return node;
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
  lab.view = view;
  $("time").textContent = clock(view.now);
  $("try").textContent = view.preset.try_this;
  $("stock-outdoor").textContent = view.stock.outdoor ?? "none";
  $("mxz-outdoor").textContent = view.mxz.outdoor ?? "none";
  $("mxz-mode").textContent = view.mxz.mode;
  $("mxz-mode").className = `mode-${view.mxz.mode}`;
  $("mxz-hold").textContent = view.mxz.hold_left > 0 ? clock(Math.ceil(view.mxz.hold_left)) : "now";
  const why = [];
  if (view.mxz.standoff) why.push("Standoff: rooms call for heat and for cool.");
  if (view.mxz.by_comfort) why.push("Comfort priority decided the shared mode.");
  if (view.mxz.restarts) why.push(`Home Assistant restarted ${view.mxz.restarts}×.`);
  $("mxz-why").textContent = why.join(" ");
  $("mxz-quiet").textContent = view.mxz.quiet_for === null ? ""
    : `Home Assistant just started: MXZ takes no change made at a head for another ${clock(Math.ceil(view.mxz.quiet_for))}.`;
  for (const side of ["stock", "mxz"]) {
    for (const room of view[side].rooms) {
      const node = $(`${side}-${room.key}`);
      node.querySelector(".temp").textContent = degrees(room.temperature);
      const head = node.querySelector(".head");
      head.textContent = headLine(room);
      head.className = `head doing-${room.doing}`;
      if (side === "mxz") {
        const badges = [];
        if (!room.enable) badges.push("room off");
        if (room.fan_held) badges.push("fan held");
        if (room.adopting) badges.push("taking the head's change");
        node.querySelector(".badge").textContent = badges.join(" · ");
        node.querySelector(".plan").textContent = `Plan: ${room.engage ?? "–"} · target ${Math.round(room.target)} °F`;
        const c = node.controls;
        keep(c.target, room.target);
        c.enable.checked = room.enable;
        c.comfort.checked = room.comfort;
        c.auto.checked = !room.fan_held;
        keep(c.mode, room.mode);
        keep(c.fan, room.fan);
      }
    }
  }
  $("mxz-calls").replaceChildren(...view.mxz.calls.slice().reverse().map((call) =>
    element("li", { textContent: `${clock(call.t)} ${call.room}: ${call.service} ${Object.entries(call.data).map(([k, v]) => `${k} ${v}`).join(", ")}` })));
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
    lab.world = lab.module.World(name);
    for (const button of $("presets").children) button.setAttribute("aria-pressed", String(button.dataset.preset === name));
    const status = $("status");
    status.classList.remove("error");
    status.textContent = "";
    buildRooms(JSON.parse(lab.world.view()));
    lab.playing = true;
    $("play").textContent = "Pause";
  });
}

function wire() {
  for (const [name, title] of PRESETS) {
    const button = element("button", { type: "button", textContent: title });
    button.dataset.preset = name;
    button.addEventListener("click", () => start(name));
    $("presets").append(button);
  }
  $("play").addEventListener("click", () => {
    lab.playing = !lab.playing;
    $("play").textContent = lab.playing ? "Pause" : "Play";
  });
  for (const button of document.querySelectorAll("[data-speed]")) {
    button.addEventListener("click", () => {
      lab.speed = Number(button.dataset.speed);
      for (const other of document.querySelectorAll("[data-speed]")) other.setAttribute("aria-pressed", String(other === button));
    });
  }
  $("skip").addEventListener("click", () => run(() => lab.world.advance(lab.world.next_wakeup())));
  $("restart").addEventListener("click", () => run(() => lab.world.restart()));
  for (const button of document.querySelectorAll("button:disabled")) button.disabled = false;
}

async function boot() {
  try {
    if (typeof loadPyodide !== "function") throw new Error(`Pyodide did not load from ${PYODIDE}. The page needs it to run MXZ's code.`);
    lab.py = await loadPyodide({ indexURL: PYODIDE });
    for (const [path, text] of Object.entries(window.MXZ_SIMULATOR_FILES)) {
      const full = `${HOME}/${path}`;
      lab.py.FS.mkdirTree(full.slice(0, full.lastIndexOf("/")));
      lab.py.FS.writeFile(full, text);
    }
    lab.py.runPython(`import sys; sys.path.insert(0, "${HOME}")`);
    lab.module = lab.py.pyimport("world");
    wire();
    start("mixed");
    requestAnimationFrame(frame);
  } catch (error) {
    fail(error);
  }
}

boot();
