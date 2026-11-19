"""hib: small helpers to open web pages in Camoufox for an AI agent.

Modules:
    env: read settings from the environment and the `.env` file.
    proxy: build proxy settings from the mobileproxy.space API or a URL.
    challenge: recognise anti-bot challenge pages by generic signs.
    browser: open pages with human-like pacing.
    errors: error types, each with its own process exit code.
"""

from hib.challenge import ChallengeCheck, detect_challenge
from hib.env import Settings, load_settings, parse_env_text
from hib.errors import (
    EXIT_CHALLENGE,
    EXIT_OK,
    BrowserStartError,
    ConfigError,
    HibError,
    PageTimeoutError,
    ProxyApiError,
    ProxyUnreachableError,
)
from hib.proxy import ProxyConfig, proxy_from_api_response, proxy_from_url, redact, traffic_left_mb

__all__ = [
    "EXIT_CHALLENGE",
    "EXIT_OK",
    "BrowserStartError",
    "ChallengeCheck",
    "ConfigError",
    "HibError",
    "PageTimeoutError",
    "ProxyApiError",
    "ProxyConfig",
    "ProxyUnreachableError",
    "Settings",
    "detect_challenge",
    "load_settings",
    "parse_env_text",
    "proxy_from_api_response",
    "proxy_from_url",
    "redact",
    "traffic_left_mb",
]
