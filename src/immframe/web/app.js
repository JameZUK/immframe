// Vanilla-JS dashboard. Polls /api/state every 5s, posts on user input.
// Auth is handled by the browser via the WWW-Authenticate prompt on first
// request — no token handling in JS.

const POLL_INTERVAL_MS = 5000;
const DEBOUNCE_MS = 250;

const $ = (id) => document.getElementById(id);

let lastAssetId = null;
let suppressInput = false;    // ignore input events while we're populating from state

// ── HTTP helpers ───────────────────────────────────────────────────────────

async function getState() {
  const r = await fetch("/api/state", { cache: "no-store" });
  if (!r.ok) throw new Error(`GET /api/state -> ${r.status}`);
  return r.json();
}

async function getVersion() {
  const r = await fetch("/api/version", { cache: "no-store" });
  if (!r.ok) throw new Error(`GET /api/version -> ${r.status}`);
  return r.json();
}

async function postValue(endpoint, value) {
  const r = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
  if (!r.ok) throw new Error(`POST ${endpoint} -> ${r.status}: ${await r.text()}`);
  return r.json().catch(() => null);
}

async function postCommand(endpoint) {
  const r = await fetch(endpoint, { method: "POST" });
  if (!r.ok) throw new Error(`POST ${endpoint} -> ${r.status}`);
}

// ── Connection badge ──────────────────────────────────────────────────────

function setConnection(status, text) {
  const el = $("connection");
  el.dataset.status = status;
  el.textContent = text;
}

// ── State rendering ───────────────────────────────────────────────────────

function render(state) {
  suppressInput = true;
  try {
    $("btn-pause").textContent = state.paused ? "Resume" : "Pause";
    $("btn-pause").dataset.active = String(state.paused);

    $("mode").value = state.selection_mode;
    $("album-ids").value = (state.album_ids || []).join(", ");
    $("smart-query").value = state.smart_query || "";
    $("people-ids").value = (state.people_ids || []).join(", ");

    // For scene and people modes, surface the current label
    const showLabel = state.selection_mode === "scene" || state.selection_mode === "people";
    $("scene-row").hidden = !showLabel;
    if (showLabel) {
      $("current-scene").textContent = state.current_scene || "(loading)";
    }

    $("brightness").value = state.brightness;
    $("brightness-value").textContent = Number(state.brightness).toFixed(2);

    $("time-delay").value = state.time_delay;
    $("time-delay-value").textContent = `${Math.round(state.time_delay)}s`;

    $("fade-time").value = state.fade_time;
    $("fade-time-value").textContent = `${Number(state.fade_time).toFixed(1)}s`;

    $("display-is-on").checked = !!state.display_is_on;
    $("show-clock").checked = !!state.show_clock;

    const active = new Set(state.show_text || []);
    document.querySelectorAll('input[data-st-key]').forEach(cb => {
      cb.checked = active.has(cb.dataset.stKey);
    });

    // Current asset
    const a = state.current_asset;
    if (a) {
      if (a.id !== lastAssetId) {
        // Cache-bust by ID change; the server already sends Cache-Control: no-store
        $("current-image").src = `/api/image/${encodeURIComponent(a.id)}`;
        $("current-image").style.display = "";
        $("image-placeholder").style.display = "none";
        lastAssetId = a.id;
      }
      $("meta-file").textContent = a.file || "—";
      $("meta-date").textContent = a.taken_at ? a.taken_at.replace("T", " ").slice(0, 19) : "—";
      $("meta-where").textContent = [a.city, a.country].filter(Boolean).join(", ") || "—";
      $("meta-camera").textContent = a.camera || "—";
      $("meta-kind").textContent = a.kind || "—";
    } else {
      $("current-image").style.display = "none";
      $("image-placeholder").style.display = "flex";
      ["meta-file", "meta-date", "meta-where", "meta-camera", "meta-kind"].forEach(id => {
        $(id).textContent = "—";
      });
      lastAssetId = null;
    }
  } finally {
    suppressInput = false;
  }
}

// ── Polling ───────────────────────────────────────────────────────────────

async function refresh() {
  try {
    const state = await getState();
    render(state);
    setConnection("ok", "live");
  } catch (e) {
    setConnection("err", "offline");
    console.warn(e);
  }
}

// ── Event wiring ──────────────────────────────────────────────────────────

function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function showTextValues() {
  return [...document.querySelectorAll('input[data-st-key]:checked')].map(cb => cb.dataset.stKey);
}

async function safePost(endpoint, value) {
  try {
    const next = await postValue(endpoint, value);
    if (next) render(next);
  } catch (e) {
    console.error(e);
    setConnection("err", "error");
  }
}

function wire() {
  $("btn-pause").addEventListener("click", async () => {
    const paused = $("btn-pause").dataset.active === "true";
    await safePost("/api/paused", !paused);
  });

  $("btn-next").addEventListener("click", async () => {
    try { await postCommand("/api/next"); }
    catch (e) { console.error(e); }
  });

  $("mode").addEventListener("change", () => {
    if (suppressInput) return;
    safePost("/api/selection_mode", $("mode").value);
  });

  const albumIdsCommit = debounce(() => {
    if (suppressInput) return;
    const ids = $("album-ids").value.split(",").map(s => s.trim()).filter(Boolean);
    safePost("/api/album_ids", ids);
  }, DEBOUNCE_MS);
  $("album-ids").addEventListener("input", albumIdsCommit);

  const smartCommit = debounce(() => {
    if (suppressInput) return;
    safePost("/api/smart_query", $("smart-query").value);
  }, DEBOUNCE_MS);
  $("smart-query").addEventListener("input", smartCommit);

  const peopleIdsCommit = debounce(() => {
    if (suppressInput) return;
    const ids = $("people-ids").value.split(",").map(s => s.trim()).filter(Boolean);
    safePost("/api/people_ids", ids);
  }, DEBOUNCE_MS);
  $("people-ids").addEventListener("input", peopleIdsCommit);

  const brightnessCommit = debounce(() => {
    if (suppressInput) return;
    safePost("/api/brightness", parseFloat($("brightness").value));
  }, DEBOUNCE_MS);
  $("brightness").addEventListener("input", () => {
    $("brightness-value").textContent = Number($("brightness").value).toFixed(2);
    brightnessCommit();
  });

  const timeDelayCommit = debounce(() => {
    if (suppressInput) return;
    safePost("/api/time_delay", parseFloat($("time-delay").value));
  }, DEBOUNCE_MS);
  $("time-delay").addEventListener("input", () => {
    $("time-delay-value").textContent = `${Math.round($("time-delay").value)}s`;
    timeDelayCommit();
  });

  const fadeTimeCommit = debounce(() => {
    if (suppressInput) return;
    safePost("/api/fade_time", parseFloat($("fade-time").value));
  }, DEBOUNCE_MS);
  $("fade-time").addEventListener("input", () => {
    $("fade-time-value").textContent = `${Number($("fade-time").value).toFixed(1)}s`;
    fadeTimeCommit();
  });

  $("display-is-on").addEventListener("change", () => {
    if (suppressInput) return;
    safePost("/api/display_is_on", $("display-is-on").checked);
  });

  $("show-clock").addEventListener("change", () => {
    if (suppressInput) return;
    safePost("/api/show_clock", $("show-clock").checked);
  });

  document.querySelectorAll('input[data-st-key]').forEach(cb => {
    cb.addEventListener("change", () => {
      if (suppressInput) return;
      safePost("/api/show_text", showTextValues());
    });
  });
}

// ── Init ──────────────────────────────────────────────────────────────────

(async function init() {
  wire();
  try {
    const v = await getVersion();
    $("version").textContent = v.version || "?";
  } catch (e) {
    $("version").textContent = "?";
  }
  await refresh();
  setInterval(refresh, POLL_INTERVAL_MS);
})();
