"""Open pages in Camoufox with human-like pacing and stop on challenge pages."""

import logging
import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from camoufox.sync_api import Camoufox
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hib.challenge import detect_challenge
from hib.errors import BrowserStartError, HibError, PageTimeoutError, ProxyUnreachableError
from hib.proxy import ProxyConfig, redact

logger = logging.getLogger(__name__)

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
        screenshot: Path to the saved screenshot.
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


def _screenshot_path(out_dir: Path, url: str) -> Path:
    """Build a unique screenshot file name from the time and the host."""
    host = (urlsplit(url).hostname or "page").replace(":", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return out_dir / f"{stamp}-{host}.png"


def open_one(page: Page, url: str, options: OpenOptions, secrets: list[str]) -> PageResult:
    """Open one address, behave like a person, save a screenshot, check for a challenge.

    Args:
        page: Browser tab to use (the same tab keeps cookies between pages).
        url: Address to open.
        options: Opening options.
        secrets: Values to strip from any error text.

    Returns:
        The page result.

    Raises:
        PageTimeoutError: The page did not load in time.
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
    act_like_a_person(page)
    text = _page_text(page)
    title = page.title()
    shot = _screenshot_path(options.out_dir, page.url)
    page.screenshot(path=str(shot))
    check = detect_challenge(title, text)
    if check.is_challenge:
        logger.warning("challenge page detected (%s): stop and show the screenshot to a person", check.reason)
    return PageResult(
        url=page.url,
        status=response.status if response else None,
        title=title,
        text_chars=len(" ".join(text.split())),
        screenshot=str(shot),
        challenge=check.is_challenge,
        reason=check.reason,
    )


@contextmanager
def launch(proxy: ProxyConfig | None, options: OpenOptions) -> Iterator[Page]:
    """Start Camoufox on a virtual display and yield one tab.

    Args:
        proxy: Proxy to use, or None to go out directly (only for neutral test pages).
        options: Opening options.

    Yields:
        A new page (tab).

    Raises:
        BrowserStartError: The browser could not start.
    """
    secrets = proxy.secrets() if proxy else []
    kwargs: dict[str, object] = {
        "headless": "virtual",
        "humanize": True,
        "locale": "ru-RU",
        "geoip": proxy is not None,
    }
    if proxy:
        kwargs["proxy"] = proxy.as_playwright()
    if options.block_images:
        kwargs["block_images"] = True
    logger.info("starting the browser (about 10 s)")
    try:
        manager = Camoufox(**kwargs)
        browser = manager.__enter__()
    except Exception as exc:  # noqa: BLE001 - any start failure means the same thing to the user
        message = redact(f"{type(exc).__name__}: {exc}".splitlines()[0], secrets)
        raise BrowserStartError(
            f"the browser did not start: {message}. Run scripts/install.sh and read its last lines."
        ) from None
    try:
        yield browser.new_page()
    finally:
        manager.__exit__(None, None, None)


def open_pages(
    urls: list[str],
    proxy: ProxyConfig | None,
    options: OpenOptions,
    emit: Callable[[PageResult], None],
) -> bool:
    """Open addresses one by one in one browser, pausing between them.

    Stops at the first challenge page.

    Args:
        urls: Addresses in order.
        proxy: Proxy or None.
        options: Opening options.
        emit: Called with each page result as soon as it is ready.

    Returns:
        True if a challenge page stopped the run.
    """
    options.out_dir.mkdir(parents=True, exist_ok=True)
    secrets = proxy.secrets() if proxy else []
    with launch(proxy, options) as page:
        for index, url in enumerate(urls):
            if index > 0:
                low, high = options.pause_between_s
                logger.info("pausing %.0f-%.0f s before the next page", low, high)
                _sleep(low, high)
            result = open_one(page, url, options, secrets)
            emit(result)
            if result.challenge:
                return True
    return False
