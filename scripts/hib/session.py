"""One sticky proxy session per profile (mobileproxy.space residential `residential_settings`).

The profile keeps its own session id. On each run the proxy address is asked
from the API (one call, kept for a few minutes because the API answers HTTP 429
to the same request more often than once per 5 seconds), and the profile's
session id is put into the proxy login. The same id keeps the same exit address
for up to the session time; this is a target, not a guarantee.

No login, password or full proxy address is ever logged or printed here.
"""

import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from hib.errors import ConfigError, ProxyApiError
from hib.profiles import write_private
from hib.proxy import ProxyConfig, call_mobileproxy, proxy_from_api_response, proxy_from_url, redact

logger = logging.getLogger(__name__)

CACHE_TTL_S = 300.0
STICKY_MINUTES = 120
RATE_LIMIT_PAUSE_S = 5.5
SETTINGS_COMMAND = "residential_settings"
# `-session-<id>` followed by `-sessTime-`; what comes after sessTime is kept as the API gave it.
_SESSION_PART = re.compile(r"(-session-)(.+?)(?=-sessTime-|$)")

ApiCall = Callable[[str, str, Mapping[str, str] | None], object]


@dataclass(frozen=True)
class SessionProxy:
    """A proxy that carries the profile's session id.

    Attributes:
        proxy: The proxy to use (credentials hidden in repr).
        country: Country from the provider settings, empty if unknown.
        city: City from the provider settings, empty if none is set.
    """

    proxy: ProxyConfig
    country: str = ""
    city: str = ""


@dataclass(frozen=True)
class _Settings:
    """What `residential_settings` answered."""

    mode: str
    minutes: int
    state: str
    city: str
    country: str
    http: str


def new_session_id() -> str:
    """Return a random session id: 10 characters, letters and digits only."""
    return secrets.token_hex(5)


def with_session(username: str, session_id: str) -> str:
    """Put a session id into the proxy login.

    Args:
        username: Login as the API gave it: `<base>-...-session-<id>-sessTime-<minutes>...`.
        session_id: The profile's session id.

    Returns:
        The login with only the id replaced.

    Raises:
        ProxyApiError: The login has no session part. The login is not quoted.
    """
    if not _SESSION_PART.search(username):
        raise ProxyApiError("the proxy login from the API has no session part: this package cannot keep a sticky session")
    return _SESSION_PART.sub(lambda m: m.group(1) + session_id, username, count=1)


def _parse_settings(answer: object) -> _Settings:
    """Read the `residential_settings` answer.

    Raises:
        ProxyApiError: A needed field is missing or has the wrong type.
    """
    if not isinstance(answer, dict) or not isinstance(answer.get("http"), str) or not answer["http"]:
        raise ProxyApiError("the proxy settings answer has no proxy address")
    try:
        minutes = int(str(answer.get("session_time", "")))
    except ValueError:
        raise ProxyApiError("the proxy settings answer has no numeric session_time") from None
    return _Settings(
        mode=str(answer.get("session_mode", "")),
        minutes=minutes,
        state=str(answer.get("state") or ""),
        city=str(answer.get("city") or ""),
        country=str(answer.get("country") or ""),
        http=answer["http"],
    )


def _build(http_url: str, session_id: str, country: str, city: str) -> SessionProxy:
    """Turn the API's ready address into a proxy with the profile's session id."""
    try:
        base = proxy_from_url(http_url)
    except ConfigError:
        raise ProxyApiError("the proxy API returned an address that is not a usable http proxy") from None
    if base.scheme != "http" or not base.username or not base.password:
        raise ProxyApiError("the proxy API returned an address without a login: a sticky session needs one")
    proxy = ProxyConfig(
        host=base.host,
        port=base.port,
        username=with_session(base.username, session_id),
        password=base.password,
        extra_secrets=(base.username, http_url),
    )
    return SessionProxy(proxy=proxy, country=country, city=city)


def _read_cache(path: Path) -> dict[str, object] | None:
    """Return the cached answer, or None when absent or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("http"), str) and isinstance(data.get("fetched_at"), (int, float)):
        return data
    return None


def _lookup_proxy_id(token: str, call: ApiCall) -> str:
    """Take the proxy id from the account's own proxy list (the first one)."""
    answer = call("get_my_proxy", token, None)
    item = answer[0] if isinstance(answer, list) and answer else answer
    value = item.get("proxy_id") if isinstance(item, dict) else None
    if value in (None, ""):
        raise ProxyApiError("the proxy API answer has no proxy_id: set HIB_PROXY_ID in .env")
    return str(value)


def resolve_session_proxy(
    token: str,
    cache_path: Path,
    session_id: str,
    *,
    proxy_id: str | None = None,
    state: str | None = None,
    city: str | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    call: ApiCall = call_mobileproxy,
) -> SessionProxy:
    """Get the proxy for one profile: sticky session, the profile's session id.

    Args:
        token: mobileproxy.space API token.
        cache_path: File where the answer is kept for CACHE_TTL_S seconds (owner-only).
        session_id: The profile's session id.
        proxy_id: Proxy id, if known; otherwise taken from the account and remembered.
        state: Wanted state/region (HIB_PROXY_STATE); sent to the provider only if it differs. Unverified values.
        city: Wanted city (HIB_PROXY_CITY); sent to the provider only if it differs. Unverified values.
        now: Current time in seconds (replaceable in tests).
        sleep: Sleep function (replaceable in tests).
        call: API caller (replaceable in tests).

    Returns:
        The proxy and the country/city the settings point to.

    Raises:
        ProxyApiError: The API failed and there is no earlier answer to fall back to.
    """
    cached = _read_cache(cache_path)
    if cached and 0 <= now() - float(str(cached["fetched_at"])) < CACHE_TTL_S:
        logger.info("using the proxy answer kept a moment ago (no API call)")
        return _build(str(cached["http"]), session_id, str(cached.get("country", "")), str(cached.get("city", "")))
    try:
        pid = proxy_id or (str(cached["proxy_id"]) if cached and cached.get("proxy_id") else "") or _lookup_proxy_id(token, call)
        settings = _parse_settings(call(SETTINGS_COMMAND, token, {"proxy_id": pid}))
        changes: dict[str, str] = {}
        if settings.mode != "sticky":
            changes["session_mode"] = "sticky"
        if settings.minutes != STICKY_MINUTES:
            changes["session_time"] = str(STICKY_MINUTES)
        if state and state != settings.state:
            changes["state"] = state
        if city and city != settings.city:
            changes["city"] = city
        if changes:
            logger.info("changing the proxy settings at the provider: %s", ", ".join(f"{k}={v}" for k, v in changes.items()))
            saved = call(SETTINGS_COMMAND, token, {"proxy_id": pid, **changes})
            if isinstance(saved, dict) and isinstance(saved.get("http"), str) and saved["http"]:
                settings = _parse_settings(saved)
            else:
                sleep(RATE_LIMIT_PAUSE_S)  # the same request may come once per 5 seconds
                settings = _parse_settings(call(SETTINGS_COMMAND, token, {"proxy_id": pid}))
        result = _build(settings.http, session_id, settings.country, settings.city)
    except ProxyApiError as exc:
        if not cached:
            raise
        logger.warning("proxy API failed (%s); using the last proxy answer", redact(str(exc), [token]))
        return _build(str(cached["http"]), session_id, str(cached.get("country", "")), str(cached.get("city", "")))
    entry = {"fetched_at": now(), "proxy_id": pid, "http": settings.http, "country": settings.country, "city": settings.city}
    try:
        write_private(cache_path, json.dumps(entry))
    except OSError as exc:
        logger.warning("could not keep the proxy answer for the next run: %s", type(exc).__name__)
    return result
