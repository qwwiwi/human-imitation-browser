#!/usr/bin/env python3
"""Open one or more web pages as one returning visitor: the same browser profile, fingerprint and proxy session.

Every run uses a named profile (default: `default`): the browser's own memory
(cookies, cache, history), the browser fingerprint and the sticky proxy session
stay the same from run to run. Two runs never use one profile at the same time.

Prints one JSON line per page to stdout:
    {"url", "status", "title", "text_chars", "screenshot", "challenge", "reason", "profile", "visits"}
On an error prints one JSON line {"error", "exit_code"} instead
(a malformed HIB_PROXY_URL is exit 4, an HTTP 5xx answer is exit 9, a profile in use is exit 10).
A profile without a successful `scripts/preflight.py` in the last 12 hours (HIB_PREFLIGHT_MAX_AGE_H) is refused
with exit 4 before any proxy call or browser start. `--ephemeral` (no profile) is only for neutral check pages
(example.com, ipinfo.io, ip-api.com); any other address with it is exit 4.
Diagnostics go to stderr. Exit codes are listed in hib/errors.py and README.md.

Example:
    .venv/bin/python scripts/open_page.py --profile my-shop https://www.ozon.ru/
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from hib.env import DEFAULT_ENV_FILE, REPO_ROOT, load_settings, require_neutral_urls
from hib.errors import EXIT_CHALLENGE, EXIT_OK, EXIT_UNEXPECTED, ConfigError, HibError, ProfileError
from hib.pace import Pacer
from hib.preflight import require_fresh_preflight
from hib.profiles import DEFAULT_PROFILE, ProfileStore, profiles_root, validate_profile_name
from hib.proxy import ProxyConfig, redact, resolve_proxy, warn_if_open_proxy
from hib.session import STICKY_MINUTES, fixed_proxy_from_settings, resolve_session_proxy

logger = logging.getLogger("open_page")


def _profile_name(text: str) -> str:
    """argparse type: a valid profile name."""
    try:
        return validate_profile_name(text)
    except ProfileError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Arguments without the program name.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="Open web pages as one returning visitor: a named browser profile with its own proxy session."
    )
    parser.add_argument("urls", nargs="+", help="one or more addresses, opened one after another")
    parser.add_argument(
        "--profile",
        type=_profile_name,
        default=None,
        help=f"profile name: the browser memory, fingerprint and proxy session to reuse (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        help="a brand-new browser without a profile, a new visitor every time; for a neutral test page only",
    )
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "out", help="folder for screenshots (default: out/)")
    parser.add_argument("--no-images", action="store_true", help="do not load images to save proxy traffic")
    parser.add_argument("--timeout", type=float, default=60.0, help="page load timeout, seconds (default 60)")
    parser.add_argument("--pause-min", type=float, default=20.0, help="min pause between pages, seconds (default 20)")
    parser.add_argument("--pause-max", type=float, default=40.0, help="max pause between pages, seconds (default 40)")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="settings file (default: .env)")
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="go out directly without a proxy; only with --ephemeral, for neutral test pages such as https://example.com",
    )
    args = parser.parse_args(argv)
    if args.ephemeral and args.profile:
        parser.error("--ephemeral and --profile cannot be used together")
    if args.no_proxy and not args.ephemeral:
        parser.error("--no-proxy is only allowed with --ephemeral: a profile always goes out through its own proxy")
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
    secrets: list[str] = []
    try:
        settings = load_settings(args.env_file)
        secrets.extend(s for s in (settings.mobileproxy_token,) if s)
        if args.ephemeral:
            require_neutral_urls(args.urls, settings.ephemeral_hosts)  # before any proxy call or browser start
        # Imported here so that a broken browser install still gives a clean JSON error.
        from hib import browser

        options = browser.OpenOptions(
            out_dir=args.out,
            timeout_s=args.timeout,
            block_images=args.no_images,
            pause_between_s=(args.pause_min, args.pause_max),
        )
        fixed_proxy = fixed_proxy_from_settings(settings, args.no_proxy)
        if fixed_proxy:
            secrets.extend(fixed_proxy.secrets())
            warn_if_open_proxy(fixed_proxy)
        if args.ephemeral:
            logger.warning(
                "EPHEMERAL run: a brand-new browser, so every site sees a new visitor. "
                "Use it for a neutral test page only; for real sites use a named profile (--profile NAME)"
            )
            proxy: ProxyConfig | None = None
            if args.no_proxy:
                logger.warning("running WITHOUT a proxy: Russian marketplaces usually block server addresses")
            else:
                proxy = fixed_proxy or resolve_proxy(settings.mobileproxy_token, settings.proxy_url)
                secrets.extend(proxy.secrets())
            stopped = browser.open_pages(
                args.urls, proxy, options, lambda result: emit_json({**result.to_dict(), "profile": None, "visits": 0})
            )
        else:
            store = ProfileStore(profiles_root(settings.profiles_dir))
            with store.acquire(args.profile or DEFAULT_PROFILE, browser.make_fingerprint) as profile:
                # Before any proxy call or browser start: a real address needs a recent pre-flight check
                # (the cheap part; the part bound to the proxy configuration follows once the proxy is known).
                require_fresh_preflight(profile, settings.preflight_max_age_h)
                if fixed_proxy:
                    logger.info("using the proxy from HIB_PROXY_URL as it is (choose a sticky session at your provider)")
                    proxy = fixed_proxy
                    require_fresh_preflight(profile, settings.preflight_max_age_h, proxy)
                else:
                    if not settings.mobileproxy_token:  # cannot happen: checked in _check_proxy_settings
                        raise ConfigError("no proxy configured")
                    session = resolve_session_proxy(
                        settings.mobileproxy_token,
                        profile.proxy_cache_path,
                        profile.data.proxy_session,
                        proxy_id=settings.proxy_id,
                        state=settings.proxy_state,
                        city=settings.proxy_city,
                    )
                    proxy = session.proxy
                    secrets.extend(proxy.secrets())
                    # The provider's sticky session lasts STICKY_MINUTES (an upper bound: it may have begun before the check).
                    require_fresh_preflight(profile, min(settings.preflight_max_age_h, STICKY_MINUTES / 60), proxy)
                    profile.data.last_exit = {"country": session.country, "city": session.city}
                pacer = Pacer(profile.data.last_navigation, settings.min_gap_s, on_change=profile.save)
                stopped = browser.open_pages(
                    args.urls,
                    proxy,
                    options,
                    # the visit is counted inside open_pages once the browser has started
                    lambda result: emit_json({**result.to_dict(), "profile": profile.name, "visits": profile.data.visits}),
                    profile,
                    pacer,
                )
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
