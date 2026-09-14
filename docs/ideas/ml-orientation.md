# Idea: ML orientation detection 🟠 *parked*

*Status: parked (2026-09-14). Recommendation agreed, not started.*

## The problem

Immich never *infers* orientation — it only honours metadata: the EXIF
`Orientation` tag for photos (applied when it generates previews) and the
container rotation / display matrix for videos (applied by ffmpeg when it
transcodes, and by mpv when the frame plays an original). None of its ML
jobs (CLIP, faces, OCR, duplicates) touch orientation.

So the photos that show sideways on the frame are the ones with *missing
or wrong* metadata: WhatsApp forwards (EXIF stripped — the "WhatsApp
Images" album alone is ~8,200 assets), old scans, screenshots, early
phone videos. Any detector is only for those, and must never argue with a
photo whose tag is present and right. Gate on assets whose
`exifInfo.orientation` is missing or `1`.

Immich 3.x has a non-destructive editor (`PUT /assets/{id}/edits`, actions
`rotate` / `crop` / `mirror`; permissions `asset.edit.get/create/delete`)
— the frame's **↻ Rotate** button already drives it. Videos cannot be
edited in Immich.

## Detection options

| | How | Verdict |
|---|---|---|
| **A. 4-class orientation CNN** (RotNet-style) | Classify a downscaled image into 0/90/180/270. Best trained on the user's own library: take N photos Immich shows upright, synthesise the four rotations (labels for free), fine-tune MobileNetV3-small, export ONNX. ~95 %+ on ordinary photos; ambiguous subjects (sky, sea, top-down food, abstract, documents) are where it — and humans — can't tell. | **Best.** ≈5 MB, 30–80 ms/image on a Pi 4 CPU with onnxruntime, trains in under an hour on the GPU box. |
| **B. Face geometry** | Faces + landmarks: eyes-above-mouth gives orientation. Immich's `/faces` returns boxes only, so a landmark model (BlazeFace-class, tiny) is needed locally. | Very reliable where there is a face (~40 k assets carry people). Good *complement* to A. |
| **C. CLIP zero-shot** using Immich's embeddings ("an upright photo" vs "rotated") | No new models. | CLIP is only weakly rotation-aware; too unreliable for auto-correction. Skip. |
| **D. OCR text angle** | PP-OCR boxes reveal rotated text. | Screenshots / documents only. Niche. |
| **E. Cloud vision APIs** | — | Ships photos out. No. |

**Recommendation: A, gated by B where a face exists, with a confidence
threshold.**

## Where it runs

- **On the Pi, in the prefetch pipeline** — the preview is already
  downloaded before it is shown; classify it there, off the render thread.
  The Pi 4 has the headroom (2 GB RAM, ~850 MB used; onnxruntime + model
  ≈ +60 MB).
- **As a crawler** — `immframe orient-scan` over the library in the
  background; the same code runs from a PC or the GPU box (needs only the
  API key), which is where a full-library pass belongs.

## What to do with a detection — the real design decision

Auto-editing ~99 k assets on a ~95 %-accurate model means thousands of
wrong edits, non-destructive or not. In increasing order of trust:

1. **Display-only fix** (safe, default): rotate the cached preview locally
   before showing it; log it; write nothing to Immich.
2. **Suggestion queue**: record every confident non-zero detection; a
   dashboard *"Looks rotated"* review page shows thumbnails with
   approve / dismiss / approve-all and applies Immich rotate edits on
   approval (same call as the ↻ button).
3. **Auto-apply** above a high threshold (e.g. ≥ 0.97 and no face
   disagreement), with the suggestion page doubling as the undo list.
   Off by default.

**Videos**: Immich can't store an edit, so a detection becomes a local
per-asset override (like the hide list) fed to mpv's `video-rotate`, with
the poster rotated locally to match.

## Shape of the work

- `orient.py` — ONNX runner, `predict(path) → (angle, confidence)`; model
  shipped in the package or downloaded on first use.
- Prefetch hook + `selection.auto_orient: off | display | suggest | apply`
  + `auto_orient_threshold`.
- Suggestion store (JSON, like `hidden.json`) + dashboard review page +
  `immframe orient-scan [--album …] [--limit N]`.
- `tools/train_orientation.py` — pulls previews via the API, synthesises
  rotations, fine-tunes, exports ONNX, reports held-out accuracy *per
  category* so weak spots are visible before anything is trusted.

Rough effort: runtime side a day; training script half a day; review page
about the size of the Settings page.

**Start with measurement**: train the model, run the crawler over the
WhatsApp album in *suggest* mode, and read the real precision on this
library before anything touches the frame or Immich.

## Prerequisites when picked up

- A write-scoped API key with `asset.edit.get/create/delete` on the frame
  (`immich.write_api_key`) — already required by the ↻ button.
- First establish whether the sideways *videos* seen on the frame are a
  metadata problem or a rendering glitch (every clip verified on the panel
  so far rendered correctly); a per-clip screen capture matched against
  `journalctl -t immframe | grep "mpv window"` settles it.
