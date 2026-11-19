#!/usr/bin/env python3
"""Check that the proxy works: get it from the provider and look at the exit address.

Prints one JSON line: {"source", "country", "city", "org", "network_owner", "ru", "traffic_left_mb",
"hosting", "proxy_flag", "mobile"}.
"network_owner" is the network owner (the same text as "org"). "hosting", "proxy_flag" and "mobile" come from one
extra request to a public reputation service (ip-api.com, plain http, which works through an http proxy here but
which a browser may refuse): true, false or null when it could not be read. They are a third
party's opinion and can be wrong. A line "reputation: ..." also goes to the log (stderr).
"ru" is true when the exit address is in Russia; the exit code stays 0 either way,
because the proxy itself works.
For a SOCKS proxy (socks5:// in HIB_PROXY_URL) the check cannot run here, because the
standard library has no SOCKS support: the line is {"source", "checked": false, "note"}
with the exit code 0, and the note says how to check through the browser instead.
Never prints the login, the password or the token.

Example:
    .venv/bin/python scripts/check_proxy.py
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from hib.env import DEFAULT_ENV_FILE, load_settings
from hib.errors import EXIT_OK, ConfigError, HibError, ProxyApiError
from hib.proxy import (
    call_mobileproxy,
    can_check_exit_ip,
    check_exit_ip,
    check_reputation,
    proxy_from_api_response,
    proxy_from_url,
    redact,
    traffic_left_mb,
    warn_if_open_proxy,
)

logger = logging.getLogger("check_proxy")

SOCKS_NOTE = (
    "the exit address was NOT checked: this script cannot go through a SOCKS proxy (no SOCKS support without "
    "an extra package). Check it through the browser instead: "
    "scripts/preflight.py --profile <name>"
)


def reputation_line(reputation: dict[str, bool | None]) -> str:
    """Return the one log line about the address reputation.

    Args:
        reputation: The `hosting` and `proxy` flags (None means unknown).

    Returns:
        `reputation: hosting=yes, proxy=no`, or `reputation: unknown` when either flag is unknown.
    """
    hosting, flagged = reputation["hosting"], reputation["proxy"]
    if hosting is None or flagged is None:
        return "reputation: unknown"
    return f"reputation: hosting={'yes' if hosting else 'no'}, proxy={'yes' if flagged else 'no'}"


def main(argv: list[str]) -> int:
    """Run the check.

    Args:
        argv: Arguments without the program name.

    Returns:
        Process exit code (0 when the proxy works).
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(description="Check the proxy without opening a browser.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="settings file (default: .env)")
    args = parser.parse_args(argv)
    secrets: list[str] = []
    try:
        settings = load_settings(args.env_file)
        secrets.extend(s for s in (settings.mobileproxy_token, settings.proxy_url) if s)
        left: float | None = None
        if settings.proxy_url:
            source = "HIB_PROXY_URL"
            proxy = proxy_from_url(settings.proxy_url)
        elif settings.mobileproxy_token:
            source = "mobileproxy.space"
            answer = call_mobileproxy("get_my_proxy", settings.mobileproxy_token)
            proxy = proxy_from_api_response(answer)
            left = traffic_left_mb(answer)
            if left is None:
                try:
                    left = traffic_left_mb(call_mobileproxy("get_balance", settings.mobileproxy_token))
                except ProxyApiError as exc:
                    # The traffic figure is a bonus; the proxy itself may still work.
                    logger.warning("could not read the traffic left: %s", redact(str(exc), secrets))
        else:
            raise ConfigError("no proxy configured: put MOBILEPROXY_API_TOKEN (or HIB_PROXY_URL) into the .env file")
        secrets.extend(proxy.secrets())
        if source == "HIB_PROXY_URL":
            warn_if_open_proxy(proxy)
        if not can_check_exit_ip(proxy):
            logger.warning("%s", SOCKS_NOTE)
            sys.stdout.write(json.dumps({"source": source, "checked": False, "note": SOCKS_NOTE}, ensure_ascii=False) + "\n")
            return EXIT_OK
        logger.info("proxy received, checking the exit address")
        info = check_exit_ip(proxy)
        reputation = check_reputation(proxy)
    except HibError as exc:
        message = redact(str(exc), secrets)
        logger.error("%s", message)
        sys.stdout.write(json.dumps({"error": message, "exit_code": exc.exit_code}, ensure_ascii=False) + "\n")
        return exc.exit_code
    result = {
        "source": source,
        **info,
        "network_owner": info.get("org") or None,
        "ru": info.get("country") == "RU",
        "traffic_left_mb": left,
        "hosting": reputation["hosting"],
        "proxy_flag": reputation["proxy"],
        "mobile": reputation.get("mobile"),
    }
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    logger.info("%s", reputation_line(reputation))
    if info.get("country") != "RU":
        logger.warning("the exit address is not in Russia (country=%s): check the proxy package", info.get("country"))
    if left is not None and left < 100:
        logger.warning("less than 100 MB of proxy traffic left")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
