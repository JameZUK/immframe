/* immframe dashboard.
 *
 * One page, four views (Now · Timeline · Blocked · Controls). On phones
 * the views are tabs with a bottom bar; on wide screens "Now" stays on the
 * left and the other three share the right-hand panel.
 *
 * Talks to the frame's REST API with the session cookie set by /login.
 * Every request carries X-Immframe-Client so an expired session answers a
 * plain 401 (→ back to /login) instead of the browser's Basic-auth popup.
 *
 * All DOM is built with createElement / textContent — nothing from the
 * server or the library is ever interpreted as HTML.
 */
"use strict";

const $ = id => document.getElementById(id);
const POLL_MS = 2000;
const TIMELINE_POLL_MS = 6000;
const WIDE = window.matchMedia("(min-width: 960px)");

let state = null;
let session = null;
let clockSkew = 0;                    // server time − local time (ms)
let lastAssetKey = null;
let timelineKey = null;
let timelineData = null;
let blockedData = [];
const selected = new Set();
const touched = new Map();            // control id → last local edit (ms)

const LIVE_HINTS = {
  once: "Plays forward once.",
  loop: "Plays forward over and over.",
  bounce: "Forward, then backward, again and again — a boomerang.",
  reverse: "Plays backward once — a rewind.",
  still: "Never plays the clip — just shows the photo.",
};
const fmtSec = v => (v === 0 ? "no limit" : fmtDuration(v));

const MODE_LABELS = {
  playlist: "Playlist", random: "Random", favorites: "Favourites", scene: "Scenes",
  people: "People", memory: "On this day", recent: "Recent uploads", album: "Albums", smart: "Smart search",
};

// ── Small helpers ─────────────────────────────────────────────────────────

function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null) continue;
    if (k === "class") e.className = v;
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (k === "text") e.textContent = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
    else if (k in e) e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) if (c !== null && c !== undefined && c !== false) e.append(c);
  return e;
}

function icon(name, cls = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "ic " + cls);
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#" + name);
  svg.append(use);
  return svg;
}

function setIcon(useEl, name) { useEl.setAttribute("href", "#" + name); }

function nowMs() { return Date.now() + clockSkew; }

function ago(epochSec) {
  if (!epochSec) return "";
  const s = Math.max(0, Math.round(nowMs() / 1000 - epochSec));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const d = Math.round(s / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

function fmtDuration(sec) {
  sec = Math.round(sec);
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60), s = sec % 60;
  return s ? `${m}m ${s}s` : `${m} min`;
}

function fmtCountdown(sec) {
  sec = Math.max(0, Math.round(sec));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function place(a) { return [a.city, a.country].filter(Boolean).join(", "); }

function titleOf(a) {
  if (!a) return "—";
  if (a.is_collage) return a.file || "Collage";
  return place(a) || a.file || "Untitled";
}

function subOf(a) {
  if (!a || a.is_collage) return "";
  return [fmtDate(a.taken_at), a.camera].filter(Boolean).join(" · ");
}

function thumbSrc(a) { return `/api/thumb/${encodeURIComponent(a.id)}`; }
function previewSrc(a) {
  return a.is_collage ? `/api/current_image?v=${encodeURIComponent(a.id)}` : `/api/image/${encodeURIComponent(a.id)}`;
}
function immichLink(id) {
  return state && state.immich_url ? `${state.immich_url}/photos/${encodeURIComponent(id)}` : null;
}
function isTyping() {
  const t = document.activeElement;
  return t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA");
}

// ── API ───────────────────────────────────────────────────────────────────

async function api(path, { method = "GET", body } = {}) {
  const headers = { "X-Immframe-Client": "web" };
  const opts = { method, headers, cache: "no-store" };
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    if (body !== undefined) opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (r.status === 401) {
    const here = location.pathname === "/" ? "" : `?next=${encodeURIComponent(location.pathname)}`;
    location.href = "/login" + here;
    throw new Error("login required");
  }
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error((data && data.error) || `${path} → HTTP ${r.status}`);
  return data;
}

async function post(path, body) {
  const data = await api(path, { method: "POST", body });
  if (data && "paused" in data && "selection_mode" in data) render(data);
  return data;
}

// ── Toasts ────────────────────────────────────────────────────────────────

function toast(text, kind = "ok", action = null) {
  const host = $("toasts");
  const ic = kind === "err" ? "i-ban" : kind === "warn" ? "i-clock" : "i-check";
  const t = el("div", { class: `toast ${kind}`, role: "status" }, icon(ic), el("span", { text }));
  if (action) {
    t.append(el("button", { type: "button", class: "btn", text: action.label, on: { click: () => { action.fn(); dismiss(); } } }));
  }
  host.append(t);
  while (host.children.length > 3) host.firstChild.remove();
  let gone = false;
  function dismiss() {
    if (gone) return; gone = true;
    t.classList.add("out");
    setTimeout(() => t.remove(), 260);
  }
  setTimeout(dismiss, action ? 7000 : 3800);
}

async function attempt(fn, okText) {
  try {
    const r = await fn();
    if (okText) toast(typeof okText === "function" ? okText(r) : okText);
    return r;
  } catch (e) {
    if (e.message !== "login required") toast(e.message, "err");
    return null;
  }
}

// ── Navigation ────────────────────────────────────────────────────────────

const VIEWS = ["now", "timeline", "blocked", "controls"];

function go(view, { push = true } = {}) {
  if (!VIEWS.includes(view)) view = "now";
  document.body.dataset.view = view;
  if (view !== "now") {
    document.body.dataset.panel = view;
    try { localStorage.setItem("immframe.panel", view); } catch (e) { /* storage off */ }
  }
  for (const b of document.querySelectorAll(".tabbar [data-go]")) b.classList.toggle("active", b.dataset.go === view);
  for (const b of document.querySelectorAll(".topnav [data-go]")) b.classList.toggle("active", b.dataset.go === document.body.dataset.panel);
  if (push && location.hash !== "#" + view) history.replaceState(null, "", "#" + view);
  if (panelVisible("timeline")) loadTimeline();
  if (panelVisible("blocked")) loadBlocked();
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
}

function panelVisible(name) {
  const b = document.body.dataset;
  return WIDE.matches ? b.panel === name : b.view === name;
}

// ── Rendering: state ──────────────────────────────────────────────────────

function render(s) {
  state = s;
  if (typeof s.now === "number") clockSkew = s.now * 1000 - Date.now();
  const a = s.current_asset;
  const pa = s.pair_asset;

  // Status pill
  const st = $("status");
  st.dataset.status = s.paused ? "paused" : "ok";
  $("status-text").textContent = s.paused ? "Paused" : s.video_playing ? "Playing video" : "Live";

  // Stage image (only when the slide changes)
  const key = a ? `${a.id}|${pa ? pa.id : ""}` : null;
  if (key !== lastAssetKey) {
    lastAssetKey = key;
    loadStage(a, pa);
    if (panelVisible("timeline")) loadTimeline();
  }

  // Badges
  const badges = $("stage-badges");
  badges.replaceChildren();
  if (a) {
    if (a.is_collage) badges.append(el("span", { class: "badge-pill" }, icon("i-grid"), "Collage"));
    else if (a.kind === "VIDEO") badges.append(el("span", { class: "badge-pill" }, icon("i-video"), "Video"));
    if (a.live) badges.append(el("span", { class: "badge-pill" }, icon("i-live"), "Live photo"));
    if (pa) badges.append(el("span", { class: "badge-pill" }, icon("i-columns"), "Pair"));
  }
  $("stage-fav").hidden = !(a && a.favorite);
  $("stage-paused").hidden = !s.paused;
  $("stage-title").textContent = a ? titleOf(a) + (pa ? `  +  ${titleOf(pa)}` : "") : "—";
  $("stage-sub").textContent = subOf(a);
  $("stage").classList.toggle("video", !!s.video_playing);

  // Transport + actions
  $("btn-prev").disabled = !s.can_go_back;
  setIcon($("pause-icon"), s.paused ? "i-play" : "i-pause");
  $("btn-pause").title = s.paused ? "Resume (space)" : "Pause (space)";
  $("btn-pause").setAttribute("aria-label", s.paused ? "Resume" : "Pause");
  const real = a && !a.is_collage;
  $("btn-favorite").disabled = !real;
  $("btn-favorite").classList.toggle("on", !!(a && a.favorite));
  $("fav-label").textContent = a && a.favorite ? "Favourited" : "Favourite";
  $("btn-rotate").disabled = !(real && a.kind === "IMAGE");
  $("btn-hide").disabled = !real;
  const link = real ? immichLink(a.id) : null;
  if (link) { $("btn-open").href = link; $("btn-open").removeAttribute("aria-disabled"); }
  else { $("btn-open").removeAttribute("href"); $("btn-open").setAttribute("aria-disabled", "true"); }

  // Details
  $("mode-chip").textContent = MODE_LABELS[s.selection_mode] || s.selection_mode;
  $("scene-label").textContent = s.current_scene || "";
  $("meta-date").textContent = a && a.taken_at ? new Date(a.taken_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";
  $("meta-where").textContent = (a && place(a)) || "—";
  $("meta-camera").textContent = (a && a.camera) || "—";
  $("meta-file").textContent = (a && a.file) || "—";

  // Blocked counts
  const n = s.hidden_count || 0;
  for (const id of ["blocked-count-top", "blocked-count-tab"]) {
    $(id).textContent = String(n);
    $(id).hidden = n === 0;
  }
  $("blocked-count").textContent = String(n);

  renderControls(s);
  tick();
}

function loadStage(a, pa) {
  // Keep the old photo up while the new one downloads, then a quick
  // fade-out / swap / fade-in — no dark gap on a slow connection.
  const stage = $("stage");
  if (!a) { stage.dataset.empty = "true"; stage.classList.remove("loaded"); return; }
  const key = `${a.id}|${pa ? pa.id : ""}`;
  const src = previewSrc(a);
  const pairSrc = pa ? previewSrc(pa) : null;
  const ready = [src, pairSrc].filter(Boolean).map(u => new Promise(res => {
    const img = new Image();
    img.onload = img.onerror = res;
    img.src = u;
  }));
  Promise.all(ready).then(() => {
    if (lastAssetKey !== key) return;                          // superseded meanwhile
    const swap = () => {
      $("stage-img").src = src;
      $("stage-backdrop").src = src;
      const second = $("stage-img-pair");
      if (pairSrc) { second.src = pairSrc; second.hidden = false; }
      else { second.hidden = true; second.removeAttribute("src"); }
      stage.dataset.empty = "false";
      requestAnimationFrame(() => stage.classList.add("loaded"));
    };
    if (stage.classList.contains("loaded")) {
      stage.classList.remove("loaded");
      setTimeout(swap, 220);
    } else {
      swap();
    }
  });
}

// Progress bar + countdown, every 250 ms between polls.
function tick() {
  if (!state) return;
  const bar = $("progress-bar");
  const label = $("next-in");
  const s = state;
  if (s.video_playing) { label.textContent = "Playing video"; return; }
  if (s.paused) { label.textContent = "Paused — the slideshow will wait"; return; }
  const start = s.slide_started_at, end = s.next_change_at;
  if (!start || !end || end <= start) { bar.style.width = "0"; label.textContent = " "; return; }
  const now = nowMs() / 1000;
  const pct = Math.min(1, Math.max(0, (now - start) / (end - start)));
  bar.style.width = (pct * 100).toFixed(2) + "%";
  label.textContent = `Next photo in ${fmtCountdown(end - now)}`;
}

// ── Rendering: controls ───────────────────────────────────────────────────

function fresh(id) { return Date.now() - (touched.get(id) || 0) > 3000 && document.activeElement !== $(id); }
function touch(id) { touched.set(id, Date.now()); }

function renderControls(s) {
  if (fresh("mode")) $("mode").value = s.selection_mode;
  for (const f of document.querySelectorAll("[data-for-mode]")) f.hidden = f.dataset.forMode !== $("mode").value;
  if (fresh("album-ids")) $("album-ids").value = (s.album_ids || []).join(", ");
  if (fresh("smart-query")) $("smart-query").value = s.smart_query || "";
  if (fresh("people-ids")) $("people-ids").value = (s.people_ids || []).join(", ");
  if (fresh("time-delay")) $("time-delay").value = s.time_delay;
  $("time-delay-value").textContent = fmtDuration(Number($("time-delay").value));
  if (fresh("fade-time")) $("fade-time").value = s.fade_time;
  $("fade-time-value").textContent = `${Number($("fade-time").value).toFixed(1)}s`;
  if (fresh("brightness")) $("brightness").value = s.brightness;
  $("brightness-value").textContent = `${Math.round(Number($("brightness").value) * 100)}%`;
  if (fresh("display-is-on")) $("display-is-on").checked = !!s.display_is_on;
  if (fresh("show-clock")) $("show-clock").checked = !!s.show_clock;
  if (fresh("show-text")) {
    const on = new Set(s.show_text || []);
    for (const c of $("show-text").children) c.classList.toggle("on", on.has(c.dataset.key));
  }
  const live = s.live_photo;
  if (live) {
    if (fresh("live-mode")) for (const b of $("live-mode").children) b.classList.toggle("on", b.dataset.value === live.mode);
    const mode = [...$("live-mode").children].find(b => b.classList.contains("on"))?.dataset.value || live.mode;
    $("live-mode-hint").textContent = LIVE_HINTS[mode] || "";
    $("live-timing").hidden = mode === "still";
    const repeating = mode === "loop" || mode === "bounce";
    for (const n of document.querySelectorAll("[data-live='repeat']")) n.hidden = !repeating;
    for (const n of document.querySelectorAll("[data-live='single']")) n.hidden = repeating;
    if (fresh("live-hold")) $("live-hold").value = live.hold_s;
    if (fresh("live-play")) $("live-play").value = live.play_s;
    if (fresh("live-cap")) $("live-cap").value = live.play_s;
    if (fresh("live-repeats")) $("live-repeats").value = live.repeats;
    if (fresh("live-speed")) $("live-speed").value = live.speed;
    if (fresh("live-pause")) $("live-pause").value = live.pause_s;
    if (fresh("live-after-next")) $("live-after-next").checked = live.after === "next";
    $("live-hold-value").textContent = fmtDuration(Number($("live-hold").value));
    $("live-play-value").textContent = Number($("live-repeats").value) > 0 ? "(using repeats)" : fmtSec(Number($("live-play").value));
    $("live-cap-value").textContent = Number($("live-cap").value) === 0 ? "its natural end" : fmtDuration(Number($("live-cap").value));
    const r = Number($("live-repeats").value);
    $("live-repeats-value").textContent = r === 0 ? "off — use the time" : `${r} time${r === 1 ? "" : "s"}`;
    $("live-speed-value").textContent = `${Number($("live-speed").value)}×`;
    $("live-pause-value").textContent = Number($("live-pause").value) === 0 ? "none" : `${Number($("live-pause").value)}s`;
  }

  const col = s.collage || {};
  if (fresh("collage-enabled")) $("collage-enabled").checked = !!col.enabled;
  if (fresh("collage-layout")) for (const b of $("collage-layout").children) b.classList.toggle("on", b.dataset.value === col.layout);
  if (fresh("collage-min")) $("collage-min").value = col.min_tiles ?? 3;
  if (fresh("collage-max")) $("collage-max").value = col.max_tiles ?? 6;
  $("collage-min-value").textContent = $("collage-min").value;
  $("collage-max-value").textContent = $("collage-max").value;
}

// ── Timeline ──────────────────────────────────────────────────────────────

let timelineBusy = false;
let lastTimelineTouch = 0;            // defer re-renders while the user is tapping tiles
async function loadTimeline() {
  if (timelineBusy) return;
  timelineBusy = true;
  try {
    const t = await api("/api/timeline");
    if (typeof t.now === "number") clockSkew = t.now * 1000 - Date.now();
    timelineData = t;
    const key = JSON.stringify([t.position, t.history.map(h => h.id + (h.blocked ? "!" : "")), t.upcoming.map(u => u.id)]);
    const busy = !$("sheet").hidden || Date.now() - lastTimelineTouch < 2500;
    if (key !== timelineKey && !busy) { timelineKey = key; renderTimeline(t); }
    else for (const span of document.querySelectorAll("[data-ago]")) span.textContent = ago(Number(span.dataset.ago));
  } catch (e) {
    if (e.message !== "login required") console.warn(e);
  } finally {
    timelineBusy = false;
  }
}

function tile(a, { label, tag, num, current, onClick }) {
  const t = el("button", { type: "button", class: "tile" + (current ? " current" : ""), title: titleOf(a), on: { click: onClick } });
  if (a.is_collage) {
    t.classList.add("placeholder");
    t.append(icon("i-grid"));
  } else {
    t.append(el("img", { src: thumbSrc(a), alt: "", loading: "lazy", decoding: "async" }));
  }
  if (tag) t.append(el("span", { class: "tile-tag" }, tag.icon ? icon(tag.icon) : null, tag.text));
  else if (a.kind === "VIDEO") t.append(el("span", { class: "tile-tag" }, icon("i-video"), "Video"));
  else if (a.live) t.append(el("span", { class: "tile-tag" }, icon("i-live"), "Live"));
  if (num) t.append(el("span", { class: "num", text: String(num) }));
  if (label) t.append(label);
  return t;
}

function renderTimeline(t) {
  const up = $("upcoming");
  up.replaceChildren();
  $("upnext-note").textContent = t.upcoming.length ? `${t.upcoming.length} ready` : "";
  if (!t.upcoming.length) up.append(el("div", { class: "empty", text: "Fetching the next photos…" }));
  t.upcoming.forEach((a, i) => {
    up.append(tile(a, {
      num: i + 1,
      label: el("span", { class: "tile-label", text: titleOf(a) }),
      onClick: () => openSheet(a, "upcoming"),
    }));
  });

  const hist = $("history");
  hist.replaceChildren();
  const items = t.history.map((h, i) => ({ h, i })).reverse();
  if (!items.length) hist.append(el("div", { class: "empty", text: "Nothing shown yet since the frame started." }));
  for (const { h, i } of items) {
    const isCurrent = i === t.position;
    const node = tile(h, {
      current: isCurrent,
      tag: h.blocked ? { icon: "i-ban", text: "Blocked" } : isCurrent ? { icon: "i-image", text: "On screen" } : null,
      label: el("span", { class: "tile-label" }, el("span", { "data-ago": String(h.shown_at || ""), text: ago(h.shown_at) })),
      onClick: () => openSheet(h, h.blocked ? "blocked" : isCurrent ? "current" : "history"),
    });
    if (h.blocked) node.classList.add("blocked");
    hist.append(node);
  }
}

// ── Blocked ───────────────────────────────────────────────────────────────

let blockedBusy = false;
async function loadBlocked() {
  if (blockedBusy) return;
  blockedBusy = true;
  try {
    const r = await api("/api/hidden");
    blockedData = r.hidden || [];
    for (const id of [...selected]) if (!blockedData.some(b => b.id === id)) selected.delete(id);
    renderBlocked();
  } catch (e) {
    if (e.message !== "login required") toast(e.message, "err");
  } finally {
    blockedBusy = false;
  }
}

function renderBlocked() {
  const host = $("blocked");
  host.replaceChildren();
  const q = $("blocked-filter").value.trim().toLowerCase();
  const items = blockedData.filter(b => !q || (b.file || b.id).toLowerCase().includes(q));
  $("blocked-count").textContent = String(blockedData.length);
  if (!blockedData.length) {
    host.append(el("div", { class: "empty", text: "Nothing blocked. Use “Block” on a photo you never want to see on the frame again." }));
  } else if (!items.length) {
    host.append(el("div", { class: "empty", text: "No blocked photos match that filter." }));
  }
  for (const b of items) {
    const isSel = selected.has(b.id);
    const card = el("div", { class: "btile" + (isSel ? " selected" : "") });
    const thumb = el("div", { class: "thumb", title: "View", on: { click: () => openSheet(b, "blocked") } },
      el("img", { src: thumbSrc(b), alt: "", loading: "lazy", decoding: "async" }),
      el("button", {
        type: "button", class: "select", "aria-label": "Select", title: "Select",
        on: { click: ev => { ev.stopPropagation(); toggleSelect(b.id); } },
      }, icon("i-check")),
    );
    const when = [b.at ? `Blocked ${ago(b.at)}` : "Blocked", b.archived ? "archived in Immich" : null].filter(Boolean).join(" · ");
    card.append(
      thumb,
      el("div", { class: "info" },
        el("div", { class: "file", text: b.file || `Asset ${b.id.slice(0, 8)}…`, title: b.file || b.id }),
        el("div", { class: "when", text: when })),
      el("button", { type: "button", class: "btn unblock", on: { click: () => unblock([b.id]) } }, icon("i-undo"), "Unblock"),
    );
    host.append(card);
  }
  $("bulkbar").hidden = selected.size === 0;
  $("bulk-count").textContent = `${selected.size} selected`;
}

function toggleSelect(id) {
  if (selected.has(id)) selected.delete(id); else selected.add(id);
  renderBlocked();
}

async function unblock(ids) {
  const names = ids.map(id => (blockedData.find(b => b.id === id) || {}).file).filter(Boolean);
  const r = await attempt(() => post("/api/unhide", { ids }));
  if (!r) return;
  const errs = (r.results || []).filter(x => x.error);
  if (errs.length) toast(errs[0].error, "warn");
  else toast(ids.length === 1 ? `Unblocked ${names[0] || "photo"} — it can come up again` : `Unblocked ${ids.length} photos`);
  ids.forEach(id => selected.delete(id));
  await Promise.all([loadBlocked(), refresh()]);
}

async function block(asset) {
  const r = await attempt(() => post("/api/hide", asset ? { id: asset.id } : undefined));
  if (!r) return;
  const id = r.hidden;
  const msg = r.archived ? "Blocked and archived in Immich" : "Blocked on the frame";
  toast(msg, r.archived ? "ok" : "warn", { label: "Undo", fn: () => unblock([id]) });
  timelineKey = null;
  await refresh();
  if (panelVisible("timeline")) loadTimeline();
  if (panelVisible("blocked")) loadBlocked();
}

// ── Detail sheet ──────────────────────────────────────────────────────────

function openSheet(a, source) {
  $("sheet-img").src = a.is_collage ? "" : previewSrc(a);
  $("sheet-img").alt = titleOf(a);
  $("sheet-title").textContent = source === "blocked" ? (a.file || titleOf(a) || `Asset ${a.id}`) : titleOf(a);
  const sub = source === "blocked"
    ? [a.at ? `Blocked ${ago(a.at)}` : "Blocked", a.archived ? "archived in Immich" : null].filter(Boolean).join(" · ")
    : [subOf(a), a.file !== titleOf(a) ? a.file : null,
       source === "history" && a.shown_at ? `shown ${ago(a.shown_at)}` : null].filter(Boolean).join(" · ");
  $("sheet-sub").textContent = sub;
  const acts = $("sheet-actions");
  acts.replaceChildren();
  const btn = (text, ic, cls, fn) => el("button", { type: "button", class: "btn " + cls, on: { click: async () => { closeSheet(); await fn(); } } }, icon(ic), text);
  if (a.is_collage) {
    acts.append(el("p", { class: "muted small", text: "Collages are made on the frame from several photos, so they can't be shown again or blocked." }));
  } else if (source === "blocked") {
    acts.append(btn("Unblock", "i-undo", "primary", () => unblock([a.id])));
  } else {
    if (source === "history") {
      acts.append(btn("Show again", "i-replay", "primary", () => attempt(() => post("/api/show", { id: a.id }), "Coming up on the frame")));
    }
    acts.append(btn(source === "upcoming" ? "Skip & block" : "Block", "i-ban", "danger", () => block(a)));
  }
  const link = !a.is_collage && immichLink(a.id);
  if (link) acts.append(el("a", { class: "btn", href: link, target: "_blank", rel: "noopener" }, icon("i-external"), "Open in Immich"));
  $("sheet").hidden = false;
  $("sheet-backdrop").hidden = false;
  $("sheet-close").focus();
}

function closeSheet() {
  $("sheet").hidden = true;
  $("sheet-backdrop").hidden = true;
  $("sheet-img").removeAttribute("src");
  timelineKey = null;                 // catch up on anything deferred while it was open
  if (panelVisible("timeline")) loadTimeline();
}

// ── Polling ───────────────────────────────────────────────────────────────

let refreshBusy = false;
async function refresh() {
  if (refreshBusy) return;
  refreshBusy = true;
  try {
    render(await api("/api/state"));
  } catch (e) {
    if (e.message !== "login required") {
      $("status").dataset.status = "err";
      $("status-text").textContent = "Offline";
    }
  } finally {
    refreshBusy = false;
  }
}

// ── Wiring ────────────────────────────────────────────────────────────────

function debounce(fn, ms) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
const ids = v => v.split(/[\s,]+/).map(x => x.trim()).filter(Boolean);

function wire() {
  for (const b of document.querySelectorAll("[data-go]")) b.addEventListener("click", () => go(b.dataset.go));
  WIDE.addEventListener("change", () => go(document.body.dataset.view, { push: false }));
  window.addEventListener("hashchange", () => go((location.hash || "").slice(1), { push: false }));
  for (const id of ["history", "upcoming"]) {
    $(id).addEventListener("pointerdown", () => { lastTimelineTouch = Date.now(); });
    $(id).addEventListener("scroll", () => { lastTimelineTouch = Date.now(); }, { passive: true });
  }

  // Transport
  $("btn-prev").addEventListener("click", () => attempt(() => post("/api/previous")));
  $("btn-next").addEventListener("click", () => attempt(() => post("/api/next")).then(() => setTimeout(refresh, 700)));
  $("btn-pause").addEventListener("click", () => attempt(() => post("/api/paused", { value: !(state && state.paused) })));

  // Curation
  $("btn-favorite").addEventListener("click", () => attempt(() => post("/api/favorite"),
    r => r && r.current_asset && r.current_asset.favorite ? "Added to favourites in Immich" : "Removed from favourites"));
  $("btn-rotate").addEventListener("click", () => attempt(() => post("/api/rotate", { angle: 90 }),
    r => `Rotated to ${r.angle}° — the frame updates once Immich has redrawn it`));
  $("btn-hide").addEventListener("click", () => block(null));

  // Controls
  $("mode").addEventListener("change", () => { touch("mode"); attempt(() => post("/api/selection_mode", { value: $("mode").value }), `Now showing: ${MODE_LABELS[$("mode").value]}`); });
  const textCommit = (id, endpoint, conv) => {
    const commit = debounce(() => attempt(() => post(endpoint, { value: conv($(id).value) })), 900);
    $(id).addEventListener("input", () => { touch(id); commit(); });
  };
  textCommit("album-ids", "/api/album_ids", ids);
  textCommit("smart-query", "/api/smart_query", v => v);
  textCommit("people-ids", "/api/people_ids", ids);

  const rangeCommit = (id, endpoint, labelFn, labelId) => {
    $(id).addEventListener("input", () => { touch(id); $(labelId).textContent = labelFn(Number($(id).value)); });
    $(id).addEventListener("change", () => { touch(id); attempt(() => post(endpoint, { value: Number($(id).value) })); });
  };
  rangeCommit("time-delay", "/api/time_delay", fmtDuration, "time-delay-value");
  rangeCommit("fade-time", "/api/fade_time", v => `${v.toFixed(1)}s`, "fade-time-value");
  rangeCommit("brightness", "/api/brightness", v => `${Math.round(v * 100)}%`, "brightness-value");
  rangeCommit("collage-min", "/api/collage_min_tiles", v => String(v), "collage-min-value");
  rangeCommit("collage-max", "/api/collage_max_tiles", v => String(v), "collage-max-value");

  const switchCommit = (id, endpoint) => $(id).addEventListener("change", () => { touch(id); attempt(() => post(endpoint, { value: $(id).checked })); });
  switchCommit("display-is-on", "/api/display_is_on");
  switchCommit("show-clock", "/api/show_clock");
  switchCommit("collage-enabled", "/api/collage_enabled");

  for (const c of $("show-text").children) {
    c.addEventListener("click", () => {
      touch("show-text");
      c.classList.toggle("on");
      const keys = [...$("show-text").children].filter(x => x.classList.contains("on")).map(x => x.dataset.key);
      attempt(() => post("/api/show_text", { value: keys }));
    });
  }
  for (const b of $("collage-layout").children) {
    b.addEventListener("click", () => {
      touch("collage-layout");
      for (const x of $("collage-layout").children) x.classList.toggle("on", x === b);
      attempt(() => post("/api/collage_layout", { value: b.dataset.value }));
    });
  }

  // Live photos
  const liveSet = (field, value) => attempt(() => post("/api/live_photo", { [field]: value }));
  for (const b of $("live-mode").children) {
    b.addEventListener("click", () => {
      touch("live-mode");
      for (const x of $("live-mode").children) x.classList.toggle("on", x === b);
      if (state) renderControls(state);
      attempt(() => post("/api/live_photo", { mode: b.dataset.value }), `Live photos: ${b.textContent.toLowerCase()}`);
    });
  }
  for (const [id, field] of [["live-hold", "hold_s"], ["live-play", "play_s"], ["live-cap", "play_s"],
                             ["live-repeats", "repeats"], ["live-speed", "speed"], ["live-pause", "pause_s"]]) {
    $(id).addEventListener("input", () => { touch(id); if (state) renderControls(state); });
    $(id).addEventListener("change", () => { touch(id); liveSet(field, Number($(id).value)); });
  }
  $("live-after-next").addEventListener("change", () => {
    touch("live-after-next");
    liveSet("after", $("live-after-next").checked ? "next" : "still");
  });

  // Blocked
  $("blocked-filter").addEventListener("input", renderBlocked);
  $("bulk-clear").addEventListener("click", () => { selected.clear(); renderBlocked(); });
  $("bulk-unblock").addEventListener("click", () => unblock([...selected]));

  // Sheet
  $("sheet-close").addEventListener("click", closeSheet);
  $("sheet-backdrop").addEventListener("click", closeSheet);

  // Logout
  $("btn-logout").addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST", headers: { "Content-Type": "application/json", "X-Immframe-Client": "web" } }).catch(() => {});
    location.href = "/login";
  });

  // Keyboard
  document.addEventListener("keydown", ev => {
    if (ev.key === "Escape" && !$("sheet").hidden) { closeSheet(); return; }
    if (isTyping() || ev.ctrlKey || ev.metaKey || ev.altKey || !$("sheet").hidden) return;
    if (ev.key === "ArrowRight") { ev.preventDefault(); $("btn-next").click(); }
    else if (ev.key === "ArrowLeft") { ev.preventDefault(); if (!$("btn-prev").disabled) $("btn-prev").click(); }
    else if (ev.key === " ") { ev.preventDefault(); $("btn-pause").click(); }
    else if (ev.key === "f" && !$("btn-favorite").disabled) $("btn-favorite").click();
    else if (ev.key === "r" && !$("btn-rotate").disabled) $("btn-rotate").click();
  });

  document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(); if (panelVisible("timeline")) loadTimeline(); } });
}

async function init() {
  wire();
  let start = (location.hash || "").slice(1);
  let panel = null;
  try { panel = localStorage.getItem("immframe.panel"); } catch (e) { /* storage off */ }
  if (panel && panel !== "now") document.body.dataset.panel = panel;
  go(VIEWS.includes(start) ? start : "now", { push: false });
  try {
    session = await api("/api/session");
    $("btn-logout").hidden = !(session.auth_required && session.user);
  } catch (e) { /* non-fatal */ }
  await refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
  setInterval(() => { if (!document.hidden && panelVisible("timeline")) loadTimeline(); }, TIMELINE_POLL_MS);
  setInterval(tick, 250);
}

init();
