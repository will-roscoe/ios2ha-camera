// The camera's control page: a client of /api/v1, exactly as the Home Assistant
// component is. Every entity comes from /objects, every value from /events, every
// write goes through POST /control/{name}, and every refusal is the API's own.
// Values are only ever set with textContent: nothing from the API is parsed as HTML.
"use strict";

// The one path a client is told to hardcode; every other one comes from its `routes`.
const INFO = "/api/v1/info";
let routes = {};
const CATEGORY_TITLES = [[null, "Camera"], ["config", "Configuration"], ["diagnostic", "Diagnostics"]];
const DOMAIN_ORDER = ["button", "switch", "select", "number", "sensor", "binary_sensor"];

let objects = [];
let objectsVersion = null;
let media = [];
let deviceName = "Camera";
const values = {};            // state key -> value
const rows = new Map();       // state key -> {update(value)}
const pending = new Map();    // state key -> timer
const heard = new Map();      // state key -> when its last state event arrived

function $(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const c of children) if (c) el.append(c);
  return el;
}

async function getJSON(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

// -- writes ---------------------------------------------------------------------

async function write(e, value, row) {
  const sent = performance.now();
  row.classList.add("pending");
  row.error.textContent = "";
  let body;
  try {
    const r = await fetch(routes.control.replace("{name}", encodeURIComponent(e.object_id)), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
    const text = await r.text();
    try { body = JSON.parse(text); } catch { body = { ok: false, error: `HTTP ${r.status}` }; }
  } catch (err) {
    body = { ok: false, error: String(err) };
  }
  if (!body.ok) {
    row.classList.remove("pending");
    row.error.textContent = body.error || "refused";
    // Snap back to the real value, even in a box that still has focus.
    if (e.state_key) rows.get(e.state_key)?.update(values[e.state_key], true);
    return;
  }
  // Accepted is not applied: the next state event for this key clears it.
  if (!e.state_key) { setTimeout(() => row.classList.remove("pending"), 800); return; }
  const key = e.state_key;
  clearTimeout(pending.get(key));
  // The camera echoes a write as soon as it is stored, and that event can beat
  // this response here: then it is already confirmed.
  if ((heard.get(key) ?? -1) >= sent) { row.classList.remove("pending"); return; }
  // Never confirmed: show the camera's real value again, and say so.
  pending.set(key, setTimeout(() => {
    row.classList.remove("pending");
    rows.get(key)?.update(values[key], true);
    row.error.textContent = "Not confirmed by the camera; showing its current value.";
  }, 15000));
}

// -- rendering ------------------------------------------------------------------

function relative(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 5) return "just now";     // also a clock a little ahead of this one
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(t).toLocaleString();
}

function formatValue(e, v) {
  if (v === undefined || v === null) return "unknown";
  if (e.device_class === "timestamp") return relative(v);
  if (typeof v === "number" && e.precision !== null && e.precision !== undefined) v = v.toFixed(e.precision);
  return e.unit ? `${v} ${e.unit}` : String(v);
}

function control(e, row) {
  const key = e.state_key;
  switch (e.domain) {
    case "button": {
      const b = $("button", { text: "Press", onclick: () => write(e, "PRESS", row) });
      return { el: b, update() {} };
    }
    case "switch": {
      const c = $("input", { type: "checkbox", onchange: () => write(e, c.checked, row) });
      return { el: $("label", { class: "switch" }, c, $("span")), update(v) { c.checked = v === true; } };
    }
    case "select": {
      const s = $("select", { onchange: () => write(e, s.value, row) });
      for (const o of e.options || []) s.append($("option", { value: o, text: o }));
      return { el: s, update(v) { if (v !== undefined && v !== null) s.value = v; } };
    }
    case "number": {
      const i = $("input", { type: "number", min: e.lo, max: e.hi, step: e.step });
      i.addEventListener("change", () => {
        const n = Number(i.value);
        if (i.value !== "" && !Number.isNaN(n)) write(e, n, row);
      });
      const wrap = $("span", { class: "number" }, i, e.unit ? $("span", { class: "unit", text: e.unit }) : null);
      // A box being typed in is left alone, unless a refusal has to put it back.
      return { el: wrap, update(v, force) { if (force || document.activeElement !== i) i.value = v ?? ""; } };
    }
    case "binary_sensor": {
      const b = $("span", { class: "badge" });
      return { el: b, update(v) {
        b.textContent = v === true ? "on" : v === false ? "off" : "unknown";
        b.dataset.on = String(v === true);
      } };
    }
    default: {
      const s = $("span", { class: "value" });
      return { el: s, update(v) { s.textContent = formatValue(e, v); s.title = v ?? ""; } };
    }
  }
}

function renderEntities() {
  const host = document.getElementById("entities");
  host.replaceChildren();
  rows.clear();
  for (const [category, title] of CATEGORY_TITLES) {
    const list = objects
      .filter((e) => e.domain !== "camera" && (e.category ?? null) === category)
      .sort((a, b) => DOMAIN_ORDER.indexOf(a.domain) - DOMAIN_ORDER.indexOf(b.domain));
    if (!list.length) continue;
    const section = $("details", { class: "group" }, $("summary", { text: `${title} (${list.length})` }));
    if (category === null) section.open = true;
    for (const e of list) {
      const row = $("div", { class: `row ${e.domain}` });
      row.error = $("div", { class: "error" });
      const c = control(e, row);
      const label = $("div", { class: "label" },
        $("div", { class: "name", text: e.name ?? deviceName }),
        e.description ? $("div", { class: "help", text: e.description }) : null);
      row.append(label, $("div", { class: "control" }, c.el), row.error);
      section.append(row);
      if (e.state_key) {
        rows.set(e.state_key, {
          update(v, force) { c.update(v, force); row.classList.remove("pending"); },
          row,
        });
        c.update(values[e.state_key]);
      }
    }
    host.append(section);
  }
}

// -- the picture ------------------------------------------------------------------

let activeMedia = null;
let stillUrl = null;

async function showSnapshot(m, img) {
  try {
    const r = await fetch(m.url, { cache: "no-store" });
    if (activeMedia !== m) return;            // another view was chosen meanwhile
    if (!r.ok) { img.alt = `Not available yet (HTTP ${r.status})`; img.removeAttribute("src"); return; }
    const blob = await r.blob();
    if (activeMedia !== m) return;
    const url = URL.createObjectURL(blob);
    if (stillUrl) URL.revokeObjectURL(stillUrl);
    stillUrl = url;
    img.src = url;
  } catch (err) {
    img.alt = String(err);
  }
}

function showStart(m, verb) {
  const img = document.getElementById("picture");
  const start = document.getElementById("start-stream");
  start.hidden = false;
  start.textContent = m.holds_phone ? `${verb} (keeps the phone awake while it runs)` : verb;
  start.onclick = () => { start.hidden = true; img.src = m.url; };
}

function stopStream() {
  const img = document.getElementById("picture");
  if (activeMedia && activeMedia.kind === "stream" && img.getAttribute("src")) {
    img.removeAttribute("src");                // closes the connection
    showStart(activeMedia, "Resume");
  }
}

function selectMedia(m) {
  stopStream();
  activeMedia = m;
  const img = document.getElementById("picture");
  const start = document.getElementById("start-stream");
  for (const t of document.querySelectorAll(".tab")) t.classList.toggle("active", t.dataset.id === m.id);
  img.removeAttribute("src");
  if (m.kind === "snapshot") {
    start.hidden = true;
    showSnapshot(m, img);
  } else {
    showStart(m, "Start");
  }
  drawRegion();
}

function renderMedia() {
  const tabs = document.getElementById("tabs");
  tabs.replaceChildren();
  const order = ["still", "stacked", "live", "screenshot"];
  const list = [...media].sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
  for (const m of list) {
    tabs.append($("button", { class: "tab", "data-id": m.id, text: m.id, onclick: () => selectMedia(m) }));
  }
  if (list.length) selectMedia(list[0]);
}

document.addEventListener("visibilitychange", () => { if (document.hidden) stopStream(); });

// -- the colour region picker -------------------------------------------------------
// Draws the colour reference region over the still and lets it be redrawn by dragging.
// It writes the four colour_region_* controls like any other control, so HA and MQTT
// see the same values, and offers the Set colour reference button beside it. Shown
// only when the service has those entities: the page stays a client of /objects.

const REGION_KEYS = ["colour_region_x0", "colour_region_y0", "colour_region_x1", "colour_region_y1"];
let picking = false;
let dragFrom = null;

function entity(domain, id) {
  return objects.find((e) => e.domain === domain && e.object_id === id);
}

function hasRegion() {
  return REGION_KEYS.every((k) => entity("number", k));
}

function regionBox() {
  const [x0, y0, x1, y1] = REGION_KEYS.map((k) => values[entity("number", k)?.state_key]);
  if ([x0, y0, x1, y1].some((v) => typeof v !== "number")) return null;
  return [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)];
}

function drawRegion(box = regionBox()) {
  const overlay = document.getElementById("region");
  const img = document.getElementById("picture");
  const onStill = activeMedia && activeMedia.id === "still" && img.getAttribute("src") && img.naturalWidth;
  if (!hasRegion() || !onStill || !box || !(picking || document.getElementById("show-region").checked)) {
    overlay.hidden = true;
    return;
  }
  const r = img.getBoundingClientRect(), f = document.getElementById("frame").getBoundingClientRect();
  overlay.hidden = false;
  overlay.style.left = `${r.left - f.left + box[0] * r.width}px`;
  overlay.style.top = `${r.top - f.top + box[1] * r.height}px`;
  overlay.style.width = `${(box[2] - box[0]) * r.width}px`;
  overlay.style.height = `${(box[3] - box[1]) * r.height}px`;
}

function showReferenceState() {
  const sensor = entity("sensor", "colour_reference");
  const v = sensor ? values[sensor.state_key] : undefined;
  document.getElementById("reference-state").textContent = v == null ? "" : `Reference: ${String(v).replaceAll("_", " ")}`;
}

// After every reload of /objects: the entities may have come or gone.
function refreshPicker() {
  document.getElementById("region-tools").hidden = !hasRegion();
  if (!hasRegion()) setPicking(false);
  drawRegion();
  showReferenceState();
}

function setPicking(on) {
  picking = on;
  dragFrom = null;
  const pick = document.getElementById("pick-region");
  pick.textContent = on ? "Drag over the still to draw the region" : "Pick colour region";
  pick.classList.toggle("active", on);
  document.getElementById("frame").classList.toggle("picking", on);
}

function fraction(ev) {
  const r = document.getElementById("picture").getBoundingClientRect();
  return [Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)),
          Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height))];
}

function dragBox(ev) {
  const [x, y] = fraction(ev);
  return [Math.min(dragFrom[0], x), Math.min(dragFrom[1], y),
          Math.max(dragFrom[0], x), Math.max(dragFrom[1], y)];
}

// The drawn box stays up while the camera confirms each edge: state events redraw
// it from the real values as they land. A refusal redraws what the camera holds.
async function writeRegion(box) {
  const tools = document.getElementById("region-tools");
  for (const [i, k] of REGION_KEYS.entries()) {
    const e = entity("number", k), value = Math.round(box[i] * 1000) / 1000;
    // An unchanged value brings no state event, so it would never be confirmed.
    if (values[e.state_key] === value) continue;
    await write(e, value, tools);
    if (tools.error.textContent) { drawRegion(); return; }   // refused: stop there
  }
  tools.classList.remove("pending");
}

// Once, at start: listeners that must not pile up on every reload.
function bindPicker() {
  const tools = document.getElementById("region-tools");
  const img = document.getElementById("picture");
  tools.error = document.getElementById("region-error");
  document.getElementById("pick-region").onclick = () => {
    setPicking(!picking);
    if (picking && activeMedia?.id !== "still") {
      const still = media.find((m) => m.id === "still");
      if (still) selectMedia(still);
    }
    drawRegion();
  };
  document.getElementById("show-region").onchange = () => drawRegion();
  document.getElementById("set-reference").onclick = () => {
    const e = entity("button", "set_colour_reference");
    if (e) write(e, "PRESS", tools);
  };
  img.addEventListener("load", () => drawRegion());
  img.addEventListener("pointerdown", (ev) => {
    if (!picking || !img.naturalWidth) return;
    ev.preventDefault();
    dragFrom = fraction(ev);
    img.setPointerCapture(ev.pointerId);
  });
  img.addEventListener("pointermove", (ev) => {
    if (picking && dragFrom) drawRegion(dragBox(ev));
  });
  img.addEventListener("pointerup", async (ev) => {
    if (!picking || !dragFrom) return;
    const box = dragBox(ev);
    dragFrom = null;
    if (box[2] - box[0] < 0.01 || box[3] - box[1] < 0.01) { drawRegion(); return; }   // a click
    setPicking(false);
    document.getElementById("show-region").checked = true;   // keep the new box in view
    drawRegion(box);
    await writeRegion(box);
  });
  img.addEventListener("pointercancel", () => { dragFrom = null; drawRegion(); });
  window.addEventListener("resize", () => drawRegion());
}

// -- live values ------------------------------------------------------------------

function apply(delta) {
  Object.assign(values, delta);
  const now = performance.now();
  for (const [k, v] of Object.entries(delta)) {
    heard.set(k, now);
    const r = rows.get(k);
    if (r) { r.update(v); r.row.error.textContent = ""; }
    clearTimeout(pending.get(k));
  }
  if ("last_still" in delta && activeMedia && activeMedia.id === "still") {
    showSnapshot(activeMedia, document.getElementById("picture"));
  }
  drawRegion();
  showReferenceState();
}

async function loadObjects() {
  const doc = await getJSON(routes.objects);
  objects = doc.objects;
  objectsVersion = doc.objects_version;
  renderEntities();
  refreshPicker();
}

function setConnected(on) {
  document.getElementById("conn").dataset.on = String(on);
  document.getElementById("conn").title = on ? "live" : "reconnecting";
}

let backoff = 1000;

function reconnectLater() {
  setTimeout(connect, backoff);
  backoff = Math.min(backoff * 2, 30000);
}

function connect() {
  const es = new EventSource(routes.events);
  es.addEventListener("open", () => { setConnected(true); backoff = 1000; });
  es.addEventListener("error", () => {
    setConnected(false);
    // EventSource retries a dropped connection by itself, but gives up for good
    // on any answer but 200 -- a 503 while the service shuts down, a proxy's 502.
    if (es.readyState === EventSource.CLOSED) reconnectLater();
  });
  es.addEventListener("snapshot", async (ev) => {
    const d = JSON.parse(ev.data);
    // Replaced at once, before anything awaits: a delta arriving while the
    // objects reload must land on top of this snapshot, not under it.
    for (const k of Object.keys(values)) delete values[k];
    Object.assign(values, d.state);
    if (d.objects_version !== objectsVersion) {
      try {
        await loadObjects();
      } catch {
        es.close(); setConnected(false); reconnectLater(); return;
      }
    }
    for (const [k, r] of rows) r.update(values[k]);
    refreshPicker();
    if (activeMedia && activeMedia.id === "still") showSnapshot(activeMedia, document.getElementById("picture"));
  });
  es.addEventListener("state", (ev) => apply(JSON.parse(ev.data).state));
  es.addEventListener("objects_changed", () => loadObjects().then(() => apply({ ...values })));
}

async function main() {
  try {
    const info = await getJSON(INFO);
    routes = info.routes;
    deviceName = info.device?.name ?? deviceName;
    document.getElementById("title").textContent = deviceName;
    document.getElementById("version").textContent = info.version ?? "";
    media = (await getJSON(routes.media)).media;
    bindPicker();
    renderMedia();
    await loadObjects();
    connect();
    setInterval(() => {   // keeps relative times fresh
      for (const e of objects) if (e.device_class === "timestamp" && e.state_key) rows.get(e.state_key)?.update(values[e.state_key]);
    }, 30000);
  } catch (err) {
    document.getElementById("fatal").textContent = `Could not reach the camera's API: ${err.message}`;
  }
}

main();
