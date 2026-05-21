"""HTTP control plane.

Small REST API for monitoring and controlling the slideshow. Designed to fix
the picframe HTTP issues by construction:

- Allow-listed endpoints — there is no `getattr(controller, key)` path
  through which an attacker can invoke arbitrary controller methods.
- No filesystem static serving — images are addressed by Immich asset ID
  only, then proxied through `ImmichClient`. There is no `os.path.join`
  with user input anywhere in this module.
- ThreadingHTTPServer so a slow image proxy doesn't serialise the control
  plane.
- `secrets.compare_digest` for Basic-auth comparison (timing-safe).
- Default bind 127.0.0.1; user must explicitly opt in to LAN exposure.

Endpoints:
    GET  /healthz                    no auth — liveness probe
    GET  /api/version                version info
    GET  /api/state                  full state snapshot
    POST /api/paused                 {"value": bool}
    POST /api/selection_mode         {"value": "random"|"album"|"smart"}
    POST /api/album_ids              {"value": ["uuid", ...]}
    POST /api/smart_query            {"value": "..."}
    POST /api/next                   force-advance
    GET  /api/image/<asset_id>       proxy preview JPEG from Immich

All control endpoints require Basic auth when `config.control.http.auth=true`.
Image proxying does too — it would be silly to gate the controls but leak
the gallery.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..config import HttpConfig
from ..immich.client import ImmichClient, ImmichError

if TYPE_CHECKING:
    from ..controller import Controller

log = logging.getLogger(__name__)

# UUIDs (Immich asset IDs) are 36-char canonical, but we allow any base64-ish
# non-pathy charset to be tolerant of future schemes. Crucially: NO slashes,
# NO dots — kills path traversal at the regex.
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_IMAGE_PATH_RE = re.compile(r"^/api/image/([A-Za-z0-9_-]{8,128})$")

_SELECTION_MODES = ("random", "album", "smart", "scene")
_SHOW_TEXT_KEYS = ("title", "caption", "name", "date", "location", "folder")

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Strict whitelist for static-file serving. The key is the URL path, the
# value is (relative-filename, content-type). No filesystem path is ever
# built from user input.
_STATIC: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
}

_POST_PATHS = frozenset({
    "/api/paused",
    "/api/selection_mode",
    "/api/album_ids",
    "/api/smart_query",
    "/api/next",
    "/api/brightness",
    "/api/display_is_on",
    "/api/show_text",
    "/api/show_clock",
    "/api/time_delay",
    "/api/fade_time",
})


class HttpInterface:
    """Lifecycle wrapper around a ThreadingHTTPServer."""

    def __init__(self, config: HttpConfig, controller: "Controller", client: ImmichClient) -> None:
        self._cfg = config
        self._ctrl = controller
        self._client = client
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        addr = (self._cfg.bind, self._cfg.port)
        server = _Server(addr, _Handler, self._ctrl, self._client, self._cfg)
        thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        log.info("http listening on %s:%d", *addr)

    def stop(self) -> None:
        s = self._server
        if s is None:
            return
        try:
            s.shutdown()
            s.server_close()
        except Exception as e:
            log.debug("http stop: %s", e)
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        addr: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        controller: "Controller",
        client: ImmichClient,
        cfg: HttpConfig,
    ) -> None:
        super().__init__(addr, handler)
        self.controller = controller
        self.immich = client
        self.auth_required = bool(cfg.auth)
        # Pre-encode the expected Basic credentials once.
        if cfg.auth and cfg.username:
            token = base64.b64encode(f"{cfg.username}:{cfg.password}".encode()).decode()
            self.expected_authz = f"Basic {token}"
        else:
            self.expected_authz = None


class _Handler(BaseHTTPRequestHandler):
    # Silence the default access log; we have our own logging.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("http %s — " + format, self.client_address[0], *args)

    # ── Auth ────────────────────────────────────────────────────────────
    def _authed(self) -> bool:
        server: _Server = self.server                      # type: ignore[assignment]
        if not server.auth_required or server.expected_authz is None:
            return True
        got = self.headers.get("Authorization", "")
        return secrets.compare_digest(got, server.expected_authz)

    def _unauthorized(self) -> None:
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="immframe"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── Response helpers ────────────────────────────────────────────────
    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    # ── Body parsing ────────────────────────────────────────────────────
    def _read_json(self, max_bytes: int = 64 * 1024) -> Any:
        cl_str = self.headers.get("Content-Length", "0")
        try:
            cl = int(cl_str)
        except ValueError:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "bad Content-Length")
        if cl < 0 or cl > max_bytes:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "body too large")
        if cl == 0:
            return None
        raw = self.rfile.read(cl)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise _HttpError(HTTPStatus.BAD_REQUEST, f"bad JSON: {e}")

    # ── Dispatch ────────────────────────────────────────────────────────
    def do_GET(self) -> None:                              # noqa: N802
        try:
            self._dispatch_get()
        except _HttpError as e:
            self._error(e.status, e.message)
        except BrokenPipeError:
            pass
        except Exception as e:
            log.exception("http GET %s failed: %s", self.path, e)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    def do_POST(self) -> None:                             # noqa: N802
        try:
            self._dispatch_post()
        except _HttpError as e:
            self._error(e.status, e.message)
        except BrokenPipeError:
            pass
        except Exception as e:
            log.exception("http POST %s failed: %s", self.path, e)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    def _dispatch_get(self) -> None:
        if self.path == "/healthz":
            return self._healthz()
        if not self._authed():
            return self._unauthorized()
        if self.path in _STATIC:
            return self._static(self.path)
        if self.path == "/api/version":
            return self._version()
        if self.path == "/api/state":
            return self._state()
        m = _IMAGE_PATH_RE.match(self.path)
        if m:
            return self._image(m.group(1))
        # POST-only paths return 405 here to be precise (vs blanket 404)
        if self.path in _POST_PATHS:
            return self._error(HTTPStatus.METHOD_NOT_ALLOWED, "POST only")
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def _dispatch_post(self) -> None:
        if not self._authed():
            return self._unauthorized()
        path = self.path
        if path == "/api/paused":
            value = self._require_value(bool)
            self._ctrl.paused = value
            return self._state()
        if path == "/api/selection_mode":
            value = self._require_value(str)
            if value not in _SELECTION_MODES:
                raise _HttpError(HTTPStatus.BAD_REQUEST, f"selection_mode must be one of {_SELECTION_MODES}")
            self._ctrl.selection_mode = value               # type: ignore[assignment]
            return self._state()
        if path == "/api/album_ids":
            value = self._require_value(list)
            for item in value:
                if not isinstance(item, str) or not _ASSET_ID_RE.match(item):
                    raise _HttpError(HTTPStatus.BAD_REQUEST, "album_ids must be a list of UUIDs")
            self._ctrl.album_ids = value
            return self._state()
        if path == "/api/smart_query":
            value = self._require_value(str)
            self._ctrl.smart_query = value
            return self._state()
        if path == "/api/next":
            self._ctrl.next()
            return self._empty(HTTPStatus.ACCEPTED)
        if path == "/api/brightness":
            value = self._require_number(0.0, 1.0)
            self._ctrl.brightness = value
            return self._state()
        if path == "/api/display_is_on":
            value = self._require_value(bool)
            self._ctrl.display_is_on = value
            return self._state()
        if path == "/api/show_text":
            value = self._require_value(list)
            for item in value:
                if not isinstance(item, str) or item not in _SHOW_TEXT_KEYS:
                    raise _HttpError(
                        HTTPStatus.BAD_REQUEST,
                        f"show_text items must be from {list(_SHOW_TEXT_KEYS)}",
                    )
            self._ctrl.show_text = value
            return self._state()
        if path == "/api/show_clock":
            value = self._require_value(bool)
            self._ctrl.show_clock = value
            return self._state()
        if path == "/api/time_delay":
            value = self._require_number(1.0, 3600.0)
            self._ctrl.time_delay = value
            return self._state()
        if path == "/api/fade_time":
            value = self._require_number(0.0, 30.0)
            self._ctrl.fade_time = value
            return self._state()
        # GET-only paths
        if path in {"/api/version", "/api/state", "/healthz"}:
            return self._error(HTTPStatus.METHOD_NOT_ALLOWED, "GET only")
        if _IMAGE_PATH_RE.match(path):
            return self._error(HTTPStatus.METHOD_NOT_ALLOWED, "GET only")
        self._error(HTTPStatus.NOT_FOUND, "not found")

    # ── Endpoint handlers ───────────────────────────────────────────────
    def _healthz(self) -> None:
        self._json(HTTPStatus.OK, {"status": "ok"})

    def _version(self) -> None:
        self._json(HTTPStatus.OK, {"version": __version__})

    def _state(self) -> None:
        c = self._ctrl
        asset = c.current_asset
        asset_obj = None
        if asset is not None:
            camera = " ".join(p for p in (asset.camera_make, asset.camera_model) if p)
            asset_obj = {
                "id": asset.id,
                "file": asset.original_file_name,
                "kind": asset.kind.value,
                "taken_at": asset.taken_at.isoformat() if asset.taken_at is not None else None,
                "city": asset.geo.city,
                "country": asset.geo.country,
                "camera": camera or None,
                "favorite": asset.favorite,
            }
        self._json(HTTPStatus.OK, {
            "paused": c.paused,
            "selection_mode": c.selection_mode,
            "album_ids": c.album_ids,
            "smart_query": c.smart_query,
            "current_scene": c.current_scene,
            "brightness": c.brightness,
            "display_is_on": c.display_is_on,
            "show_text": c.show_text,
            "show_clock": c.show_clock,
            "time_delay": c.time_delay,
            "fade_time": c.fade_time,
            "current_asset": asset_obj,
        })

    def _static(self, url_path: str) -> None:
        filename, content_type = _STATIC[url_path]
        path = _WEB_DIR / filename
        try:
            data = path.read_bytes()
        except OSError as e:
            log.warning("static read %s: %s", filename, e)
            raise _HttpError(HTTPStatus.INTERNAL_SERVER_ERROR, "static file missing")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _image(self, asset_id: str) -> None:
        # Belt and braces — the path regex already enforces this, but double-check.
        if not _ASSET_ID_RE.match(asset_id):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "bad asset id")
        server: _Server = self.server                      # type: ignore[assignment]
        try:
            with server.immich.stream_preview(asset_id) as r:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", r.headers.get("Content-Type", "image/jpeg"))
                cl = r.headers.get("Content-Length")
                if cl is not None:
                    self.send_header("Content-Length", cl)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        self.wfile.write(chunk)
        except ImmichError as e:
            log.warning("image proxy %s: %s", asset_id, e)
            raise _HttpError(HTTPStatus.BAD_GATEWAY, "upstream error")

    # ── Helpers ─────────────────────────────────────────────────────────
    @property
    def _ctrl(self) -> "Controller":
        return self.server.controller                      # type: ignore[attr-defined]

    def _require_value(self, typ: type) -> Any:
        body = self._read_json()
        if not isinstance(body, dict) or "value" not in body:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "expected {'value': ...}")
        v = body["value"]
        # bool is a subclass of int; reject the cross-coercion explicitly.
        if typ is bool and not isinstance(v, bool):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "value must be boolean")
        if typ is not bool and isinstance(v, bool):
            raise _HttpError(HTTPStatus.BAD_REQUEST, f"value must be {typ.__name__}")
        if not isinstance(v, typ):
            raise _HttpError(HTTPStatus.BAD_REQUEST, f"value must be {typ.__name__}")
        return v

    def _require_number(self, lo: float, hi: float) -> float:
        body = self._read_json()
        if not isinstance(body, dict) or "value" not in body:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "expected {'value': number}")
        v = body["value"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "value must be number")
        v = float(v)
        if v < lo or v > hi:
            raise _HttpError(HTTPStatus.BAD_REQUEST, f"value must be in [{lo}, {hi}]")
        return v


class _HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
