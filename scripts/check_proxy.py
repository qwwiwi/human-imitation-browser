#!/usr/bin/env python3
"""Check that the proxy works: get it from the provider and look at the exit address.

Prints one JSON line: {"source", "country", "city", "org", "traffic_left_mb"}.
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
from hib.errors import EXIT_OK, ConfigError, HibError
from hib.proxy import call_mobileproxy, check_exit_ip, proxy_from_api_response, proxy_from_url, redact, traffic_left_mb

logger = logging.getLogger("check_proxy")


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
    settings = load_settings(args.env_file)
    secrets = [s for s in (settings.mobileproxy_token, settings.proxy_url) if s]
    try:
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
                left = traffic_left_mb(call_mobileproxy("get_balance", settings.mobileproxy_token))
        else:
            raise ConfigError("no proxy configured: put MOBILEPROXY_API_TOKEN (or HIB_PROXY_URL) into the .env file")
        secrets.extend(proxy.secrets())
        logger.info("proxy received, checking the exit address")
        info = check_exit_ip(proxy)
    except HibError as exc:
        message = redact(str(exc), secrets)
        logger.error("%s", message)
        sys.stdout.write(json.dumps({"error": message, "exit_code": exc.exit_code}, ensure_ascii=False) + "\n")
        return exc.exit_code
    result = {"source": source, **info, "traffic_left_mb": left}
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    if info.get("country") != "RU":
        logger.warning("the exit address is not in Russia (country=%s): check the proxy package", info.get("country"))
    if left is not None and left < 100:
        logger.warning("less than 100 MB of proxy traffic left")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
