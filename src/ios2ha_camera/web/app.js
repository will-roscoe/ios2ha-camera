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
  row.classList.add("pending");
  row.error.textContent = "";
  let body;
  try {
    const r = await fetch(routes.control.replace("{name}", encodeURIComponent(e.object_id)), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
    body = await r.json();
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
  clearTimeout(pending.get(e.state_key));
  pending.set(e.state_key, setTimeout(() => row.classList.remove("pending"), 15000));
}

// -- rendering ------------------------------------------------------------------

function relative(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.round((Date.now() - t) / 1000);
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
    if (!r.ok) { img.alt = `Not available yet (HTTP ${r.status})`; img.removeAttribute("src"); return; }
    const url = URL.createObjectURL(await r.blob());
    if (stillUrl) URL.revokeObjectURL(stillUrl);
    stillUrl = url;
    img.src = url;
  } catch (err) {
    img.alt = String(err);
  }
}

function stopStream() {
  const img = document.getElementById("picture");
  if (activeMedia && activeMedia.kind === "stream") img.removeAttribute("src");   // closes the connection
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
    start.hidden = false;
    start.textContent = m.holds_phone
      ? "Start (keeps the phone awake while it runs)" : "Start";
    start.onclick = () => { start.hidden = true; img.src = m.url; };
  }
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

// -- live values ------------------------------------------------------------------

function apply(delta) {
  Object.assign(values, delta);
  for (const [k, v] of Object.entries(delta)) {
    rows.get(k)?.update(v);
    clearTimeout(pending.get(k));
  }
  if ("last_still" in delta && activeMedia && activeMedia.id === "still") {
    showSnapshot(activeMedia, document.getElementById("picture"));
  }
}

async function loadObjects() {
  const doc = await getJSON(routes.objects);
  objects = doc.objects;
  objectsVersion = doc.objects_version;
  renderEntities();
}

function setConnected(on) {
  document.getElementById("conn").dataset.on = String(on);
  document.getElementById("conn").title = on ? "live" : "reconnecting";
}

function connect() {
  const es = new EventSource(routes.events);
  es.addEventListener("open", () => setConnected(true));
  es.addEventListener("error", () => setConnected(false));   // EventSource retries by itself
  es.addEventListener("snapshot", async (ev) => {
    const d = JSON.parse(ev.data);
    for (const k of Object.keys(values)) delete values[k];
    if (d.objects_version !== objectsVersion) await loadObjects();
    apply(d.state);
    for (const [k, r] of rows) if (!(k in d.state)) r.update(undefined);
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
