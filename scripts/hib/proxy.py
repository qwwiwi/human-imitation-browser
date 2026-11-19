"""Proxy settings: from the mobileproxy.space API or from a plain URL.

Credentials never leave these objects in logs: repr() hides them and
redact() removes them from error texts.
"""

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from hib.errors import ConfigError, ProxyApiError, ProxyUnreachableError

logger = logging.getLogger(__name__)

MOBILEPROXY_API = "https://mobileproxy.space/api.html"
API_TIMEOUT_S = 20
REQUIRED_FIELDS = ("proxy_hostname", "proxy_http_port", "proxy_login", "proxy_pass")


@dataclass(frozen=True)
class ProxyConfig:
    """HTTP proxy with login and password.

    Attributes:
        host: Proxy host name.
        port: Proxy port.
        username: Login (hidden in repr).
        password: Password (hidden in repr).
    """

    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)

    def as_playwright(self) -> dict[str, str]:
        """Return the dict Camoufox / Playwright expect in `proxy=`."""
        return {"server": f"http://{self.host}:{self.port}", "username": self.username, "password": self.password}

    def as_url(self) -> str:
        """Return `http://user:pass@host:port` with percent-encoded credentials."""
        user = quote(self.username, safe="")
        pwd = quote(self.password, safe="")
        return f"http://{user}:{pwd}@{self.host}:{self.port}"

    def secrets(self) -> list[str]:
        """Return the values that must never be printed."""
        return [self.password, self.username, quote(self.password, safe=""), quote(self.username, safe="")]


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every secret value in text with `***`.

    Args:
        text: Any message, for example an exception text.
        secrets: Values to hide; empty strings are ignored.

    Returns:
        The text without the secrets.
    """
    # Longest first so that a secret containing another one is hidden whole.
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def _first_item(data: object) -> dict[str, object]:
    """Return the first proxy record from an API answer (a list or a single object)."""
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict) and data:
        return data
    raise ProxyApiError("the proxy API returned no proxy (is a package bought and active?)")


def proxy_from_api_response(data: object) -> ProxyConfig:
    """Build a ProxyConfig from the mobileproxy.space `get_my_proxy` answer.

    Args:
        data: Decoded JSON. The first proxy in the list is used.

    Returns:
        The proxy settings.

    Raises:
        ProxyApiError: The answer has no usable proxy record.
    """
    item = _first_item(data)
    missing = [name for name in REQUIRED_FIELDS if not item.get(name)]
    if missing:
        raise ProxyApiError(f"the proxy API answer lacks fields: {', '.join(missing)}")
    try:
        port = int(str(item["proxy_http_port"]))
    except ValueError as exc:
        raise ProxyApiError("the proxy API returned a non-numeric port") from exc
    return ProxyConfig(
        host=str(item["proxy_hostname"]),
        port=port,
        username=str(item["proxy_login"]),
        password=str(item["proxy_pass"]),
    )


def traffic_left_mb(data: object) -> float | None:
    """Read `residential_traffic_left_mb` from an API answer if it is there.

    Args:
        data: Decoded JSON (list or object).

    Returns:
        Megabytes left, or None when the field is absent or not a number.
    """
    try:
        item = _first_item(data)
    except ProxyApiError:
        return None
    value = item.get("residential_traffic_left_mb")
    if value is None:
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def proxy_from_url(url: str) -> ProxyConfig:
    """Parse `http://user:pass@host:port` (HIB_PROXY_URL) into a ProxyConfig.

    Args:
        url: Proxy URL; user and password may be percent-encoded.

    Returns:
        The proxy settings.

    Raises:
        ConfigError: The URL is not an http(s) proxy URL with host, port, login and password.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise ConfigError("HIB_PROXY_URL must look like http://login:password@host:port")
    try:
        port = parts.port
    except ValueError as exc:
        raise ConfigError("HIB_PROXY_URL has an invalid port") from exc
    if not parts.hostname or not port or parts.username is None or parts.password is None:
        raise ConfigError("HIB_PROXY_URL must look like http://login:password@host:port")
    return ProxyConfig(host=parts.hostname, port=port, username=unquote(parts.username), password=unquote(parts.password))


def call_mobileproxy(command: str, token: str) -> object:
    """Call one mobileproxy.space API command and return decoded JSON.

    Args:
        command: API command, for example `get_my_proxy` or `get_balance`.
        token: API token from the personal cabinet.

    Returns:
        Decoded JSON answer.

    Raises:
        ProxyApiError: Network error, HTTP error or a non-JSON answer.
    """
    req = urllib.request.Request(
        f"{MOBILEPROXY_API}?command={quote(command)}",
        headers={"Authorization": f"Bearer {token}"},
    )
    logger.info("asking the proxy API: %s", command)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        hint = " (check MOBILEPROXY_API_TOKEN)" if exc.code in (401, 403) else ""
        raise ProxyApiError(f"proxy API answered HTTP {exc.code}{hint}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = redact(str(getattr(exc, "reason", exc)), [token])
        raise ProxyApiError(f"proxy API is not reachable: {reason}") from None
    try:
        return json.loads(body)
    except ValueError:
        raise ProxyApiError("proxy API returned something that is not JSON") from None


def resolve_proxy(token: str | None, proxy_url: str | None) -> ProxyConfig:
    """Pick the proxy: HIB_PROXY_URL first, otherwise the mobileproxy.space API.

    Args:
        token: mobileproxy.space API token or None.
        proxy_url: Proxy URL of any provider or None.

    Returns:
        The proxy settings.

    Raises:
        ConfigError: Neither value is set.
        ProxyApiError: The API call failed.
    """
    if proxy_url:
        logger.info("using the proxy from HIB_PROXY_URL")
        return proxy_from_url(proxy_url)
    if not token:
        raise ConfigError(
            "no proxy configured: put MOBILEPROXY_API_TOKEN (or HIB_PROXY_URL) into the .env file"
        )
    return proxy_from_api_response(call_mobileproxy("get_my_proxy", token))


def check_exit_ip(proxy: ProxyConfig, url: str = "https://ipinfo.io/json") -> dict[str, str]:
    """Open a neutral IP-info page through the proxy and return country, city, org.

    Args:
        proxy: Proxy to test.
        url: JSON endpoint that describes the caller's IP.

    Returns:
        Mapping with the keys `country`, `city`, `org` (empty strings when absent).

    Raises:
        ProxyUnreachableError: The proxy refused, failed the login or timed out.
    """
    proxy_url = proxy.as_url()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    try:
        with opener.open(url, timeout=API_TIMEOUT_S) as resp:
            info = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ProxyUnreachableError(f"the check page answered HTTP {exc.code} through the proxy") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = redact(str(getattr(exc, "reason", exc)), proxy.secrets())
        hint = " (wrong login/password?)" if "407" in reason else ""
        raise ProxyUnreachableError(f"cannot get through the proxy: {reason}{hint}") from None
    if not isinstance(info, dict):
        raise ProxyUnreachableError("the check page returned unexpected data")
    return {key: str(info.get(key, "")) for key in ("country", "city", "org")}
