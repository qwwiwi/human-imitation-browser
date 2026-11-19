"""Settings from the environment and an optional `.env` file."""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hib.errors import ConfigError

logger = logging.getLogger(__name__)

# Repository root: scripts/hib/env.py -> parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


@dataclass(frozen=True)
class Settings:
    """Values the scripts need.

    Attributes:
        mobileproxy_token: API token of mobileproxy.space, or None.
        proxy_url: Full proxy URL for any other provider, or None.
    """

    mobileproxy_token: str | None
    proxy_url: str | None

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

    return Settings(mobileproxy_token=pick("MOBILEPROXY_API_TOKEN"), proxy_url=pick("HIB_PROXY_URL"))
