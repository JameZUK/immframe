"""Renderer subpackage.

Phase 1 vendors two files from picframe:
    - `display.py`  ← picframe/src/picframe/viewer_display.py
    - `mat_image.py` ← picframe/src/picframe/mat_image.py

Both are pi3d/PIL-only and don't need to know about Immich. The controller
hands the viewer `(local_path, metadata_dict)` — same contract picframe uses.

One change to make when vendoring `display.py`:
    Strip the EXIF-orientation rotation step. Immich's preview JPEGs are
    already pre-rotated, and `Asset.width`/`height` reflect the displayed
    shape. Any further rotation in the viewer will double-rotate landscape-
    in-portrait-housing photos. Look for `__orientate_image` (or similar)
    in picframe's viewer_display and bypass it.

No stubs in this file; the picframe sources come in when Phase 1 starts.
"""
