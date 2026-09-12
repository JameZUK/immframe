/* immframe Settings page — edits config.yaml through /api/config.
 *
 * The form is generated from the schema the server sends (sections of
 * typed fields addressed by dotted path) plus a playlist builder. Saving
 * sends the whole config tree back: fields the form knows about are taken
 * from the inputs, everything else is carried over untouched from what was
 * loaded, so keys the form doesn't cover survive a save. The YAML tab edits
 * the file text directly. Secrets arrive masked and are only replaced when
 * a new value is typed. All DOM is built with createElement/textContent —
 * nothing from the server or the user is ever interpreted as HTML.
 */
"use strict";

const $ = id => document.getElementById(id);

let META = null;          // /api/config payload (schema, options, mask …)
let TREE = {};            // config tree as loaded (masked)
let PLAYLIST = [];        // working copy of selection.playlist
let activeTab = "form";

// ── Tree helpers ─────────────────────────────────────────────────────────

function getPath(tree, path) {
  return path.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), tree);
}

function setPath(tree, path, value) {
  const keys = path.split(".");
  let o = tree;
  for (const k of keys.slice(0, -1)) {
    if (typeof o[k] !== "object" || o[k] === null) o[k] = {};
    o = o[k];
  }
  const last = keys[keys.length - 1];
  if (value === undefined) delete o[last]; else o[last] = value;
}

function deepClone(x) { return JSON.parse(JSON.stringify(x)); }

function parseList(text) {
  return String(text || "").split(/[\s,]+/).map(s => s.trim()).filter(Boolean);
}

function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (k === "text") e.textContent = v;
    else e[k] = v;
  }
  for (const c of children) if (c != null) e.append(c);
  return e;
}

// ── Field rendering ──────────────────────────────────────────────────────

function fieldId(path) { return "f-" + path.replace(/\./g, "-"); }

function selectWithDefault(options, value) {
  const s = el("select");
  s.append(el("option", { value: "", text: "(default)" }));
  for (const opt of options) s.append(el("option", { value: String(opt), text: String(opt) }));
  s.value = value === undefined || value === null ? "" : String(value);
  return s;
}

function renderField(f, value) {
  const id = fieldId(f.path);
  const row = el("div", { class: "row" + (f.type === "bool" ? " toggle" : "") });
  row.append(el("label", { htmlFor: id, text: f.label }));

  let input;
  if (f.type === "bool") {
    input = el("input", { type: "checkbox", checked: value === true });
  } else if (f.type === "enum") {
    input = selectWithDefault(f.options, value);
  } else if (f.type === "list" && f.options) {
    // Checkbox set (overlay fields).
    input = el("div", { class: "checks" });
    const current = new Set(Array.isArray(value) ? value : parseList(value));
    for (const opt of f.options) {
      input.append(el("label", {}, el("input", { type: "checkbox", value: opt, checked: current.has(opt) }), " " + opt));
    }
  } else {
    const type = f.type === "secret" ? "password" : (f.type === "int" || f.type === "float") ? "number" : "text";
    input = el("input", { type });
    if (f.type === "int") input.step = "1";
    if (f.type === "float") input.step = "any";
    if (f.type === "secret") input.autocomplete = "new-password";
    input.value = Array.isArray(value) ? value.join(", ") : (value === undefined || value === null ? "" : String(value));
  }
  input.id = id;
  input.dataset.path = f.path;
  input.dataset.type = f.type;
  row.append(input);
  if (!f.help) return row;
  return el("div", { class: "field" }, row, el("div", { class: "hint", text: f.help }));
}

function readField(f) {
  const node = $(fieldId(f.path));
  if (!node) return undefined;
  if (f.type === "bool") return node.checked;
  if (f.type === "enum") {
    if (node.value === "") return undefined;
    return f.options.some(o => typeof o === "number") ? Number(node.value) : node.value;
  }
  if (f.type === "list" && f.options) {
    return [...node.querySelectorAll("input:checked")].map(cb => cb.value);
  }
  if (f.type === "list") {
    const items = parseList(node.value);
    return items.length ? items : undefined;
  }
  if (f.type === "int" || f.type === "float") {
    if (node.value.trim() === "") return undefined;
    const n = Number(node.value);
    return Number.isFinite(n) ? (f.type === "int" ? Math.trunc(n) : n) : undefined;
  }
  // str / secret: blank means "unset" (the packaged default applies).
  return node.value === "" ? undefined : node.value;
}

function renderSections() {
  const host = $("form-sections");
  host.replaceChildren();
  for (const section of META.schema) {
    const card = el("section", { class: "card" }, el("h2", { text: section.title }));
    for (const f of section.fields) {
      // Effective value (file merged over defaults) so an unset key shows
      // what actually runs; fall back to the raw file value.
      const eff = META.effective || {};
      const value = f.path in eff ? eff[f.path] : getPath(TREE, f.path);
      card.append(renderField(f, value));
    }
    host.append(card);
  }
}

// ── Playlist builder ─────────────────────────────────────────────────────

function entryOptionInput(opt, value) {
  let input;
  if (opt.type === "bool") {
    input = el("input", { type: "checkbox", checked: value === true });
  } else if (opt.type === "enum") {
    input = selectWithDefault(opt.options, value);
  } else {
    input = el("input", { type: opt.type === "int" ? "number" : "text", placeholder: opt.label });
    input.value = Array.isArray(value) ? value.join(", ") : (value === undefined ? "" : String(value));
  }
  input.dataset.key = opt.key;
  input.dataset.type = opt.type;
  input.title = opt.label;
  return input;
}

function renderPlaylist() {
  const host = $("playlist-rows");
  host.replaceChildren();
  PLAYLIST.forEach((entry, i) => {
    const row = el("div", { class: "pl-row", dataset: { index: String(i) } });

    const mode = el("select", { dataset: { field: "mode" } });
    for (const m of META.modes.filter(m => m !== "playlist")) mode.append(el("option", { value: m, text: m }));
    mode.value = entry.mode || "random";
    mode.addEventListener("change", () => { PLAYLIST[i] = collectEntry(row); renderPlaylist(); });

    const count = el("input", {
      type: "number", min: "1", value: entry.count ?? 25, dataset: { field: "count" },
      title: "slides (or collages) before the next entry",
    });

    const cb = el("input", { type: "checkbox", checked: entry.collage === true, dataset: { field: "collage" } });
    cb.addEventListener("change", () => { PLAYLIST[i] = collectEntry(row); renderPlaylist(); });
    const collage = el("label", { class: "pl-collage" }, cb, " collage");

    const opts = el("div", { class: "pl-opts" });
    for (const opt of META.playlist_entry_options[mode.value] || []) {
      opts.append(el("label", { text: opt.label + " " }, entryOptionInput(opt, entry[opt.key])));
    }
    if (cb.checked) {
      for (const opt of META.collage_entry_options) {
        opts.append(el("label", { text: opt.label + " " }, entryOptionInput(opt, entry[opt.key])));
      }
    }

    const tools = el("div", { class: "pl-tools" });
    for (const [txt, fn, title] of [
      ["▲", () => moveEntry(i, -1), "move up"],
      ["▼", () => moveEntry(i, 1), "move down"],
      ["✕", () => removeEntry(i), "remove"],
    ]) {
      const b = el("button", { type: "button", text: txt, title });
      b.addEventListener("click", fn);
      tools.append(b);
    }

    row.append(
      el("div", { class: "pl-top" }, el("span", { class: "pl-num", text: String(i + 1) }), mode, count, collage, tools),
      opts,
    );
    host.append(row);
  });
  if (!PLAYLIST.length) host.append(el("p", { class: "hint", text: "No entries — add one below." }));
}

function collectEntry(row) {
  const entry = {};
  entry.mode = row.querySelector('[data-field="mode"]').value;
  const c = Number(row.querySelector('[data-field="count"]').value);
  entry.count = Number.isFinite(c) && c > 0 ? Math.trunc(c) : 25;
  if (row.querySelector('[data-field="collage"]').checked) entry.collage = true;
  for (const node of row.querySelectorAll(".pl-opts [data-key]")) {
    const key = node.dataset.key, t = node.dataset.type;
    let v;
    if (t === "bool") v = node.checked ? true : undefined;
    else if (t === "enum") v = node.value === "" ? undefined : node.value;
    else if (t === "int") { v = node.value.trim() === "" ? undefined : Math.trunc(Number(node.value)); if (!Number.isFinite(v)) v = undefined; }
    else if (t === "list") { const items = parseList(node.value); v = items.length ? items : undefined; }
    else v = node.value === "" ? undefined : node.value;
    if (v !== undefined) entry[key] = v;
  }
  return entry;
}

function syncPlaylistFromDom() {
  PLAYLIST = [...document.querySelectorAll("#playlist-rows .pl-row")].map(collectEntry);
}

function moveEntry(i, d) {
  syncPlaylistFromDom();
  const j = i + d;
  if (j < 0 || j >= PLAYLIST.length) return;
  [PLAYLIST[i], PLAYLIST[j]] = [PLAYLIST[j], PLAYLIST[i]];
  renderPlaylist();
}

function removeEntry(i) {
  syncPlaylistFromDom();
  PLAYLIST.splice(i, 1);
  renderPlaylist();
}

// ── Assemble + save ──────────────────────────────────────────────────────

function buildTree() {
  const tree = deepClone(TREE);
  for (const section of META.schema) {
    for (const f of section.fields) {
      const v = readField(f);
      if (f.type === "secret" && v === undefined) continue;   // blank = keep (masked value round-trips)
      setPath(tree, f.path, v);
    }
  }
  syncPlaylistFromDom();
  setPath(tree, "selection.playlist", PLAYLIST.length ? PLAYLIST : undefined);
  return tree;
}

function msg(kind, text) {
  const node = $("save-msg");
  node.textContent = text; node.dataset.kind = kind; node.hidden = false;
}

function setConnection(status, text) {
  const node = $("connection"); node.dataset.status = status; node.textContent = text;
}

async function save(restart) {
  const body = activeTab === "yaml" ? { yaml: $("yaml").value } : { config: buildTree() };
  body.restart = restart;
  $("btn-save").disabled = $("btn-save-restart").disabled = true;
  msg("info", restart ? "Saving and restarting…" : "Saving…");
  try {
    const r = await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
    if (data.yaml) $("yaml").value = data.yaml;
    if (restart) {
      msg("ok", `Saved to ${data.path}. Restarting the slideshow — back in ~10 s.`);
      await waitForRestart();
    } else {
      msg("ok", `Saved to ${data.path} (previous file kept as ${data.backup}). Restart to apply.`);
      await load();
    }
  } catch (e) {
    msg("err", String(e.message || e));
  } finally {
    $("btn-save").disabled = $("btn-save-restart").disabled = false;
  }
}

async function waitForRestart() {
  setConnection("loading", "restarting");
  const started = Date.now();
  let sawDown = false;
  while (Date.now() - started < 60000) {
    await new Promise(r => setTimeout(r, 1500));
    try {
      const r = await fetch("/api/version", { cache: "no-store" });
      if (r.ok && (sawDown || Date.now() - started > 20000)) {
        setConnection("ok", "live");
        msg("ok", "Restarted with the new configuration.");
        await load();
        return;
      }
    } catch (e) { sawDown = true; }
  }
  setConnection("err", "offline");
  msg("err", "The slideshow did not come back within 60 s — check `journalctl -t immframe`.");
}

// ── Load ─────────────────────────────────────────────────────────────────

async function load() {
  const r = await fetch("/api/config", { cache: "no-store" });
  if (!r.ok) throw new Error(`GET /api/config -> ${r.status}`);
  META = await r.json();
  TREE = META.config || {};
  PLAYLIST = deepClone(getPath(TREE, "selection.playlist") || []);
  $("cfg-path").textContent = META.path + (META.exists ? "" : " (will be created)");
  $("yaml").value = META.yaml || "";
  renderPlaylist();
  renderSections();
  setConnection("ok", "live");
}

function wire() {
  for (const b of document.querySelectorAll(".tab")) {
    b.addEventListener("click", () => {
      activeTab = b.dataset.tab;
      for (const t of document.querySelectorAll(".tab")) t.dataset.active = String(t === b);
      $("tab-form").hidden = activeTab !== "form";
      $("tab-yaml").hidden = activeTab !== "yaml";
    });
  }
  $("playlist-add").addEventListener("click", () => {
    syncPlaylistFromDom();
    PLAYLIST.push({ mode: "random", count: 25 });
    renderPlaylist();
  });
  $("btn-save").addEventListener("click", () => save(false));
  $("btn-save-restart").addEventListener("click", () => save(true));
}

wire();
load().catch(e => { setConnection("err", "offline"); msg("err", String(e.message || e)); });
