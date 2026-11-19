"""Open pages in Camoufox with human-like pacing and stop on challenge pages."""

import json
import logging
import platform
import random
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from camoufox.exceptions import InvalidIP, InvalidProxy
from camoufox.fingerprints import Screen, generate_fingerprint
from camoufox.sync_api import Camoufox
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hib.challenge import MIN_TEXT_CHARS, detect_challenge
from hib.errors import BrowserStartError, HibError, PageServerError, PageTimeoutError, ProxyUnreachableError
from hib.pace import Pacer
from hib.profiles import Profile
from hib.proxy import ProxyConfig, parse_reputation, redact

logger = logging.getLogger(__name__)

# The fingerprint is drawn once per profile: a desktop Firefox on the host's own OS with a common
# screen size. Camoufox 0.5.9 (utils.py plan_rendering) keeps stock canvas/WebGL readback only when
# the identity claims the host OS; another OS gets randomised readback and a LeakWarning.
HOST_OS_NAMES = {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}
FALLBACK_OS = "linux"
PROFILE_SCREEN = Screen(min_width=1280, max_width=1920, min_height=720, max_height=1080)
TIMEZONE_SCRIPT = "() => Intl.DateTimeFormat().resolvedOptions().timeZone"

# A page whose text is still short is read again this many times, after this pause, before it is judged.
SETTLE_READS = 3
SETTLE_PAUSE_S = (1.5, 2.5)

# Firefox network error names that mean the proxy itself failed.
PROXY_ERROR_MARKERS = ("NS_ERROR_PROXY", "NS_ERROR_UNKNOWN_PROXY_HOST")


@dataclass(frozen=True)
class OpenOptions:
    """How to open pages.

    Attributes:
        out_dir: Folder for screenshots.
        timeout_s: Page load timeout in seconds.
        block_images: Do not download images (saves proxy traffic).
        pause_between_s: Random pause range between pages, seconds.
    """

    out_dir: Path
    timeout_s: float = 60.0
    block_images: bool = False
    pause_between_s: tuple[float, float] = (20.0, 40.0)


@dataclass(frozen=True)
class PageResult:
    """What the agent gets back for one page (printed as one JSON line).

    Attributes:
        url: Final address after redirects.
        status: HTTP status of the main document, None if unknown.
        title: Page title.
        text_chars: Length of the visible text.
        screenshot: Path to the saved screenshot, empty if it could not be taken.
        challenge: True when the page looks like a captcha / anti-bot page.
        reason: Why it was judged a challenge, empty otherwise.
    """

    url: str
    status: int | None
    title: str
    text_chars: int
    screenshot: str
    challenge: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dict."""
        return asdict(self)


def _sleep(low: float, high: float) -> None:
    """Sleep a random time between low and high seconds."""
    time.sleep(random.uniform(low, high))


def _viewport(page: Page) -> tuple[int, int]:
    """Return the visible area size in CSS pixels."""
    size = page.viewport_size
    if size:
        return size["width"], size["height"]
    dims = page.evaluate("() => [window.innerWidth, window.innerHeight]")
    return int(dims[0]), int(dims[1])


def act_like_a_person(page: Page) -> None:
    """Pause, move the cursor a few times and scroll down and back in steps.

    Camoufox's `humanize` option makes each cursor move follow a curved path;
    this function adds the pauses and the uneven wheel steps.

    Args:
        page: An already loaded page.
    """
    _sleep(2.0, 5.0)
    width, height = _viewport(page)
    for _ in range(random.randint(2, 4)):
        page.mouse.move(random.randint(int(width * 0.1), int(width * 0.9)), random.randint(int(height * 0.15), int(height * 0.85)))
        _sleep(0.3, 1.2)
    scrolled = 0
    for _ in range(random.randint(3, 6)):
        step = random.randint(120, 360)
        page.mouse.wheel(0, step)
        scrolled += step
        _sleep(0.4, 1.5)
    _sleep(1.0, 3.0)
    while scrolled > 0:
        step = min(scrolled, random.randint(300, 600))
        page.mouse.wheel(0, -step)
        scrolled -= step
        _sleep(0.3, 0.9)


def _page_text(page: Page) -> str:
    """Return visible body text, or an empty string if the body is not readable."""
    try:
        return page.inner_text("body", timeout=10_000)
    except PlaywrightError:
        return ""


def _read_page(page: Page) -> tuple[str, str]:
    """Return the visible body text and the title of the page as shown now."""
    text = _page_text(page)
    try:
        title = page.title()
    except PlaywrightError:
        title = ""
    return text, title


def screenshot_path(out_dir: Path, url: str) -> Path:
    """Build a unique screenshot file name from the time and the host."""
    host = (urlsplit(url).hostname or "page").replace(":", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return out_dir / f"{stamp}-{host}.png"


def open_one(page: Page, url: str, options: OpenOptions, secrets: list[str]) -> PageResult:
    """Open one address, check for a challenge, behave like a person, save a screenshot.

    The page is classified before any pointer input. A challenge or an explicit
    access denial (see `hib.challenge`) is captured as it is: the page is not touched,
    nothing is retried, and the caller stops. Otherwise the human-like actions run and
    the page is checked again. HTTP 403 and other 4xx answers are not failures when a
    full page rendered without denial wording.

    Args:
        page: Browser tab to use (the same tab keeps cookies between pages).
        url: Address to open.
        options: Opening options.
        secrets: Values to strip from any error text.

    Returns:
        The page result.

    Raises:
        PageTimeoutError: The page did not load in time.
        PageServerError: The site answered HTTP 5xx and the page is not a challenge page.
        ProxyUnreachableError: Firefox reported a proxy failure.
        HibError: Any other navigation error.
    """
    logger.info("opening %s", url)
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=options.timeout_s * 1000)
    except PlaywrightTimeoutError:
        raise PageTimeoutError(f"page did not load in {options.timeout_s:.0f} s: {url}") from None
    except PlaywrightError as exc:
        message = redact(str(exc).splitlines()[0] if str(exc) else type(exc).__name__, secrets)
        if any(marker in message for marker in PROXY_ERROR_MARKERS):
            raise ProxyUnreachableError(f"the proxy failed: {message}") from None
        raise HibError(f"cannot open {url}: {message}") from None
    status = response.status if response else None
    text, title = _read_page(page)
    # First look: only explicit wording counts, the text may still be rendering.
    check = detect_challenge(title, text, min_chars=0, status=status)
    settles = 0
    while not check.is_challenge and len(" ".join(text.split())) < MIN_TEXT_CHARS and settles < SETTLE_READS:
        settles += 1
        _sleep(*SETTLE_PAUSE_S)
        text, title = _read_page(page)
        check = detect_challenge(title, text, min_chars=0, status=status)
    if not check.is_challenge:
        # The page has had time to render: judge it fully (denial status, short text) before any input.
        check = detect_challenge(title, text, status=status)
    if not check.is_challenge:
        try:
            act_like_a_person(page)
        except PlaywrightError as exc:
            # For example the page redirected while scrolling; still look at what is shown now.
            logger.warning("human-like actions interrupted: %s", redact(str(exc).splitlines()[0] if str(exc) else "", secrets))
        text, title = _read_page(page)
        check = detect_challenge(title, text, status=status)
    if check.is_challenge:
        logger.warning("challenge page detected (%s): stop and show the screenshot to a person", check.reason)
    elif status is not None and status >= 500:
        raise PageServerError(f"the site answered HTTP {status} (a server error, not a challenge page)")
    shot = screenshot_path(options.out_dir, page.url)
    try:
        page.screenshot(path=str(shot))
        shot_path = str(shot)
    except PlaywrightError as exc:
        logger.warning("screenshot failed: %s", redact(str(exc).splitlines()[0] if str(exc) else "", secrets))
        shot_path = ""
    return PageResult(
        url=page.url,
        status=status,
        title=title,
        text_chars=len(" ".join(text.split())),
        screenshot=shot_path,
        challenge=check.is_challenge,
        reason=check.reason,
    )


def host_os() -> str:
    """Return the host operating system in Camoufox's names.

    Returns:
        "linux", "windows" or "macos"; any other system maps to "linux"
        (Camoufox itself reads every non-Windows, non-macOS user agent as Linux).
    """
    system = platform.system()
    name = HOST_OS_NAMES.get(system)
    if name is None:
        logger.warning("unknown host system %r: the new profile will describe itself as %s", system, FALLBACK_OS)
        return FALLBACK_OS
    return name


def make_fingerprint() -> dict[str, object]:
    """Draw the browser fingerprint of a new profile (called once, at profile creation).

    The fingerprint claims the same OS the server runs, so the page's canvas and
    WebGL pixels agree with it. Existing profiles keep the fingerprint they stored.

    Returns:
        A JSON-ready fingerprint for Camoufox's `fingerprint=` option.
    """
    return generate_fingerprint(os=host_os(), screen=PROFILE_SCREEN)


@contextmanager
def launch(proxy: ProxyConfig | None, options: OpenOptions, profile: Profile | None = None) -> Iterator[Page]:
    """Start Camoufox on a virtual display and yield one tab.

    With a profile the browser keeps its own folder (cookies, cache, history) and
    always presents the fingerprint stored in the profile. Without one (only for
    `--ephemeral`) every start is a brand-new browser with a new fingerprint.

    Args:
        proxy: Proxy to use, or None to go out directly (only for neutral test pages).
        options: Opening options.
        profile: The profile to use, or None for a throw-away browser.

    Yields:
        The profile's tab (a new tab for a throw-away browser).

    Raises:
        BrowserStartError: The browser could not start.
    """
    secrets = proxy.secrets() if proxy else []
    kwargs: dict[str, object] = {
        "headless": "virtual",
        "humanize": True,
        "locale": profile.data.locale if profile else "ru-RU",
        "geoip": proxy is not None,
    }
    if profile:
        # Camoufox 0.5.9: persistent_context -> sync_api.py _launch -> launch_persistent_context(user_data_dir=...);
        # fingerprint= is used as is, and the seeded draws (fonts, voices, GPU) follow it (fingerprints.identity_salt).
        kwargs["persistent_context"] = True
        kwargs["user_data_dir"] = str(profile.user_data_dir)
        kwargs["fingerprint"] = profile.fingerprint
    if proxy:
        kwargs["proxy"] = proxy.as_playwright()
    if options.block_images:
        kwargs["block_images"] = True
    logger.info("starting the browser (about 10 s)")
    try:
        manager = Camoufox(**kwargs)
        with warnings.catch_warnings():
            # Reusing our own stored fingerprint is the point of a profile; Camoufox's general
            # "passing your own fingerprint is not recommended" notice would only confuse here.
            warnings.filterwarnings("ignore", message="Passing your own fingerprint", category=RuntimeWarning)  # LeakWarning
            browser = manager.__enter__()
    except (InvalidIP, InvalidProxy) as exc:
        # With geoip=True Camoufox first looks up the exit address through the proxy.
        message = redact(f"{type(exc).__name__}: {exc}".splitlines()[0], secrets)
        raise ProxyUnreachableError(f"cannot get through the proxy: {message}") from None
    except Exception as exc:  # noqa: BLE001 - any start failure means the same thing to the user
        message = redact(f"{type(exc).__name__}: {exc}".splitlines()[0], secrets)
        raise BrowserStartError(
            f"the browser did not start: {message}. Usually system libraries (GTK 3) or Xvfb are missing: "
            "run scripts/install.sh and the apt-get command it prints at the end"
        ) from None
    try:
        if profile:
            # A persistent context opens with one tab already; use it instead of adding a second.
            yield browser.pages[0] if browser.pages else browser.new_page()  # type: ignore[union-attr]
        else:
            yield browser.new_page()
    finally:
        manager.__exit__(None, None, None)


def _observed_timezone(page: Page) -> str:
    """Read the time zone the browser shows to sites; empty if it cannot be read."""
    try:
        return str(page.evaluate(TIMEZONE_SCRIPT))
    except PlaywrightError:
        return ""


def fetch_json_page(page: Page, url: str, timeout_s: float, secrets: list[str]) -> object:
    """Open a neutral JSON address in the tab and return the decoded JSON.

    No human-like actions and no challenge detection: this is for the address-check pages
    of the pre-flight, never for a real site.

    Args:
        page: Browser tab to use.
        url: Address of a page that answers with JSON.
        timeout_s: Load timeout in seconds.
        secrets: Values to strip from any error text.

    Returns:
        The decoded JSON.

    Raises:
        PageTimeoutError: The page did not load in time.
        ProxyUnreachableError: Firefox reported a proxy failure.
        HibError: Another navigation error, an answer other than HTTP 200, or a body that is not JSON.
    """
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
    except PlaywrightTimeoutError:
        raise PageTimeoutError(f"the address-check page did not load in {timeout_s:.0f} s") from None
    except PlaywrightError as exc:
        message = redact(str(exc).splitlines()[0] if str(exc) else type(exc).__name__, secrets)
        if any(marker in message for marker in PROXY_ERROR_MARKERS):
            raise ProxyUnreachableError(f"the proxy failed: {message}") from None
        raise HibError(f"cannot open the address-check page: {message}") from None
    if response is None or response.status != 200:
        raise HibError(f"the address-check page answered HTTP {response.status if response else 'nothing'}")
    try:
        return json.loads(response.text())
    except (PlaywrightError, ValueError):
        raise HibError("the address-check page did not return JSON") from None


def fetch_reputation(page: Page, url: str, timeout_s: float, secrets: list[str]) -> dict[str, bool | None]:
    """Ask the reputation service from the browser context and return the `hosting`, `proxy` and `mobile` flags.

    This is a request made by the context of the tab, not a page navigation: the plain-http answer is not
    shown as a page, and the request goes through the proxy of this very context. Nothing is ever sent from
    this machine directly, and no redirect is followed. Any failure (no request API, timeout, HTTP status other than 200
    (a redirect included), an answer that
    is not JSON, a status other than `success`) is not an error: the flags are then None (unknown).

    Args:
        page: Browser tab whose context (and so whose proxy) is used.
        url: Address of a JSON service with `status`, `hosting`, `proxy` and `mobile` fields (ip-api.com format).
        timeout_s: Request timeout in seconds.
        secrets: Values to strip from any log text.

    Returns:
        Mapping with the keys `hosting`, `proxy` and `mobile`: True, False or None (unknown).
    """
    try:
        # max_redirects=0: a redirect is never followed (its target could be a host that skips the proxy); a 3xx is "unknown".
        response = page.context.request.get(url, timeout=timeout_s * 1000, max_redirects=0)
        if response.status != 200:
            raise HibError(f"the reputation service answered HTTP {response.status}")
        return parse_reputation(response.json())
    except (PlaywrightError, HibError, AttributeError, ValueError) as exc:
        logger.info("reputation lookup failed: %s", redact(str(exc).splitlines()[0] if str(exc) else type(exc).__name__, secrets))
        return parse_reputation(None)


def refusal_reason(page: Page) -> str:
    """Say why the page shown now is a refusal or a challenge page, or return an empty string.

    Only looks (title and visible text, see `hib.challenge`); it never interacts with the page.

    Args:
        page: The tab, for example after a load that timed out.

    Returns:
        The reason, empty when the page does not look like a refusal or a challenge.
    """
    text, title = _read_page(page)
    check = detect_challenge(title, text, min_chars=0)
    return check.reason if check.is_challenge else ""


def stored_cookie_count(page: Page) -> int | None:
    """Count the cookies the browser holds (a number only: names and values are never read out).

    Args:
        page: A tab of the profile's browser.

    Returns:
        The count, or None when the browser cannot say.
    """
    try:
        return len(page.context.cookies())
    except (PlaywrightError, AttributeError):
        return None


def open_pages(
    urls: list[str],
    proxy: ProxyConfig | None,
    options: OpenOptions,
    emit: Callable[[PageResult], None],
    profile: Profile | None = None,
    pacer: Pacer | None = None,
) -> bool:
    """Open addresses one by one in one browser, pausing between them.

    Stops at the first challenge page; the profile is kept as it is.

    Args:
        urls: Addresses in order.
        proxy: Proxy or None.
        options: Opening options.
        emit: Called with each page result as soon as it is ready.
        profile: Profile whose browser memory and fingerprint are used, or None (`--ephemeral`).
        pacer: Keeps the gap between navigations to one host across runs, or None.

    Returns:
        True if a challenge page stopped the run.
    """
    options.out_dir.mkdir(parents=True, exist_ok=True)
    secrets = proxy.secrets() if proxy else []
    with launch(proxy, options, profile) as page:
        if profile:
            profile.record_visit()  # counted only now: a browser that did not start is not a visit
            profile.note_timezone(_observed_timezone(page))
        for index, url in enumerate(urls):
            if index > 0:
                low, high = options.pause_between_s
                logger.info("pausing %.0f-%.0f s before the next page", low, high)
                _sleep(low, high)
            if pacer:
                # Right before the navigation (also the first one, after a slow browser start):
                # the gap is checked and the time recorded at the moment the page is requested.
                pacer.wait_turn(urlsplit(url).hostname or "")
            result = open_one(page, url, options, secrets)
            emit(result)
            if result.challenge:
                _remember_cookie_count(page, profile)
                return True
        _remember_cookie_count(page, profile)
    return False


def _remember_cookie_count(page: Page, profile: Profile | None) -> None:
    """Keep the number of cookies in the profile: the next pre-flight checks that the memory did not vanish."""
    count = stored_cookie_count(page) if profile else None
    if profile and count is not None:
        profile.note_cookie_count(count)
