"""Pre-flight check: look at the address and at the profile memory before the first real site.

The check opens a neutral address-check page in the SAME profile and through the SAME proxy that the
real visit would use, then answers four questions:

    1. does the proxy work (the page loaded through it);
    2. how does the address look from outside (country, city, network owner, hosting and proxy flags);
    3. is it still the same visitor (country, city and owner against what the profile saw last time);
    4. is the profile memory intact (data folder, cookie store, number of cookies, visit counter).

`assess` is a pure function and holds all the rules; `observe_in_browser` is the only part that
needs a browser. Output never carries an IP address, a cookie name or value, or a proxy detail.
"""

import hashlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from hib.errors import ConfigError, HibError, PageRefusedError, PageTimeoutError
from hib.profiles import Profile, clean_place
from hib.proxy import BROWSER_REPUTATION_URL, ProxyConfig, redact

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from hib.browser import OpenOptions

logger = logging.getLogger(__name__)

DEFAULT_CHECK_URL = "https://ipinfo.io/json"
RETRY_PAUSE_S = 5.0  # pause before the one retry of an address page that timed out (a first load through a proxy can be slow)
BINDING_TAG = "hib-preflight-v1"  # version of what `proxy_binding` hashes
COOKIE_STORE_FILE = "cookies.sqlite"  # Firefox keeps the cookies of a profile folder here
NETWORK_KEYS = ("country", "city", "owner")
_OWNER_MAX_CHARS = 80


def sanitize_owner(value: str, secrets: list[str]) -> str:
    """Return the network owner text only if it is safe to store and print, otherwise an empty string.

    The text comes from a third-party page. It is dropped when it is long, has line breaks or other
    unprintable characters, or when the one sanitising function (`hib.proxy.redact`) would change it:
    it holds an IP address (IPv4, IPv6 in any form) or an exact match of any secret.

    Args:
        value: Text from the address-check page (for example `AS64500 Example Net`).
        secrets: Values that must never be stored or shown (proxy login, password, token).

    Returns:
        The text, or "".
    """
    text = value.strip()
    if not text or len(text) > _OWNER_MAX_CHARS or not text.isprintable():
        return ""
    if redact(text, secrets) != text:
        return ""
    lowered = text.lower()
    if any(len(secret) >= 3 and secret.lower() in lowered for secret in secrets):
        return ""
    return text


@dataclass(frozen=True)
class Before:
    """What the profile had recorded before this check.

    Attributes:
        visits: The visit counter (runs that started the browser for a real site).
        last_preflight: Time of the last successful pre-flight, empty if none.
        network: Country, city and owner seen by the last successful pre-flight.
        cookie_count: Cookies held at the end of the last run, None if unknown.
        visits_at_preflight: The visit counter when the last pre-flight passed; the counter never goes down.
    """

    visits: int
    last_preflight: str
    network: dict[str, str]
    cookie_count: int | None
    visits_at_preflight: int = 0


@dataclass(frozen=True)
class Seen:
    """What this check observed.

    Attributes:
        country: Country code from the check page, empty if unknown.
        city: City from the check page, empty if unknown.
        owner: Network owner from the check page, empty if unknown.
        hosting: True or False when the reputation service said so, None if unknown.
        proxy_flag: Same for the proxy flag.
        cookies_at_start: Cookies the browser held when it started, None if it could not say.
        cookies_at_end: Cookies the browser held when the check ended, None if it could not say.
        cookie_store_present: The cookie store file existed in the profile folder BEFORE the browser started (the start recreates a missing one).
        cookies_persisted: Cookies the cookie store file held before the browser started (a count only), None if there was no readable store.
        data_dir_empty: The browser data folder was empty before the run.
        screenshot: Path of the screenshot of the check page, empty if it could not be taken.
        mobile: True when the reputation service says the address belongs to a mobile operator, False when not, None if unknown.
    """

    country: str
    city: str
    owner: str
    hosting: bool | None
    proxy_flag: bool | None
    cookies_at_start: int | None
    cookies_at_end: int | None
    cookie_store_present: bool
    data_dir_empty: bool
    screenshot: str
    mobile: bool | None = None
    cookies_persisted: int | None = None


@dataclass(frozen=True)
class Verdict:
    """The result of `assess`.

    Attributes:
        ok: True when nothing stops the real visit.
        address_changed: Country, city or owner differs from the last pre-flight.
        change: `{"old": {...}, "new": {...}}` (keys country, city, owner) when the address changed, else None.
        profile_memory: `new`, `ok` or `empty`.
        problems: Reasons the check failed.
        problems_soft: Warnings that do not fail the check.
    """

    ok: bool
    address_changed: bool
    change: dict[str, dict[str, str]] | None
    profile_memory: str
    problems: list[str] = field(default_factory=list)
    problems_soft: list[str] = field(default_factory=list)


def before_from_profile(profile: Profile) -> Before:
    """Take the record of a profile as it is before the check."""
    data = profile.data
    return Before(
        visits=data.visits,
        last_preflight=data.last_preflight,
        network=dict(data.network),
        cookie_count=data.cookie_count,
        visits_at_preflight=data.preflight_visits,
    )


def sanitized(seen: Seen, secrets: list[str]) -> Seen:
    """Return the observation with the third-party texts cleaned: the one place before anything is stored or printed.

    Args:
        seen: The raw observation (country, city and owner are text from a third-party page).
        secrets: Values that must never be stored or shown.

    Returns:
        The same observation; a country or city that does not look like a place name, and an owner text that
        `sanitize_owner` refuses, become empty (unknown).
    """
    return replace(
        seen,
        country=clean_place(seen.country.strip(), secrets),
        city=clean_place(seen.city.strip(), secrets),
        owner=sanitize_owner(seen.owner, secrets),
    )


def network_of(seen: Seen) -> dict[str, str]:
    """Return the network keys of an observation."""
    return {"country": seen.country, "city": seen.city, "owner": seen.owner}


def _memory_problems(before: Before, seen: Seen) -> list[str]:
    """Reasons to believe the memory of a used profile is gone (empty list when intact)."""
    reasons = []
    if before.visits < before.visits_at_preflight:
        reasons.append("the visit counter went down (the profile was reset or restored from an older copy)")
    if seen.data_dir_empty:
        reasons.append("the browser data folder is empty")
    if not seen.cookie_store_present:
        reasons.append("the cookie store file is missing")
    if (before.cookie_count or 0) > 0 and (seen.cookies_at_start == 0 or seen.cookies_persisted == 0):
        reasons.append("the stored cookies are gone")
    return reasons


def assess(before: Before, seen: Seen, *, strict: bool = False, accept_address_change: bool = False) -> Verdict:
    """Apply the pre-flight rules.

    Rules: an address page that gave no country is not a usable answer and fails; a country, city or owner
    that differs from the last pre-flight is a change (a value the lookup did not give is unknown, not a
    change); a profile that was used before but has no memory
    fails; hosting/proxy flags are a warning, and fail only with `strict`; unknown flags never fail.

    Args:
        before: What the profile recorded earlier.
        seen: What this check observed.
        strict: Treat a hosting or proxy flag as a failure.
        accept_address_change: The owner accepted the new address: a change is reported but does not fail.

    Returns:
        The verdict.
    """
    problems: list[str] = []
    soft: list[str] = []

    if not seen.country:
        problems.append("the address-check page gave no usable address (no country): the address is unknown")

    old = {key: before.network.get(key, "") for key in NETWORK_KEYS}
    new = network_of(seen)
    changed = any(old[key] and new[key] and old[key] != new[key] for key in NETWORK_KEYS)
    change = {"old": old, "new": new} if changed else None
    if changed and accept_address_change:
        soft.append("the address changed and the owner accepted it: the new country, city and owner are now recorded")
    elif changed:
        problems.append("the address changed: country, city or network owner differ from the last pre-flight (a different visitor to a site)")

    had_history = before.visits > 0 or bool(before.last_preflight)
    memory = "new"
    if had_history:
        reasons = _memory_problems(before, seen)
        memory = "empty" if reasons else "ok"
        if reasons:
            problems.append("the profile memory is empty: " + "; ".join(reasons))

    flagged = [name for name, flag in (("hosting", seen.hosting), ("proxy", seen.proxy_flag)) if flag]
    if flagged:
        text = f"a public reputation service flags the address as {' and '.join(flagged)}: many sites show a check or refuse such addresses"
        if strict:
            problems.append(f"{text} (--strict)")
        else:
            soft.append(text)
    if seen.hosting is None or seen.proxy_flag is None:
        soft.append("reputation unknown: the lookup service gave no usable answer")

    return Verdict(ok=not problems, address_changed=changed, change=change, profile_memory=memory, problems=problems, problems_soft=soft)


def build_report(profile_name: str, seen: Seen, verdict: Verdict) -> dict[str, object]:
    """Build the one JSON line the command prints.

    Args:
        profile_name: The profile that was checked.
        seen: What the check observed.
        verdict: The result of `assess`.

    Returns:
        A JSON-ready dict: no IP address, no cookie names or values, no proxy details.
    """
    report: dict[str, object] = {
        "ok": verdict.ok,
        "profile": profile_name,
        "proxy_works": True,  # the check page loaded through the proxy; otherwise the command ends with the proxy's own exit code
        "country": seen.country or None,
        "city": seen.city or None,
        "network_owner": seen.owner or None,
        "hosting": seen.hosting,
        "proxy_flag": seen.proxy_flag,
        "mobile": seen.mobile,
        "address_changed": verdict.address_changed,
    }
    if verdict.change is not None:
        report["address_change"] = verdict.change
    report.update(
        {
            "profile_memory": verdict.profile_memory,
            "cookies_stored": seen.cookies_at_start,
            "screenshot": seen.screenshot,
            "problems": verdict.problems,
            "problems_soft": verdict.problems_soft,
        }
    )
    return report


def check_url_or_default(value: str | None) -> str:
    """Return the address-check page: HIB_CHECK_URL if set, otherwise the default.

    Raises:
        ConfigError: The override is not an http:// or https:// address.
    """
    if value is None:
        return DEFAULT_CHECK_URL
    if not re.fullmatch(r"https?://[^\s/]+\S*", value):
        raise ConfigError("HIB_CHECK_URL must be an http:// or https:// address of a page that answers with JSON")
    return value


def reputation_url_or_default(value: str | None) -> str:
    """Return the address that gives the hosting, proxy and mobile flags: HIB_REPUTATION_URL if set, otherwise the default.

    Raises:
        ConfigError: The override is not an http:// or https:// address.
    """
    if value is None:
        return BROWSER_REPUTATION_URL
    if not re.fullmatch(r"https?://[^\s/]+\S*", value):
        raise ConfigError(
            "HIB_REPUTATION_URL must be an http:// or https:// address of a service that answers with JSON "
            "having status (success), hosting, proxy and mobile (the ip-api.com format)"
        )
    return value


def proxy_binding(profile: Profile, proxy: ProxyConfig) -> str:
    """Return the hash that ties a pre-flight record to one profile and one proxy configuration.

    The hash covers the profile folder name, the profile's browser fingerprint and the proxy scheme, host,
    port, login and password. It is a one-way sha256: the record never holds the proxy URL or a credential.
    A record copied into another profile folder, or kept after the proxy settings changed, no longer matches.

    Args:
        profile: The profile the check is made for.
        proxy: The proxy the real visit would use.

    Returns:
        64 lower-case hex characters.
    """
    fingerprint = hashlib.sha256(json.dumps(profile.fingerprint, sort_keys=True).encode("utf-8")).hexdigest()
    parts = [BINDING_TAG, profile.path.name, fingerprint, proxy.scheme, proxy.host, proxy.port, proxy.username or "", proxy.password or ""]
    return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()


def require_fresh_preflight(profile: Profile, max_age_h: float, proxy: ProxyConfig | None = None) -> None:
    """Refuse a real visit when the profile has no recent successful pre-flight for this proxy configuration.

    Without `proxy` only the cheap part is checked (a fresh, not revoked record): it can run before any
    proxy call. With `proxy` the record must also be bound to this profile and exactly this proxy.
    A refusal changes nothing in the profile.

    Args:
        profile: The profile about to be used.
        max_age_h: Longest accepted age of the pre-flight, hours.
        proxy: The proxy the real visit will use, or None for the cheap check.

    Raises:
        ConfigError: There is no valid pre-flight. The message names the command to run.
    """
    command = f"`.venv/bin/python scripts/preflight.py --profile {profile.name}`"
    if not (profile.data.preflight_binding and profile.preflight_is_fresh(max_age_h)):
        raise ConfigError(
            f"no successful pre-flight check for profile {profile.name} in the last {max_age_h:g} hours: "
            f"run {command} first and read the result"
        )
    if proxy is not None and not profile.admits(max_age_h, proxy_binding(profile, proxy)):
        raise ConfigError(
            f"the pre-flight check of profile {profile.name} was made for another proxy configuration or another profile folder: "
            f"run {command} again and read the result"
        )


def count_persisted_cookies(data_dir: Path) -> int | None:
    """Count the cookies in the cookie store file of a browser data folder (a number only: no name or value is read).

    Call it while the browser is not running. The file is opened read-only.

    Args:
        data_dir: The browser data folder of a profile.

    Returns:
        The count, or None when there is no store file or it cannot be read as a cookie store.
    """
    store = data_dir / COOKIE_STORE_FILE
    if not store.is_file():
        return None
    try:
        db = sqlite3.connect(f"{store.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        row = db.execute("SELECT COUNT(*) FROM moz_cookies").fetchone()  # Firefox's own table name
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return int(row[0]) if row else None


def _is_empty_folder(path: Path) -> bool:
    """True when the folder is missing or has nothing in it."""
    try:
        return not any(path.iterdir())
    except OSError:
        return True


def _fetch_check_page(page: "Page", check_url: str, timeout_s: float, secrets: list[str], sleep: Callable[[float], None]) -> object:
    """Load the address-check page; a load that timed out is tried once more, unless the page shows a refusal.

    The page is looked at before any retry: a refusal or a challenge is a stop, never a retry.

    Raises:
        PageRefusedError: The page shows a refusal or a challenge (after a first or a second timeout).
        PageTimeoutError: The page did not load in time, twice, and shows no refusal.
    """
    from hib import browser

    for attempt in (1, 2):
        try:
            return browser.fetch_json_page(page, check_url, timeout_s, secrets)
        except PageTimeoutError:
            reason = browser.refusal_reason(page)
            if reason:
                raise PageRefusedError(
                    f"the address-check page shows a refusal or a challenge ({reason}): the run stops here, "
                    "it is not tried again and the tool does not try to get past it; a person must look at it"
                ) from None
            if attempt == 2:
                raise
            logger.info("the address-check page timed out: trying once more in %g s", RETRY_PAUSE_S)
            sleep(RETRY_PAUSE_S)
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


def observe_in_browser(
    proxy: ProxyConfig,
    profile: Profile,
    options: "OpenOptions",
    check_url: str,
    reputation_url: str = BROWSER_REPUTATION_URL,
    sleep: Callable[[float], None] = time.sleep,
) -> Seen:
    """Open the address-check pages in the profile's browser through the proxy and describe what they show.

    The check page gives country, city and owner (one retry after a load timeout); a request from the same
    browser context (so through the same proxy, not a page navigation) gives the hosting, proxy and mobile
    flags (unknown if it fails or answers something unusable). No human-like pauses, no pacing: neither is a real site.

    Args:
        proxy: The proxy the real visit would use.
        profile: The profile whose browser memory is used.
        options: Opening options (screenshot folder, timeout).
        check_url: Page that answers with JSON having `country`, `city` and `org`.
        reputation_url: Address of a service that answers with JSON having `status` (`success`) and the boolean
            `hosting`, `proxy` and `mobile` (the ip-api.com format); it is asked with a request of the browser context.
        sleep: The pause function (tests pass a fake clock).

    Returns:
        The observation.

    Raises:
        ProxyUnreachableError: The proxy failed.
        PageTimeoutError: The check page did not load in time, twice.
        PageRefusedError: The check page shows a refusal or a challenge (no retry).
        BrowserStartError: The browser did not start.
        HibError: The check page answered something unusable.
    """
    from hib import browser  # imported here so that a broken browser install still gives a clean JSON error

    secrets = proxy.secrets()
    options.out_dir.mkdir(parents=True, exist_ok=True)
    # Looked at before the launch: starting the browser recreates a missing cookie store and would hide its loss.
    data_dir_empty = _is_empty_folder(profile.user_data_dir)
    store_present = (profile.user_data_dir / COOKIE_STORE_FILE).is_file()
    persisted = count_persisted_cookies(profile.user_data_dir)
    with browser.launch(proxy, options, profile) as page:
        at_start = browser.stored_cookie_count(page)
        info = _fetch_check_page(page, check_url, options.timeout_s, secrets, sleep)
        if not isinstance(info, dict):
            raise HibError("the address-check page returned unexpected data")
        shot = browser.screenshot_path(options.out_dir, check_url)
        try:
            page.screenshot(path=str(shot))
            screenshot = str(shot)
        except browser.PlaywrightError as exc:
            logger.warning("screenshot failed: %s", redact(str(exc).splitlines()[0] if str(exc) else "", secrets))
            screenshot = ""
        flags = browser.fetch_reputation(page, reputation_url, options.timeout_s, secrets)
        at_end = browser.stored_cookie_count(page)
    return Seen(
        country=str(info.get("country") or ""),
        city=str(info.get("city") or ""),
        owner=str(info.get("org") or ""),
        hosting=flags["hosting"],
        proxy_flag=flags["proxy"],
        mobile=flags["mobile"],
        cookies_at_start=at_start,
        cookies_at_end=at_end,
        cookie_store_present=store_present,
        cookies_persisted=persisted,
        data_dir_empty=data_dir_empty,
        screenshot=screenshot,
    )
