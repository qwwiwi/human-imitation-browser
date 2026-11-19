"""Proxy settings: from the mobileproxy.space API or from a plain URL.

Credentials never leave these objects in logs: repr() hides them and
redact() removes them from error texts.
"""

import ipaddress
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from email.message import Message
from typing import IO
from urllib.parse import quote, unquote, urlsplit

from hib.errors import ConfigError, HibError, ProxyApiError, ProxyAuthError, ProxyUnreachableError

logger = logging.getLogger(__name__)

MOBILEPROXY_API = "https://mobileproxy.space/api.html"
API_TIMEOUT_S = 20
# Two public reputation services; neither request carries a login or token.
# check_proxy.py goes through the proxy with plain urllib: ip-api.com (free tier is plain http only) works there,
# but a real browser may refuse or redirect plain http, so the browser pre-flight reads an https page instead.
PROXY_REPUTATION_URL = "http://ip-api.com/json/?fields=status,hosting,proxy,mobile"
BROWSER_REPUTATION_URL = "https://api.ipapi.is/"
REQUIRED_FIELDS = ("proxy_hostname", "proxy_http_port", "proxy_login", "proxy_pass")

ACCEPTED_FORMS = "http://host:port, http://login:password@host:port, socks5://host:port"

# Addresses where a proxy without a login is acceptable: this machine and private networks.
# 100.64.0.0/10 is the shared range that mesh VPNs (Tailscale and similar) hand out.
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",  # IPv6 private range; mesh VPNs use it too
        "fe80::/10",  # IPv6 link-local
    )
)
_PRIVATE_NAME_SUFFIXES = (".ts.net", ".local", ".localhost")


@dataclass(frozen=True)
class ProxyConfig:
    """A proxy: HTTP with or without login and password, or SOCKS5 without a login.

    Attributes:
        host: Proxy host name.
        port: Proxy port.
        username: Login (hidden in repr), or None for a proxy without a login.
        password: Password (hidden in repr), or None for a proxy without a login.
        extra_secrets: Other values to hide, for example the login before a session id was put in.
        scheme: `http` or `socks5`. The login is only used with `http`.
    """

    host: str
    port: int
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    extra_secrets: tuple[str, ...] = field(default=(), repr=False)
    scheme: str = "http"

    @property
    def _authority(self) -> str:
        """`host:port`, with brackets around an IPv6 literal (URLs need them)."""
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{host}:{self.port}"

    @property
    def has_credentials(self) -> bool:
        """True when the proxy asks for a login and password."""
        return bool(self.username and self.password)

    def as_playwright(self) -> dict[str, str]:
        """Return the dict Camoufox / Playwright expect in `proxy=`.

        The `username` and `password` keys are present only when the proxy has them.
        """
        result = {"server": f"{self.scheme}://{self._authority}"}
        if self.username and self.password:
            result["username"] = self.username
            result["password"] = self.password
        return result

    def as_url(self) -> str:
        """Return `scheme://[user:pass@]host:port`; credentials, when present, are percent-encoded."""
        if not (self.username and self.password):
            return f"{self.scheme}://{self._authority}"
        user = quote(self.username, safe="")
        pwd = quote(self.password, safe="")
        return f"{self.scheme}://{user}:{pwd}@{self._authority}"

    def secrets(self) -> list[str]:
        """Return the values that must never be printed (an empty list for a proxy without a login)."""
        found: list[str] = []
        for value in (self.password, self.username):
            if value:
                found += [value, quote(value, safe="")]
        for extra in self.extra_secrets:
            found += [extra, quote(extra, safe="")]
        return found


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
    if isinstance(data, list) and len(data) > 1:
        logger.warning("the account has %d proxies; using the first one in the list", len(data))
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
    """Parse HIB_PROXY_URL into a ProxyConfig.

    Accepted: `http://host:port`, `http://login:password@host:port` and `socks5://host:port`.
    A proxy without a login must be reachable only over localhost or a private network
    (see `warn_if_open_proxy`).

    Args:
        url: Proxy URL; login and password may be percent-encoded.

    Returns:
        The proxy settings.

    Raises:
        ConfigError: The URL has another scheme, no host or no port, only one of login and
            password, or is a SOCKS URL with a login. An https:// proxy URL is refused: both
            adapters speak plain http to the proxy, so accepting it would silently send the
            login unencrypted. SOCKS with a login is refused because Playwright's Firefox
            cannot be relied on to send it (not verified here). The message never contains the URL.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        # Raised for an unclosed bracket or a bad port; the message may echo the URL, so it is dropped.
        raise ConfigError("HIB_PROXY_URL is malformed: check the brackets and the port") from None
    if parts.scheme == "https":
        raise ConfigError(f"HIB_PROXY_URL must not start with https://: https:// proxies are not supported. Use: {ACCEPTED_FORMS}")
    if parts.scheme == "socks5h":
        raise ConfigError(f"socks5h:// is not supported in HIB_PROXY_URL: use socks5://host:port. Accepted: {ACCEPTED_FORMS}")
    if parts.scheme not in ("http", "socks5"):
        raise ConfigError(f"HIB_PROXY_URL must look like one of: {ACCEPTED_FORMS}")
    if not parts.hostname or not port:
        raise ConfigError(f"HIB_PROXY_URL needs a host and a port. Accepted: {ACCEPTED_FORMS}")
    if parts.username is None and parts.password is None:
        return ProxyConfig(host=parts.hostname, port=port, scheme=parts.scheme)
    if not parts.username or not parts.password:
        raise ConfigError(f"HIB_PROXY_URL has only one of login and password: give both or neither. Accepted: {ACCEPTED_FORMS}")
    if parts.scheme == "socks5":
        raise ConfigError(
            "a SOCKS proxy with a login and password is not supported in HIB_PROXY_URL "
            f"(the browser layer cannot be relied on to send it). Accepted: {ACCEPTED_FORMS}"
        )
    return ProxyConfig(host=parts.hostname, port=port, username=unquote(parts.username), password=unquote(parts.password))


def _is_private_host(host: str) -> bool:
    """True when the host is loopback, a private or shared (mesh VPN) address, or a local-only name."""
    name = host.lower().rstrip(".")
    if name == "localhost" or name.endswith(_PRIVATE_NAME_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return any(address in net for net in _PRIVATE_NETWORKS if net.version == address.version)


def warn_if_open_proxy(proxy: ProxyConfig) -> bool:
    """Log one WARNING when a proxy without a login sits on a public address.

    A proxy without a login must be reachable only over localhost or a private network:
    on a public address anyone can use it, and the abuse goes to the owner's name.

    Args:
        proxy: The proxy from HIB_PROXY_URL.

    Returns:
        True when the warning was logged.
    """
    if proxy.has_credentials or _is_private_host(proxy.host):
        return False
    logger.warning(
        "the proxy in HIB_PROXY_URL has no login and its address looks public: an open proxy on a public "
        "address can be used by anyone. Keep it reachable only over localhost or a private network"
    )
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so the Bearer token is never sent to another address."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        """Return None: urllib then raises HTTPError for the 3xx answer."""
        return None


def _check_token(token: str) -> None:
    """Refuse a token that cannot be a header value (spaces, line breaks, non-ASCII).

    Raises:
        ConfigError: The token is not printable ASCII without spaces. The message
            never contains the token.
    """
    if not token or not token.isascii() or not token.isprintable() or any(ch.isspace() for ch in token):
        raise ConfigError("MOBILEPROXY_API_TOKEN has characters that are not allowed (spaces, line breaks): re-copy it")


def build_api_url(command: str, params: Mapping[str, str] | None = None) -> str:
    """Build the API address for one command with optional extra parameters.

    Args:
        command: API command.
        params: Extra query parameters, in order.

    Returns:
        The address (it never contains the token: that goes in a header).
    """
    query = "".join(f"&{quote(key)}={quote(value)}" for key, value in (params or {}).items())
    return f"{MOBILEPROXY_API}?command={quote(command)}{query}"


def call_mobileproxy(command: str, token: str, params: Mapping[str, str] | None = None) -> object:
    """Call one mobileproxy.space API command and return decoded JSON.

    Args:
        command: API command, for example `get_my_proxy` or `get_balance`.
        token: API token from the personal cabinet.
        params: Extra query parameters, for example `proxy_id`.

    Returns:
        Decoded JSON answer.

    Raises:
        ConfigError: The token has characters that cannot be sent in a header.
        ProxyApiError: Network error, HTTP error (a redirect counts as one) or a non-JSON answer.
    """
    _check_token(token)
    req = urllib.request.Request(
        build_api_url(command, params),
        headers={"Authorization": f"Bearer {token}"},
    )
    logger.info("asking the proxy API: %s", command)
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=API_TIMEOUT_S) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ProxyAuthError(f"proxy API answered HTTP {exc.code} (check MOBILEPROXY_API_TOKEN)") from None
        raise ProxyApiError(f"proxy API answered HTTP {exc.code}") from None
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


def can_check_exit_ip(proxy: ProxyConfig) -> bool:
    """True when `check_exit_ip` can use this proxy (the standard library speaks only HTTP proxies)."""
    return proxy.scheme == "http"


def _get_json_via_proxy(proxy: ProxyConfig, url: str) -> object:
    """Fetch one JSON page through an HTTP proxy and return the decoded JSON.

    Raises:
        ProxyUnreachableError: The proxy refused, failed the login or timed out, the page
            answered an error, or the NO_PROXY setting would make the request skip the proxy
            (the answer would then describe this server's own address).
        ConfigError: The proxy is SOCKS: urllib cannot speak SOCKS (see `can_check_exit_ip`).
    """
    if not can_check_exit_ip(proxy):
        raise ConfigError("the exit address cannot be checked through a SOCKS proxy here: urllib has no SOCKS support")
    host = urlsplit(url).hostname or ""
    if urllib.request.proxy_bypass(host):
        raise ProxyUnreachableError(
            f"NO_PROXY / no_proxy in the environment excludes {host}: the check would not use the proxy; "
            "remove that entry and run it again"
        )
    proxy_url = proxy.as_url()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    try:
        with opener.open(url, timeout=API_TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ProxyUnreachableError(f"the check page answered HTTP {exc.code} through the proxy") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = redact(str(getattr(exc, "reason", exc)), proxy.secrets())
        hint = " (wrong login/password?)" if "407" in reason else ""
        raise ProxyUnreachableError(f"cannot get through the proxy: {reason}{hint}") from None


def check_exit_ip(proxy: ProxyConfig, url: str = "https://ipinfo.io/json") -> dict[str, str]:
    """Open a neutral IP-info page through the proxy and return country, city, org.

    Args:
        proxy: Proxy to test.
        url: JSON endpoint that describes the caller's IP.

    Returns:
        Mapping with the keys `country`, `city`, `org` (empty strings when absent). `org` is the
        network owner as the service names it.

    Raises:
        ProxyUnreachableError: The proxy refused, failed the login or timed out, or
            the NO_PROXY setting would make the request skip the proxy (the check
            would then report this server's own address).
        ConfigError: The proxy is SOCKS: urllib cannot speak SOCKS (see `can_check_exit_ip`).
    """
    info = _get_json_via_proxy(proxy, url)
    if not isinstance(info, dict):
        raise ProxyUnreachableError("the check page returned unexpected data")
    return {key: str(info.get(key, "")) for key in ("country", "city", "org")}


def _bool_or_none(value: object) -> bool | None:
    """Return the value when it is a real boolean, otherwise None (unknown)."""
    return value if isinstance(value, bool) else None


def parse_reputation(data: object) -> dict[str, bool | None]:
    """Read the `hosting`, `proxy` and `mobile` flags from an ip-api.com answer.

    Args:
        data: Decoded JSON of the reputation service, or None when there was no answer.

    Returns:
        Mapping with the keys `hosting`, `proxy` and `mobile`: True or False when the service said so
        (status `success`, real booleans), otherwise None (unknown). `hosting` and `proxy` are unknown
        together; `mobile` stands on its own.
    """
    unknown: dict[str, bool | None] = {"hosting": None, "proxy": None, "mobile": None}
    if not isinstance(data, dict) or data.get("status") != "success":
        return unknown
    hosting, flagged = _bool_or_none(data.get("hosting")), _bool_or_none(data.get("proxy"))
    if hosting is None or flagged is None:
        return {**unknown, "mobile": _bool_or_none(data.get("mobile"))}
    return {"hosting": hosting, "proxy": flagged, "mobile": _bool_or_none(data.get("mobile"))}


def parse_ipapi_is(data: object) -> dict[str, bool | None]:
    """Read the flags from an api.ipapi.is answer (the page the browser pre-flight opens).

    Mapping: `hosting` = `is_datacenter`; `proxy` = `is_proxy` or `is_vpn` or `is_tor`; `mobile` = `is_mobile`.
    Each flag stands on its own. `proxy` is True when any of its three fields is true, False when all three
    are false, and unknown when a field is missing or not a boolean and none of the others is true.

    Args:
        data: Decoded JSON of the page, or None when there was no usable answer.

    Returns:
        Mapping with the keys `hosting`, `proxy` and `mobile`: True, False or None (unknown).
    """
    if not isinstance(data, dict):
        return {"hosting": None, "proxy": None, "mobile": None}
    parts = [_bool_or_none(data.get(key)) for key in ("is_proxy", "is_vpn", "is_tor")]
    if any(part is True for part in parts):
        flagged: bool | None = True
    elif all(part is False for part in parts):
        flagged = False
    else:
        flagged = None
    return {"hosting": _bool_or_none(data.get("is_datacenter")), "proxy": flagged, "mobile": _bool_or_none(data.get("is_mobile"))}


def extract_json(text: str) -> dict[str, object] | None:
    """Read a JSON object out of a page body, also when the browser wrapped it in HTML.

    Args:
        text: The response body (a viewer or a proxy may have put HTML around the JSON).

    Returns:
        The JSON object, or None when the text holds none (empty, a plain page, broken JSON, a list).
    """
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def check_reputation(proxy: ProxyConfig, url: str = PROXY_REPUTATION_URL) -> dict[str, bool | None]:
    """Ask one public reputation service whether the exit address is flagged as hosting or proxy (and mobile).

    This is one extra read-only request through the proxy. The answer is a third party's opinion
    and can be wrong. Any failure (service down, rate limit, odd answer, SOCKS proxy) is not an
    error: the flags are then None, which means "unknown".

    Args:
        proxy: Proxy to test.
        url: JSON endpoint that describes the caller's IP with `hosting`, `proxy` and `mobile` fields (ip-api.com format).

    Returns:
        Mapping with the keys `hosting`, `proxy` and `mobile`: True, False or None (unknown).
    """
    try:
        data = _get_json_via_proxy(proxy, url)
    except HibError as exc:
        logger.info("reputation lookup failed: %s", redact(str(exc), proxy.secrets()))
        return parse_reputation(None)
    return parse_reputation(data)

