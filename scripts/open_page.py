#!/usr/bin/env python3
"""Open one or more web pages in Camoufox through the proxy, like a person would.

Prints one JSON line per page to stdout:
    {"url", "status", "title", "text_chars", "screenshot", "challenge", "reason"}
On an error prints one JSON line {"error", "exit_code"} instead.
Diagnostics go to stderr. Exit codes are listed in hib/errors.py and README.md.

Example:
    .venv/bin/python scripts/open_page.py https://www.ozon.ru/
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from hib.env import DEFAULT_ENV_FILE, REPO_ROOT, load_settings
from hib.errors import EXIT_CHALLENGE, EXIT_OK, EXIT_UNEXPECTED, HibError
from hib.proxy import ProxyConfig, redact, resolve_proxy

logger = logging.getLogger("open_page")


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Arguments without the program name.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Open web pages in a human-like browser through a proxy.")
    parser.add_argument("urls", nargs="+", help="one or more addresses, opened one after another")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "out", help="folder for screenshots (default: out/)")
    parser.add_argument("--no-images", action="store_true", help="do not load images to save proxy traffic")
    parser.add_argument("--timeout", type=float, default=60.0, help="page load timeout, seconds (default 60)")
    parser.add_argument("--pause-min", type=float, default=20.0, help="min pause between pages, seconds (default 20)")
    parser.add_argument("--pause-max", type=float, default=40.0, help="max pause between pages, seconds (default 40)")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="settings file (default: .env)")
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="go out directly without a proxy; only for neutral test pages such as https://example.com",
    )
    args = parser.parse_args(argv)
    if args.pause_min < 0 or args.pause_max < args.pause_min:
        parser.error("need 0 <= --pause-min <= --pause-max")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def emit_json(data: dict[str, object]) -> None:
    """Write one JSON line to stdout (the result channel for the agent)."""
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    """Run the command.

    Args:
        argv: Arguments without the program name.

    Returns:
        Process exit code.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    args = parse_args(argv)
    settings = load_settings(args.env_file)
    secrets = [s for s in (settings.mobileproxy_token,) if s]
    try:
        # Imported here so that a broken browser install still gives a clean JSON error.
        from hib.browser import OpenOptions, open_pages

        proxy: ProxyConfig | None = None
        if args.no_proxy:
            logger.warning("running WITHOUT a proxy: Russian marketplaces usually block server addresses")
        else:
            proxy = resolve_proxy(settings.mobileproxy_token, settings.proxy_url)
            secrets.extend(proxy.secrets())
        options = OpenOptions(
            out_dir=args.out,
            timeout_s=args.timeout,
            block_images=args.no_images,
            pause_between_s=(args.pause_min, args.pause_max),
        )
        stopped = open_pages(args.urls, proxy, options, lambda result: emit_json(result.to_dict()))
    except HibError as exc:
        message = redact(str(exc), secrets)
        logger.error("%s", message)
        emit_json({"error": message, "exit_code": exc.exit_code})
        return exc.exit_code
    except ImportError as exc:
        message = f"browser packages are not installed ({exc.name}); run scripts/install.sh"
        logger.error("%s", message)
        emit_json({"error": message, "exit_code": 8})
        return 8
    except Exception as exc:  # noqa: BLE001 - last resort: still one JSON line, never a raw traceback
        message = redact(f"unexpected error: {type(exc).__name__}: {exc}".splitlines()[0], secrets)
        logger.error("%s", message)
        emit_json({"error": message, "exit_code": EXIT_UNEXPECTED})
        return EXIT_UNEXPECTED
    if stopped:
        return EXIT_CHALLENGE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
