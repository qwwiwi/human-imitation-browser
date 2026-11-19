#!/usr/bin/env python3
"""Pre-flight check: look at the address and the profile memory before the first real site.

Opens a neutral address-check page in the SAME profile and through the SAME proxy that the real
visit would use (nothing is opened on a real site), and prints one JSON line:
    {"ok", "profile", "proxy_works", "country", "city", "network_owner", "hosting", "proxy_flag",
     "mobile", "address_changed", ["address_change": {"old", "new"}], "profile_memory", "cookies_stored",
     "screenshot", "problems", "problems_soft"}
"hosting", "proxy_flag" and "mobile" are a third-party opinion (true, false or null when unknown); they are
read with a request from the browser context, so through the proxy (default http://ip-api.com/json/?fields=status,country,city,isp,as,mobile,proxy,hosting
in the ip-api.com format; set HIB_REPUTATION_URL to use another one).
An address page that times out is tried once more after a short pause.
"mobile" true means the address of a mobile operator; it never fails the check.
"profile_memory" is "new", "ok" or "empty". The line never carries an IP address, a cookie name or
value, or a proxy detail. Diagnostics go to stderr.

Exit codes: 0 ok; 12 the check failed (the address changed, the profile memory is empty for a used
profile, or with --strict the address is flagged as hosting/proxy); a proxy failure keeps its own
code (5, 6); the others are listed in hib/errors.py.
A passed check is remembered in the profile; open_page.py refuses a real address without a recent one.

Example:
    .venv/bin/python scripts/preflight.py --profile my-shop
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from hib import preflight as core
from hib.env import DEFAULT_ENV_FILE, REPO_ROOT, load_settings
from hib.errors import EXIT_OK, EXIT_PREFLIGHT_FAILED, EXIT_UNEXPECTED, ConfigError, HibError, ProfileError
from hib.profiles import DEFAULT_PROFILE, ProfileStore, profiles_root, validate_profile_name
from hib.proxy import redact, warn_if_open_proxy
from hib.session import fixed_proxy_from_settings, resolve_session_proxy

logger = logging.getLogger("preflight")


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
    parser = argparse.ArgumentParser(description="Check the address and the profile memory before the first real site.")
    parser.add_argument(
        "--profile",
        type=_profile_name,
        default=DEFAULT_PROFILE,
        help=f"profile to check: the same one the real visit will use (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail (exit 12) when a reputation service flags the address as hosting or proxy; without it this is a warning",
    )
    parser.add_argument(
        "--accept-address-change",
        action="store_true",
        help="only the owner, after reading what changed: record the new country, city and network owner as the profile's address",
    )
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "out", help="folder for the screenshot (default: out/)")
    parser.add_argument("--timeout", type=float, default=60.0, help="page load timeout, seconds (default 60)")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="settings file (default: .env)")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def emit_json(data: dict[str, object]) -> None:
    """Write one JSON line to stdout (the result channel for the agent)."""
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    """Run the check.

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
        check_url = core.check_url_or_default(settings.check_url)
        reputation_url = core.reputation_url_or_default(settings.reputation_url)
        # Imported here so that a broken browser install still gives a clean JSON error.
        from hib import browser

        options = browser.OpenOptions(out_dir=args.out, timeout_s=args.timeout)
        fixed_proxy = fixed_proxy_from_settings(settings)
        if fixed_proxy:
            secrets.extend(fixed_proxy.secrets())
            warn_if_open_proxy(fixed_proxy)
        store = ProfileStore(profiles_root(settings.profiles_dir))
        with store.acquire(args.profile, browser.make_fingerprint) as profile:
            if fixed_proxy:
                proxy = fixed_proxy
            else:
                if not settings.mobileproxy_token:  # cannot happen: checked in fixed_proxy_from_settings
                    raise ConfigError("no proxy configured")
                proxy = resolve_session_proxy(
                    settings.mobileproxy_token,
                    profile.proxy_cache_path,
                    profile.data.proxy_session,
                    proxy_id=settings.proxy_id,
                    state=settings.proxy_state,
                    city=settings.proxy_city,
                ).proxy
                secrets.extend(proxy.secrets())
            before = core.before_from_profile(profile)
            logger.info("pre-flight for profile %s: opening the address-check page through its proxy", profile.name)
            observed = core.observe_in_browser(proxy, profile, options, check_url, reputation_url=reputation_url)
            seen = core.sanitized(observed, secrets)
            verdict = core.assess(before, seen, strict=args.strict, accept_address_change=args.accept_address_change)
            if verdict.ok:
                # A failed check records nothing: running it again must not turn a changed address into the normal one.
                profile.record_preflight(core.network_of(seen), seen.cookies_at_end)
            report = core.build_report(profile.name, seen, verdict)
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
    emit_json(report)
    for problem in verdict.problems:
        logger.error("pre-flight failed: %s", problem)
    for note in verdict.problems_soft:
        logger.warning("%s", note)
    return EXIT_OK if verdict.ok else EXIT_PREFLIGHT_FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
