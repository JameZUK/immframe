# Configuration reference

immframe reads a single YAML file. Search order:

1. `--config <path>` on the command line
2. `$IMMFRAME_CONFIG`
3. `~/.config/immframe/config.yaml`
4. `/etc/immframe/config.yaml`

Anything you don't set inherits from
[`src/immframe/_defaults/default.yaml`](../src/immframe/_defaults/default.yaml).

> **Secrets live inline in this file.** Always `chmod 600 ~/.config/immframe/config.yaml`.
> Any string value supports `${ENV_VAR}` substitution if you'd rather keep
> credentials out of the YAML — e.g. `api_key: ${IMMICH_API_KEY}`.

Top-level sections:

- [`immich`](#immich) — server URL + API key
- [`selection`](#selection) — what to show next
- [`video`](#video) — VLC / streaming
- [`viewer`](#viewer) — rendering, fonts, overlays, display
- [`control`](#control) — MQTT, HTTP, dashboard

---

## immich

| Key | Type | Default | Description |
|---|---|---|---|
| `url` | string | `https://immich.local` | Base URL of your Immich server. No trailing slash. immframe appends `/api`. |
| `api_key` | string | `""` (required) | Immich API key. Create in *Immich → Account Settings → API Keys → New API Key*. `${ENV}` expansion supported. |
| `timeout_s` | float | `10` | HTTP timeout (seconds) for every Immich call. |
| `image_size` | enum | `fullsize` | `preview` (~1440px on the long edge) or `fullsize` (original-resolution JPEG; Immich transcodes HEIC/RAW). `fullsize` is right for 4K displays. |

```yaml
immich:
  url: https://immich.example.local
  api_key: ${IMMICH_API_KEY}
  timeout_s: 15
```

---

## selection

| Key | Type | Default | Description |
|---|---|---|---|
| `default_mode` | enum | `random` | One of `random`, `album`, `smart`, `scene`, `people`, `memory`, `recent`, `playlist`. |
| `album_ids` | list[string] | `[]` | Album UUIDs to draw from when `default_mode = album`. Multiple albums are merged + shuffled. |
| `smart_query` | string | `""` | CLIP query when `default_mode = smart`. e.g. `"family at the beach"`. |
| `people_ids` | list[string] | `[]` | Person UUIDs to filter on when `default_mode = people`. Empty list = rotate through ALL named people in the library. Use `immframe list-people` to discover UUIDs. |
| `recent_days` | int | `30` | Window size for `default_mode = recent`. Photos uploaded (or taken — see `recent_field`) in the last N days. |
| `recent_field` | enum | `created` | `created` = "uploaded to Immich" (most users want this for "new photos"); `taken` = "captured by camera" (use for "trip from last month"). |
| `playlist` | list[dict] | `[]` | Used when `default_mode = playlist`. See [Playlist mode](#playlist-mode) below. |
| `prefetch_count` | int | `5` | How many slides to pre-download ahead of the renderer. Higher = smoother on slow networks, more temp-disk and RAM. |

### The selection modes

- **random** — `POST /api/search/random` across the whole library. Always something fresh.
- **album** — Random within one or more albums. Curated.
- **smart** — Immich's CLIP smart-search. Free-text. Requires the smart-search ML jobs to have run on your library.
- **scene** — Picks a random label that Immich has auto-discovered (*beach*, *Amsterdam*, *wedding*, …) and slideshows ~25 photos from it before rotating. **Multi-source with auto-fallback** in this priority order: `things` (CLIP scenes) → cities → curated CLIP queries. The curated fallback works whenever Immich's smart search is enabled, even when `/search/explore` doesn't surface anything useful.
- **people** — Slideshow of photos featuring specific people. With `people_ids` empty, rotates through every named person in the library (one person's photos at a time). With UUIDs set, restricts to those. Find UUIDs via `immframe list-people`.
- **memory** — On-this-day. Uses Immich's `/memories` endpoint (the same "5 years ago" feature on the Immich home screen). Picks a random memory and shows its photos, then rotates. Zero config.
- **recent** — Photos uploaded (or taken) recently. Configurable via `recent_days` and `recent_field`. Re-queries each rotation so newly-uploaded photos surface within minutes.
- **playlist** — Round-robins through a sequence of other modes with configurable batch sizes. See below.

### Playlist mode

Cycle through several modes per session:

```yaml
selection:
  default_mode: playlist
  playlist:
    - mode: random
      count: 25
    - mode: scene
      count: 25
    - mode: memory                  # On this day
      count: 5
    - mode: recent                  # Recently uploaded
      count: 10
      days: 7                       # override controller-level recent_days
    - mode: people
      count: 10
      people_ids:                   # override controller-level people_ids
        - "uuid-of-alice"
        - "uuid-of-bob"
```

Per entry:
- `mode` — required. Any selection mode except `playlist` (no nesting).
- `count` — how many slides to show before rotating to the next entry (default `25`).
- Mode-specific overrides — `album_ids`, `smart_query`, `people_ids`, `days`, `field`. If omitted, falls back to the controller-level config value.

The playlist rotates indefinitely. If a sub-selector returns nothing (e.g. `recent` finds no new uploads), playlist auto-advances to the next entry without stalling.

You can switch modes at runtime via MQTT, HTTP, the dashboard, or `immframe mode <mode>` — config just sets the starting mode.

```yaml
selection:
  default_mode: scene
  album_ids: []
  smart_query: ""
  people_ids: []
  recent_days: 30
  recent_field: created
  playlist: []
  prefetch_count: 5
```

---

## video

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Whether videos are included in selection batches. When `false`, videos are skipped at the prefetch layer. |
| `stream` | bool | `true` | When `true`, videos play straight from the Immich URL via MPV (no local file). When `false`, videos would be downloaded first — Phase-1 immframe only implements the streaming path. |
| `mute` | bool | `true` | MPV `mute` option. |
| `vo` | enum | `gpu` | MPV video output backend. `gpu` (the default — KMS/DRM on Pi, X11 GL elsewhere), `x11`, `drm`, or `sdl`. |
| `fit` | enum | `contain` | How video fits the screen. `contain` preserves aspect ratio and letterboxes/pillarboxes the gaps (no cropping). `cover` preserves aspect ratio but fills the whole screen, cropping the overflow — good for edge-to-edge playback on a 16:9 panel at the cost of clipping the edges. |
| `poster` | bool | `true` | When `true`, render the video's matted preview JPEG via pi3d first (same fade/blur/mat treatment as images), then hand off to MPV. When `false`, videos go straight to MPV fullscreen with no frame. |
| `poster_hold_s` | float | `3.0` | Seconds to hold the matted poster before MPV starts. |
| `live_photo_hold_s` | float | `1.0` | Seconds to hold the still before playing a Live Photo's motion clip. |
| `rotate` | enum | `auto` | Override MPV's video rotation: `auto` (honor container rotation tag — the default), `no` (disable rotation entirely), or `0`/`90`/`180`/`270` to force a clockwise rotation. Phone-shot portrait videos rotate correctly under `auto` because their container metadata carries the rotation. |
| `fullscreen` | bool | `false` | Whether MPV requests its own fullscreen. **Leave `false` on the labwc kiosk** — the compositor is configured to fullscreen every window, and a *second* fullscreen request from MPV makes labwc's `ToggleFullscreen` rule toggle it back to a tiny default-size window (the "video plays in a small window" symptom). Set `true` only when running MPV under a compositor that does **not** auto-fullscreen windows (e.g. a regular desktop or X11 session). |
| `max_play_s` | float | `60.0` | Hard cap per video in seconds. Slideshow advances even if MPV hasn't reported EOF (bad codec, network stall, etc.). |

```yaml
video:
  enabled: true
  stream: true
  mute: true
```

---

## viewer

Carries the picframe-style viewer config straight through to the vendored
`viewer/display.py`. The full picframe wiki lists every key
([Configuration](https://github.com/helgeerbe/picframe/wiki/Configuration));
the table below covers the ones you're most likely to want.

### Rendering

| Key | Type | Default | Description |
|---|---|---|---|
| `time_delay` | float | `60` | Seconds per slide. Bigger = slower slideshow. Runtime-tunable via HA / dashboard / `immframe delay`. |
| `fade_time` | float | `4` | Seconds for the crossfade between slides. `0` = jump-cut. Must be `< time_delay`. |
| `fps` | float | `20.0` | Render-loop target frames per second. |
| `background` | list[float] | `[0.2, 0.2, 0.3, 1.0]` | RGBA fill colour around an image when it doesn't fill the screen. |
| `blend_type` | enum | `blend` | Shader blend mode: `blend`, `burn`, `bump`. |
| `shader` | string | (bundled `blend_new`) | Path to a custom GLSL shader (without `.fs`/`.vs`). |

### Fit, blur, Ken Burns

| Key | Type | Default | Description |
|---|---|---|---|
| `fit_to_screen` / `fit` | bool | `true` | `true` = scale image so it's fully visible (may leave bars). `false` = crop to fill. |
| `blur_edges` | bool | `true` | When the image doesn't fill the screen, fill the gaps with a blurred copy of the image (vs flat colour). |
| `blur_amount` | int | `12` | Gaussian blur strength for `blur_edges`. Higher = softer + more CPU. |
| `blur_zoom` | float | `1.0` | Zoom factor for the blurred background. Must be ≥ 1.0. |
| `edge_alpha` | float | `0.5` | Opacity of the edge fill (0.0 transparent → 1.0 full). |
| `kenburns` | bool | `false` | Slow pan/zoom during each slide. Forces `fit = false` and `blur_edges = false`. |
| `video_fit_display` | bool | `false` | Stretch video to fill the screen (may distort) vs preserve aspect with bars. |

### Matting (frame within a frame)

| Key | Type | Default | Description |
|---|---|---|---|
| `mat_images` | bool / float | `true` | `true` = mat every image. `false` = never. A float ≥ 0.0 = mat only when image-vs-screen aspect difference exceeds this value. |
| `mat_type` | string | `null` | Space-separated style list: any of `float`, `float_polaroid`, `float_color_wrap`, `single_bevel`, `double_bevel`, `double_flat`. `null` = use all. |
| `outer_mat_color` | list[int] | `null` | RGB `[r, g, b]` for the outer mat. `null` = auto-pick from the image. |
| `inner_mat_color` | list[int] | `null` | RGB for the inner mat (styles that have one). `null` = auto. |
| `outer_mat_border` | int | `75` | Minimum outer mat border in pixels. |
| `inner_mat_border` | int | `40` | Minimum inner mat border (for relevant styles). |
| `outer_mat_use_texture` | bool | `true` | Use a paper-style texture vs flat colour. |
| `inner_mat_use_texture` | bool | `false` | Texture on the inner mat. |
| `mat_resource_folder` | path | (bundled) | Where mat textures + 9-patches live. Default ships with the wheel. |

### Overlay text

immframe overlays metadata text over each slide. The fields are a bitmask
stored in `show_text` and individually documented below.

| Key | Type | Default | Description |
|---|---|---|---|
| `show_text` | space-string | `"title caption name date location"` | Which fields to show (see [Overlay fields](#overlay-fields)). Space-separated; runtime-tunable. |
| `show_text_tm` | float | `20` | Seconds to keep the overlay visible after a slide change. `0` = always on. |
| `show_text_sz` | int | `40` | Font size in pixels. **Bump this for bigger text.** |
| `show_text_fm` | string | `"%b %-d, %Y"` | `strftime` format for the date field. |
| `text_justify` | enum | `L` | `L`/`C`/`R` — left, centre, right. |
| `text_x_margin` | int | `100` | Horizontal pixels in from the screen edge. |
| `text_y_margin` | int | `0` | Additional vertical margin (relative to bottom). Negative pushes text down. |
| `text_bkg_hgt` | float | `0.25` | Fraction of screen height occupied by the dark background strip behind the text (0.0 to 1.0). |
| `text_opacity` | float | `1.0` | Overlay text alpha (0.0 invisible → 1.0 full). |
| `font_file` | path | (bundled Noto Sans) | TTF path. Default ships with the wheel. |

#### Overlay fields

`show_text` is a space-separated list (or runtime-friendly CSV). Each
field appears on its own line in the overlay when its data is present.

| Key | Renders when | Content |
|---|---|---|
| `caption` | Asset has `exifInfo.description` | Caption / description from the photo's EXIF metadata. |
| `name` | Always | Original filename of the asset (e.g. `IMG_0042.jpg`) — read from Immich's `originalFileName`. |
| `date` | Asset has a `localDateTime` or `fileCreatedAt` | Photo taken date, formatted via `show_text_fm`. |
| `location` | Any of city / state / country present in EXIF | "City, State, Country" — pulled from Immich's reverse-geocoded EXIF data. |
| `people` | Immich has named, non-hidden people tagged in the asset | Comma-separated names from face recognition. Skips unnamed face clusters and people marked hidden in Immich. |
| `tags` | Asset has user-defined tags assigned in Immich | Comma-separated tag values. Immich does **not** auto-generate CLIP-detected tags — these are tags you (or Immich's importer) assigned explicitly. |
| `ocr` | Image contains visible text that Immich's OCR job extracted | Comma-separated text strings. Use for "what's in the image" on screenshots / signs / documents. Costs one extra HTTP round-trip per slide while enabled. |
| `title` | (legacy, kept for picframe-config compat) | Reserved — Immich has no separate title field. Never renders. Don't enable. |
| `folder` | (legacy, kept for picframe-config compat) | Shows the cache tempdir under immframe (meaningless). Don't enable. |

You can also toggle these at runtime — via HA's `text.immframe_show_text`,
`POST /api/show_text` (JSON list), the dashboard checkboxes, or
`immframe show-text title,date,location,people`.

### Clock overlay

| Key | Type | Default | Description |
|---|---|---|---|
| `show_clock` | bool | `false` | Show a live clock on screen. Runtime-tunable via HA / `immframe clock on`. |
| `clock_format` | string | `"%-I:%M"` | `strftime` format. e.g. `"%H:%M"` for 24-hour. |
| `clock_text_sz` | int | `120` | Clock font size in pixels. |
| `clock_justify` | enum | `R` | `L`/`C`/`R`. |
| `clock_top_bottom` | enum | `T` | `T` (top) or `B` (bottom). |
| `clock_wdt_offset_pct` | float | `3.0` | Horizontal offset as % of screen width (1.0–10.0). |
| `clock_hgt_offset_pct` | float | `3.0` | Vertical offset as % of screen height (1.0–10.0). |
| `clock_opacity` | float | `1.0` | Alpha 0.0–1.0. |

If the file `/dev/shm/clock.txt` exists, its contents appear under the
time as a free-form caption (handy for HA template sensors writing to
ramdisk).

### Display + brightness

| Key | Type | Default | Description |
|---|---|---|---|
| `brightness` | float | `1.0` | Initial brightness 0.0–1.0. Runtime-tunable via HA / dashboard / `immframe brightness`. |
| `display_x` / `display_y` | int | `0` | Top-left offset of the pi3d window. Negative values allowed. |
| `display_w` / `display_h` | int or null | `null` | Force a specific render resolution. `null` = use whatever the hardware reports. |
| `use_glx` | bool | `false` | Force GLX on X servers. Set `false` on a console / KMS setup. |
| `use_sdl2` | bool | `true` | Use pi3d's SDL2 display backend. Required when there's no X server. |
| `display_hdmi` | string | `HDMI-A-1` | Which HDMI port to address for power control. `HDMI-A-2` for the second port on a Pi 4/5. |
| `display_power` | enum 0–3 | `0` | How to turn the display on/off: `0` = `vcgencmd` (legacy Pi), `1` = `xset` (X server), `2` = `wlr-randr` (Wayland), `3` = write to `/sys/class/drm/card?-<hdmi>/status` (works for multi-screen). |

### Miscellaneous

| Key | Type | Default | Description |
|---|---|---|---|
| `geo_suppress_list` | list[string] | `[]` | Substrings to strip out of the `location` overlay (e.g. `["United Kingdom"]`). |
| `menu_text_sz` | int | `40` | Size of the on-frame menu (unused by immframe — no on-device input). |
| `menu_autohide_tm` | float | `0.0` | Same — unused. `0.0` disables the menu code path. |

---

## control

Each sub-section is independently toggled. The frame has no on-device input
(no keyboard, mouse, or touch), so enable at least one of MQTT or HTTP.

### control.mqtt

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Wire MQTT + HA discovery. |
| `host` | string | `homeassistant.local` | Broker hostname. |
| `port` | int | `1883` | Broker port. |
| `user` | string | `""` | Username. Empty = no auth. |
| `password` | string | `""` | Password. `${ENV}` expansion supported. |
| `base_topic` | string | `immframe` | Topic prefix. Also used as the HA device identifier — change if running multiple frames against the same broker. |

When `enabled`, HA auto-discovers the device. Entity list and a sample
Lovelace card live in [home-assistant.md](./home-assistant.md).

```yaml
control:
  mqtt:
    enabled: true
    host: homeassistant.local
    port: 1883
    user: immframe
    password: ${MQTT_PASSWORD}
    base_topic: immframe
```

### control.http

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Bind the REST API + dashboard. |
| `bind` | string | `127.0.0.1` | Bind address. Set `0.0.0.0` to expose on the LAN (only ever with `auth: true`). |
| `port` | int | `8080` | TCP port. |
| `auth` | bool | `true` | Require Basic auth. Strongly recommended; the credentials gate the dashboard and all `/api/*` endpoints. |
| `username` | string | `""` | Basic-auth username. |
| `password` | string | `""` | Basic-auth password. `${ENV}` expansion supported. |

`/healthz` is exempt from auth so monitoring tools can probe. Everything else
(including the dashboard SPA and the image proxy) requires auth when
`auth: true`.

```yaml
control:
  http:
    enabled: true
    bind: 0.0.0.0
    port: 8080
    auth: true
    username: admin
    password: ${IMMFRAME_HTTP_PW}
```

---

## How to add a new overlay field

If you want to extend `show_text` with a new field of your own (e.g.
`exposure` showing camera settings), four files need an edit:

1. `src/immframe/controller.py` — add the key to `SHOW_TEXT_KEYS`.
   The HTTP validator imports this; the dashboard reads it at runtime
   via `/api/state`.
2. `src/immframe/viewer/display.py` — add a `key: bit` entry to
   `_SHOW_TEXT_BITS` (use the next power of 2), and add the rendering
   branch in `__make_text`.
3. `src/immframe/controller.py` — extend `Pic.__init__` to populate
   whatever attribute your viewer branch reads (`pic.exposure`).
4. `src/immframe/web/index.html` — add a `<label>` with `data-st-key`.

If the data needs to come from Immich, also extend `Asset` in
`src/immframe/immich/models.py` and `_to_asset` in
`src/immframe/immich/client.py` to extract the JSON field.
