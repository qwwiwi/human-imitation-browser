"""Settings from the environment and an optional `.env` file."""

import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hib.errors import ConfigError
from hib.pace import DEFAULT_MIN_GAP_S, parse_min_gap

logger = logging.getLogger(__name__)

# Repository root: scripts/hib/env.py -> parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_PREFLIGHT_MAX_AGE_H = 12.0


def parse_preflight_max_age(value: str | None) -> float:
    """Read how many hours a pre-flight check stays valid (env HIB_PREFLIGHT_MAX_AGE_H).

    Args:
        value: Text from the environment, or None for the default (12 hours).

    Returns:
        Hours.

    Raises:
        ConfigError: The text is not a finite non-negative number.
    """
    if value is None or not value.strip():
        return DEFAULT_PREFLIGHT_MAX_AGE_H
    try:
        hours = float(value)
    except ValueError:
        raise ConfigError("HIB_PREFLIGHT_MAX_AGE_H must be a number of hours") from None
    if not math.isfinite(hours) or hours < 0:
        raise ConfigError("HIB_PREFLIGHT_MAX_AGE_H must be a number of hours, zero or more")
    return hours


@dataclass(frozen=True)
class Settings:
    """Values the scripts need.

    Attributes:
        mobileproxy_token: API token of mobileproxy.space, or None.
        proxy_url: Full proxy URL for any other provider, or None.
        profiles_dir: Override of the profile folder (HIB_PROFILES_DIR), or None.
        proxy_state: Wanted proxy state/region (HIB_PROXY_STATE), or None.
        proxy_city: Wanted proxy city (HIB_PROXY_CITY), or None.
        proxy_id: Provider's proxy id (HIB_PROXY_ID), or None to take it from the account.
        min_gap_s: Minimal seconds between navigations to one host (HIB_MIN_GAP_S).
        check_url: Address-check page for the pre-flight (HIB_CHECK_URL), or None for the default.
        preflight_max_age_h: Hours a successful pre-flight stays valid (HIB_PREFLIGHT_MAX_AGE_H).
        reputation_url: Page with the hosting/proxy/mobile flags for the pre-flight (HIB_REPUTATION_URL), or None for the default.
    """

    mobileproxy_token: str | None
    proxy_url: str | None
    profiles_dir: str | None = None
    proxy_state: str | None = None
    proxy_city: str | None = None
    proxy_id: str | None = None
    min_gap_s: float = DEFAULT_MIN_GAP_S
    check_url: str | None = None
    preflight_max_age_h: float = DEFAULT_PREFLIGHT_MAX_AGE_H
    reputation_url: str | None = None

    def __repr__(self) -> str:
        """Never show secret values."""
        token = "set" if self.mobileproxy_token else "unset"
        url = "set" if self.proxy_url else "unset"
        return f"Settings(mobileproxy_token={token}, proxy_url={url})"


def parse_env_text(text: str) -> dict[str, str]:
    """Parse `.env` content: KEY=VALUE lines, `#` comments, optional quotes and `export`.

    Args:
        text: File content.

    Returns:
        Mapping of keys to values. Lines without `=` are skipped.
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or " " in key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def load_settings(env_file: Path = DEFAULT_ENV_FILE, environ: Mapping[str, str] | None = None) -> Settings:
    """Load settings; real environment variables win over the `.env` file.

    Args:
        env_file: Path to the `.env` file; a missing file is fine.
        environ: Environment mapping, defaults to os.environ.

    Returns:
        Settings with empty values turned into None.

    Raises:
        ConfigError: The settings file exists but cannot be read.
    """
    env = os.environ if environ is None else environ
    file_values: dict[str, str] = {}
    if env_file.is_file():
        try:
            file_values = parse_env_text(env_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"cannot read the settings file {env_file}: {type(exc).__name__}") from None
        logger.debug("read settings file %s", env_file)

    def pick(name: str) -> str | None:
        value = env.get(name) or file_values.get(name) or ""
        return value.strip() or None

    return Settings(
        mobileproxy_token=pick("MOBILEPROXY_API_TOKEN"),
        proxy_url=pick("HIB_PROXY_URL"),
        profiles_dir=pick("HIB_PROFILES_DIR"),
        proxy_state=pick("HIB_PROXY_STATE"),
        proxy_city=pick("HIB_PROXY_CITY"),
        proxy_id=pick("HIB_PROXY_ID"),
        min_gap_s=parse_min_gap(pick("HIB_MIN_GAP_S")),
        check_url=pick("HIB_CHECK_URL"),
        preflight_max_age_h=parse_preflight_max_age(pick("HIB_PREFLIGHT_MAX_AGE_H")),
        reputation_url=pick("HIB_REPUTATION_URL"),
    )
