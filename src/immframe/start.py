"""Entry point for the `immframe` console script.

Without a subcommand, runs the slideshow.

With a subcommand, acts as a small CLI client against the local HTTP
control plane (or, for `random`, against Immich directly). Subcommands
require the config to point at a running immframe instance with HTTP
enabled.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

from . import __version__
from .config import Config
from .immich.client import ImmichClient, ImmichError

if TYPE_CHECKING:
    from .config import Config as ConfigType   # noqa: F401

log = logging.getLogger("immframe.start")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="immframe", description="Immich-backed slideshow")
    parser.add_argument("--config", type=Path, help="path to config.yaml (overrides search)")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="log level (default INFO)",
    )
    parser.add_argument("--version", action="version", version=f"immframe {__version__}")

    sub = parser.add_subparsers(dest="cmd", metavar="<command>")

    sub.add_parser("run", help="Run the slideshow (default with no subcommand)")
    sub.add_parser("state", help="Print current state as JSON")
    sub.add_parser("ping", help="Check the local HTTP server is up")
    sub.add_parser("immich-ping", help="Check Immich connectivity")
    sub.add_parser("pause", help="Pause the slideshow")
    sub.add_parser("resume", help="Resume the slideshow")
    sub.add_parser("next", help="Force-advance to the next slide")

    mode_p = sub.add_parser("mode", help="Set selection mode")
    mode_p.add_argument("mode", choices=("random", "album", "smart"))

    br_p = sub.add_parser("brightness", help="Set brightness (0.0 - 1.0)")
    br_p.add_argument("value", type=float)

    disp_p = sub.add_parser("display", help="Turn display on or off")
    disp_p.add_argument("state", choices=("on", "off"))

    albums_p = sub.add_parser("albums", help="Set album IDs (comma-separated)")
    albums_p.add_argument("ids", help='e.g. "uuid-1,uuid-2"')

    query_p = sub.add_parser("query", help="Set smart-search query")
    query_p.add_argument("text", help='e.g. "family at the beach"')

    delay_p = sub.add_parser("delay", help="Set slide duration in seconds")
    delay_p.add_argument("seconds", type=float)

    fade_p = sub.add_parser("fade", help="Set fade duration in seconds")
    fade_p.add_argument("seconds", type=float)

    showtext_p = sub.add_parser("show-text", help="Set overlay fields (comma-separated)")
    showtext_p.add_argument(
        "fields",
        help='subset of: title,caption,name,date,location,folder (or "none" to hide all)',
    )

    clock_p = sub.add_parser("clock", help="Show/hide the clock overlay")
    clock_p.add_argument("state", choices=("on", "off"))

    random_p = sub.add_parser("random", help="Pull N random asset IDs from Immich (direct)")
    random_p.add_argument("count", type=int, nargs="?", default=5)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    try:
        config = Config.load(args.config)
    except FileNotFoundError as e:
        log.error("config not found: %s", e)
        return 2
    except (ValueError, KeyError) as e:
        log.error("config invalid: %s", e)
        return 2

    cmd = args.cmd or "run"

    if cmd == "run":
        return _run_slideshow(config)

    try:
        return _dispatch_cli(config, args, cmd)
    except _CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


# ── Slideshow runner ───────────────────────────────────────────────────────


def _run_slideshow(config: Config) -> int:
    # Imported lazily so CLI subcommands don't pull in pi3d / mpv.
    from .controller import Controller

    controller = Controller(config)
    try:
        controller.start()
        controller.loop()
    except KeyboardInterrupt:
        log.info("interrupted")
    except Exception as e:
        log.exception("fatal: %s", e)
        return 1
    finally:
        controller.stop()
    return 0


# ── CLI client ─────────────────────────────────────────────────────────────


class _CliError(Exception):
    """User-facing CLI failure."""


def _http_client(config: Config) -> tuple[requests.Session, str]:
    http = config.control.http
    if not http.enabled:
        raise _CliError("HTTP control plane is disabled in config (set control.http.enabled: true)")
    session = requests.Session()
    if http.auth and http.username:
        session.auth = (http.username, http.password)
    bind = http.bind if http.bind not in ("0.0.0.0", "") else "127.0.0.1"
    base = f"http://{bind}:{http.port}"
    return session, base


def _print_response(r: requests.Response) -> None:
    try:
        print(json.dumps(r.json(), indent=2, sort_keys=True))
    except ValueError:
        if r.text:
            print(r.text)


def _check(r: requests.Response) -> None:
    if r.status_code >= 400:
        body = r.text[:200]
        raise _CliError(f"{r.request.method} {r.request.path_url} -> {r.status_code}: {body}")


def _post(session: requests.Session, url: str, body: dict | None = None) -> requests.Response:
    try:
        r = session.post(url, json=body if body is not None else {}, timeout=5.0)
    except requests.RequestException as e:
        raise _CliError(f"POST {url}: {e}") from e
    _check(r)
    return r


def _get(session: requests.Session, url: str) -> requests.Response:
    try:
        r = session.get(url, timeout=5.0)
    except requests.RequestException as e:
        raise _CliError(f"GET {url}: {e}") from e
    _check(r)
    return r


def _dispatch_cli(config: Config, args: argparse.Namespace, cmd: str) -> int:
    if cmd == "immich-ping":
        return _cmd_immich_ping(config)
    if cmd == "random":
        return _cmd_immich_random(config, args.count)

    session, base = _http_client(config)

    if cmd == "ping":
        r = _get(session, f"{base}/healthz")
        _print_response(r)
        return 0
    if cmd == "state":
        r = _get(session, f"{base}/api/state")
        _print_response(r)
        return 0
    if cmd == "pause":
        _post(session, f"{base}/api/paused", {"value": True})
        return 0
    if cmd == "resume":
        _post(session, f"{base}/api/paused", {"value": False})
        return 0
    if cmd == "next":
        _post(session, f"{base}/api/next")
        return 0
    if cmd == "mode":
        _post(session, f"{base}/api/selection_mode", {"value": args.mode})
        return 0
    if cmd == "brightness":
        _post(session, f"{base}/api/brightness", {"value": args.value})
        return 0
    if cmd == "display":
        _post(session, f"{base}/api/display_is_on", {"value": args.state == "on"})
        return 0
    if cmd == "albums":
        ids = [t.strip() for t in args.ids.split(",") if t.strip()]
        _post(session, f"{base}/api/album_ids", {"value": ids})
        return 0
    if cmd == "query":
        _post(session, f"{base}/api/smart_query", {"value": args.text})
        return 0
    if cmd == "delay":
        _post(session, f"{base}/api/time_delay", {"value": args.seconds})
        return 0
    if cmd == "fade":
        _post(session, f"{base}/api/fade_time", {"value": args.seconds})
        return 0
    if cmd == "show-text":
        fields_str = args.fields.strip().lower()
        fields: list[str] = [] if fields_str == "none" else [
            f.strip() for f in fields_str.split(",") if f.strip()
        ]
        _post(session, f"{base}/api/show_text", {"value": fields})
        return 0
    if cmd == "clock":
        _post(session, f"{base}/api/show_clock", {"value": args.state == "on"})
        return 0

    raise _CliError(f"unknown command: {cmd}")


# ── Immich-direct subcommands ──────────────────────────────────────────────


def _cmd_immich_ping(config: Config) -> int:
    c = ImmichClient(config.immich.url, config.immich.api_key, timeout_s=5.0)
    try:
        ok = c.ping()
    finally:
        c.close()
    print("pong" if ok else "no response")
    return 0 if ok else 1


def _cmd_immich_random(config: Config, count: int) -> int:
    c = ImmichClient(config.immich.url, config.immich.api_key, timeout_s=10.0)
    try:
        try:
            assets = c.random_assets(count)
        except ImmichError as e:
            raise _CliError(str(e)) from e
    finally:
        c.close()
    for a in assets:
        dt = a.taken_at.isoformat(sep=" ", timespec="seconds") if a.taken_at else "?"
        loc = ", ".join(p for p in (a.geo.city, a.geo.country) if p)
        kind = a.kind.value
        print(f"{a.id}  {kind:5s}  {dt:19s}  {a.original_file_name:30s}  {loc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
