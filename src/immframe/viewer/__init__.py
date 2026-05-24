"""Renderer subpackage.

Phase 1 vendors two files from picframe:
    - `display.py`  ← picframe/src/picframe/viewer_display.py
    - `mat_image.py` ← picframe/src/picframe/mat_image.py

Both are pi3d/PIL-only and don't need to know about Immich. The controller
hands the viewer `(local_path, metadata_dict)` — same contract picframe uses.

Orientation handling: the vendored display.py was modified to use
`PIL.ImageOps.exif_transpose` instead of picframe's manual orientation
switch. This is a no-op for Immich's `preview` JPEGs (which are
pre-rotated) and correctly applies orientation to original files served
via `fullsize` (which redirects to `/original`).

No stubs in this file; the picframe sources come in when Phase 1 starts.
"""
