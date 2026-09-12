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
  // Content-Type marks this as a scripted request; the server rejects
  // form-encoded POSTs (CSRF guard) and this keeps us on the right side.
  const r = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  if (!r.ok) throw new Error(`POST ${endpoint} -> ${r.status}`);
}

// POST with an optional JSON body (null = none) and return the parsed JSON
// reply, surfacing the server's `error` text on failure so the user sees
// *why* (e.g. the key lacks asset.update).
async function postValueRaw(endpoint, body) {
  const r = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === null ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error((data && data.error) || `${endpoint} -> ${r.status}`);
  return data;
}

let curateTimer = null;
function curateMsg(kind, text) {
  const el = $("curate-msg");
  el.textContent = text;
  el.dataset.kind = kind;
  el.hidden = false;
  if (curateTimer) clearTimeout(curateTimer);
  curateTimer = setTimeout(() => { el.hidden = true; }, 6000);
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

    // All modes that carry a rotating label
    const showLabel = ["favorites", "scene", "people", "memory", "recent", "playlist"].includes(state.selection_mode);
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

    // Collage
    const col = state.collage || {};
    const colOn = !!col.enabled;
    $("collage-enabled").checked = colOn;
    $("collage-layout").value = col.layout || "auto";
    $("collage-min-tiles").value = col.min_tiles ?? 3;
    $("collage-min-tiles-value").textContent = String(col.min_tiles ?? 3);
    $("collage-max-tiles").value = col.max_tiles ?? 6;
    $("collage-max-tiles-value").textContent = String(col.max_tiles ?? 6);
    // The layout/tile controls only matter when collage is on.
    ["collage-layout", "collage-min-tiles", "collage-max-tiles"].forEach(id => {
      $(id).disabled = !colOn;
    });

    // Current asset
    const a = state.current_asset;
    if (a) {
      if (a.id !== lastAssetId) {
        // Cache-bust by ID change; the server already sends Cache-Control: no-store.
        // Collages are synthetic (no Immich asset) — load the composited file
        // from /api/current_image instead of the image proxy.
        $("current-image").src = a.is_collage
          ? `/api/current_image?v=${encodeURIComponent(a.id)}`
          : `/api/image/${encodeURIComponent(a.id)}`;
        $("current-image").style.display = "";
        $("image-placeholder").style.display = "none";
        lastAssetId = a.id;
      }
      // Curation buttons: collages are synthetic (nothing to star/hide).
      $("btn-favorite").disabled = !!a.is_collage;
      $("btn-hide").disabled = !!a.is_collage;
      $("btn-favorite").dataset.active = String(!!a.favorite);
      $("btn-favorite").textContent = a.favorite ? "♥ Favourited" : "♡ Favourite";
      $("meta-file").textContent = a.file || "—";
      $("meta-date").textContent = a.taken_at ? a.taken_at.replace("T", " ").slice(0, 19) : "—";
      $("meta-where").textContent = [a.city, a.country].filter(Boolean).join(", ") || "—";
      $("meta-camera").textContent = a.camera || "—";
      $("meta-kind").textContent = a.kind || "—";
      const pa = state.pair_asset;
      $("meta-pair").textContent = pa ? `with ${pa.file || pa.id}` : "—";
    } else {
      $("btn-favorite").disabled = true;
      $("btn-hide").disabled = true;
      $("current-image").style.display = "none";
      $("image-placeholder").style.display = "flex";
      ["meta-file", "meta-date", "meta-where", "meta-camera", "meta-kind", "meta-pair"].forEach(id => {
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

  $("btn-favorite").addEventListener("click", async () => {
    // Empty body = toggle. Response is the state snapshot with the new ♥.
    try {
      const next = await postValueRaw("/api/favorite", null);
      if (next) render(next);
      curateMsg("ok", next && next.current_asset && next.current_asset.favorite
        ? "Starred in Immich" : "Un-starred in Immich");
    } catch (e) {
      curateMsg("err", String(e.message || e));
    }
  });

  $("btn-hide").addEventListener("click", async () => {
    try {
      const r = await postValueRaw("/api/hide", null);
      curateMsg(r && r.archived ? "ok" : "warn",
        r && r.archived
          ? "Hidden and archived in Immich — moving on"
          : `Hidden on the frame (Immich archive failed: ${r && r.error ? r.error : "?"})`);
      await refresh();
    } catch (e) {
      curateMsg("err", String(e.message || e));
    }
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

  $("collage-enabled").addEventListener("change", () => {
    if (suppressInput) return;
    safePost("/api/collage_enabled", $("collage-enabled").checked);
  });

  $("collage-layout").addEventListener("change", () => {
    if (suppressInput) return;
    safePost("/api/collage_layout", $("collage-layout").value);
  });

  const collageMinCommit = debounce(() => {
    if (suppressInput) return;
    safePost("/api/collage_min_tiles", parseInt($("collage-min-tiles").value, 10));
  }, DEBOUNCE_MS);
  $("collage-min-tiles").addEventListener("input", () => {
    $("collage-min-tiles-value").textContent = String($("collage-min-tiles").value);
    collageMinCommit();
  });

  const collageMaxCommit = debounce(() => {
    if (suppressInput) return;
    safePost("/api/collage_max_tiles", parseInt($("collage-max-tiles").value, 10));
  }, DEBOUNCE_MS);
  $("collage-max-tiles").addEventListener("input", () => {
    $("collage-max-tiles-value").textContent = String($("collage-max-tiles").value);
    collageMaxCommit();
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
