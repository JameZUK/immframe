"""Entry point for the `immframe` console script."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .config import Config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="immframe", description="Immich-backed slideshow")
    parser.add_argument("--config", type=Path, help="path to config.yaml (overrides search)")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="log level (default INFO)",
    )
    parser.add_argument("--version", action="version", version=f"immframe {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    log = logging.getLogger("immframe.start")

    try:
        config = Config.load(args.config)
    except FileNotFoundError as e:
        log.error("config not found: %s", e)
        return 2
    except (ValueError, KeyError) as e:
        log.error("config invalid: %s", e)
        return 2

    # Import controller lazily so config errors above don't pull in pi3d/mpv.
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


if __name__ == "__main__":
    sys.exit(main())
