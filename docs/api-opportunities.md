# What else the Immich API offers

After probing a live Immich v2.7.5 instance to fix the scene-mode bug, here's
everything I noticed that we could build on. Each item lists what it'd add,
roughly how much code, and where the data comes from. Pick the ones that
sound worth doing.

## Selection modes we could add

### Favourites mode 🟢 *easy*

Show only photos you've starred in Immich. People curate their best
photos this way; perfect for a frame.

- **API:** `POST /search/metadata` with `isFavorite: true`
- **Code:** new `FavouritesSelector` (~30 lines) + the usual controller / MQTT /
  HTTP / SPA wiring.
- **Bonus:** the frame's MQTT/HA gets a "favourite this" button that
  flips the current asset's `isFavorite` flag via `PUT /assets/{id}`.

### On-This-Day mode 🟢 *easy*

For each slide, look back at the same day in previous years. A
sentimental classic.

- **API:** `GET /memories` (Immich already aggregates these) or
  `/search/metadata` with `takenAfter` / `takenBefore` spanning the
  current day across years.
- **Code:** new `MemorySelector` (~80 lines).

### Highly-rated mode 🟢 *easy*

Immich supports a 1–5 star rating that you can set on assets. This mode
only shows ≥N stars.

- **API:** `POST /search/metadata` with `rating: N` (Immich filters
  greater-than-or-equal).
- **Code:** ~30 lines selector + 1 config knob.

### Recent uploads mode 🟢 *easy*

Show only photos uploaded (or taken) in the last *N* days.

- **API:** `POST /search/metadata` with `createdAfter` /
  `takenAfter` (we already plumb takenAfter).
- **Code:** ~30 lines + a config knob.

### Tags mode 🟡 *medium*

Immich has user-defined tags (different from CLIP "things"). This mode
rotates through tagged photos.

- **API:** `GET /tags` lists them; `POST /search/metadata` with `tagIds`.
- **Code:** ~60 lines (like albums, but for tags). Also a
  `immframe list-tags` CLI command to make the UUIDs discoverable.

### Live photos mode 🟡 *medium*

Only motion photos (the Apple-style "live photo" pairs Immich detects).

- **API:** `POST /search/metadata` with `isMotion: true`. The asset will
  have a `livePhotoVideoId` referencing the matching video, which the
  player could show as a 3-second motion clip instead of a still.
- **Code:** ~50 lines plus an MPV-handoff for the motion clip.

### Camera / lens mode 🟡 *medium*

Rotate through photos taken with a specific camera body or lens —
useful if you want a frame dedicated to one of your cameras.

- **API:** `GET /search/suggestions?type=camera-make` /
  `camera-model` / `camera-lens-model` to list available; then
  `POST /search/metadata` with `make` / `model` / `lensModel`.
- **Code:** ~50 lines.

### "More like this" mode 🟠 *experimental*

Pick a photo, then keep showing visually-similar ones. Useful when you
spot a slide you like and want to dwell on the theme.

- **API:** `POST /search/smart` accepts `queryAssetId` — searches for
  CLIP-similar photos.
- **Code:** ~50 lines, plus a "more like this" button in the dashboard /
  HA.

## Overlay fields we could add

These would join the existing `title | caption | name | date | location | folder | people` set.

| Key | Source | What it'd show |
|---|---|---|
| `exif` | exifInfo (fNumber / exposureTime / iso / focalLength) | `f/2.8 1/250s ISO 400 35mm` |
| `lens` | exifInfo.lensModel | Lens name |
| `rating` | exifInfo.rating | `★★★★☆` |
| `favorite` | isFavorite | Heart icon when starred |
| `tags` | tags[].value | Comma-separated user-defined tags |
| `kind` | type | `📷 IMAGE` / `🎬 VIDEO` (rarely useful, but possible) |
| `size` | exifInfo.fileSizeInByte | `12.4 MB` |

Adding any of these is the four-file recipe in
[configuration.md → "How to add a new overlay field"](./configuration.md#how-to-add-a-new-overlay-field).

## Other display upgrades

### Thumbhash placeholders 🟡 *medium*

`thumbhash` is a tiny base64 string each asset carries — Immich uses it
for blurry loading placeholders on the web UI. We could decode it
client-side and show a low-res blurred image while the full preview
downloads — eliminates the brief blank frame during a slow network blip.

- **Cost:** adds a thumbhash decoder (~150 lines) and a pi3d two-pass
  draw.

### Per-asset display duration 🟡 *medium*

Show landscapes longer than portraits; reduce dwell time on screenshots
or duplicates.

- **Heuristic** based on width/height/file_size or even Immich's rating.
- **Code:** ~30 lines in the controller's main loop.

### Live photo playback 🟠 *experimental*

When an image asset has `livePhotoVideoId`, play that 2–3s motion clip
via MPV after the still has been visible for a moment. Subtle and
beautiful.

- **Code:** ~80 lines coordinating viewer ↔ MPV.

## Infrastructure / observability

### Immich statistics MQTT sensor 🟢 *easy*

Surface library totals (asset count, video count, classified count,
named people, etc.) as a read-only HA sensor — useful for dashboards or
ML-job progress monitoring.

- **API:** `GET /assets/statistics`, `GET /server/statistics`.
- **Code:** ~50 lines + one MQTT entity.

### Webhook / SSE on new uploads 🟡 *medium*

Immich emits webhooks / supports SSE for asset events. We could
auto-bias the slideshow toward freshly-uploaded photos for a few hours
after they land.

- **API:** Webhook endpoint (Immich → us) or SSE subscription.
- **Code:** ~100 lines + a small HTTP endpoint to receive the webhook.

### Album discovery sensor 🟢 *easy*

Like the people list, surface album UUIDs+names through a sensor or CLI
command so HA users can find their album IDs without curl.

- **API:** `GET /albums`.
- **Code:** ~10 lines for the `immframe list-albums` CLI; already
  available — just hasn't been added.

## Things we should **not** do

- `original` file fetch. Immich blocks it at the thumbnail endpoint
  ("May not request original file") and the direct endpoint can return
  RAW / HEIC / video formats that pi3d won't render. `fullsize` is the
  right ceiling.
- `withStacked: true`. Returns multiple shots per stack — would mean
  showing very similar photos back-to-back. The default (one
  representative per stack) is better for a slideshow.
- Caching original-resolution JPEGs on disk for offline use. Pi 4/5 SD
  cards are slow and small; the network round-trip is faster.

---

## My picks if you want a quick win

The two highest-value, lowest-cost additions:

1. **Favourites mode** — most users already maintain a "best of" set
   in Immich.
2. **Overlay `exif` field** — camera nerds love seeing the settings
   alongside the photo.

Both are sub-30 lines each. Want me to do them?
