"""The browser pre-flight reads the reputation flags with a request from the browser context and retries a slow address page once.

No browser and no network: a fake tab answers `goto` and `context.request.get` from scripts; the pause is a fake clock.
All values are synthetic.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from hib import browser
from hib import preflight as core
from hib.browser import OpenOptions, PlaywrightError, PlaywrightTimeoutError
from hib.errors import PageTimeoutError, ProxyUnreachableError
from hib.proxy import BROWSER_REPUTATION_URL, ProxyConfig

CHECK_URL = "https://check.example.test/json"
REPUTATION_URL = "http://reputation.example.test/json"
INFO = {"country": "RU", "city": "Test City", "org": "AS64500 Test Home Net"}
CLEAN = {"status": "success", "country": "Test", "city": "Test City", "isp": "Test Net", "as": "AS64500 Test Net", "mobile": False, "proxy": False, "hosting": False}


# ------------------------------------------------------------ the default address and the context request


def test_the_default_flags_address_is_the_ip_api_json_with_the_fields_the_tool_reads() -> None:
    assert BROWSER_REPUTATION_URL.startswith("http://ip-api.com/json/")
    fields = BROWSER_REPUTATION_URL.split("fields=")[1].split(",")
    assert {"status", "hosting", "proxy", "mobile"} <= set(fields)


def test_the_ipapi_is_mapping_is_gone() -> None:
    import hib.proxy as proxy_module

    assert not hasattr(proxy_module, "parse_ipapi_is") and not hasattr(proxy_module, "extract_json")
    assert not hasattr(browser, "fetch_page_body")


class FakeApiResponse:
    """What `context.request.get` returns: a status and a body that `json()` decodes like Playwright does."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    def json(self) -> object:
        return json.loads(self._body)


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    def text(self) -> str:
        return self._body


class FakeTab:
    """A tab whose `goto` follows a script (url -> outcomes) and whose context has a scripted `request.get`."""

    def __init__(
        self,
        script: dict[str, list[Exception | tuple[int, str]]],
        api: dict[str, list[Exception | tuple[int, str]]] | None = None,
    ) -> None:
        self.script = {url: list(outcomes) for url, outcomes in script.items()}
        self.api = {url: list(outcomes) for url, outcomes in (api or {}).items()}
        self.visited: list[str] = []
        self.requested: list[tuple[str, float]] = []
        self.context = SimpleNamespace(cookies=lambda: [], request=SimpleNamespace(get=self._api_get))

    def goto(self, url: str, wait_until: str = "load", timeout: float = 0) -> FakeResponse:
        self.visited.append(url)
        outcome = self.script[url].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(*outcome)

    def _api_get(self, url: str, timeout: float = 0) -> FakeApiResponse:
        self.requested.append((url, timeout))
        outcome = self.api[url].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeApiResponse(*outcome)

    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"png")


@pytest.fixture()
def tab_and_launch(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Install a fake `browser.launch` that hands out the tab set by the test."""
    holder: dict[str, FakeTab] = {}

    @contextmanager
    def fake_launch(proxy: object, options: object, profile: object) -> Iterator[FakeTab]:
        yield holder["tab"]

    monkeypatch.setattr(browser, "launch", fake_launch)
    return holder


class FakeClock:
    """Stands in for time.sleep: records the pauses, never waits."""

    def __init__(self) -> None:
        self.pauses: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.pauses.append(seconds)


def _observe(tmp_path: Path, tab: FakeTab, holder: dict[str, FakeTab], clock: FakeClock, **kwargs: object) -> core.Seen:
    holder["tab"] = tab
    profile = SimpleNamespace(user_data_dir=tmp_path / "data")
    options = OpenOptions(out_dir=tmp_path / "out", timeout_s=5)
    return core.observe_in_browser(
        ProxyConfig("127.0.0.1", 1), profile, options, CHECK_URL, reputation_url=REPUTATION_URL, sleep=clock, **kwargs  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, (False, False, False)),
        ({"hosting": True, "proxy": True}, (True, True, False)),
        ({"hosting": True}, (True, False, False)),
        ({"proxy": True}, (False, True, False)),
        ({"mobile": True}, (False, False, True)),
    ],
)
def test_flags_come_from_a_request_of_the_browser_context_not_from_a_navigation(
    tmp_path: Path, tab_and_launch: dict[str, FakeTab], changes: dict[str, bool], expected: tuple[bool, bool, bool]
) -> None:
    tab = FakeTab({CHECK_URL: [(200, json.dumps(INFO))]}, {REPUTATION_URL: [(200, json.dumps({**CLEAN, **changes}))]})
    seen = _observe(tmp_path, tab, tab_and_launch, FakeClock())
    assert (seen.hosting, seen.proxy_flag, seen.mobile) == expected
    assert seen.owner == "AS64500 Test Home Net" and seen.country == "RU"
    assert tab.visited == [CHECK_URL]  # the flags address is never navigated to
    assert [url for url, _ in tab.requested] == [REPUTATION_URL]  # one request, from the same context as the address page


def test_the_context_request_gets_the_load_timeout_in_milliseconds(tmp_path: Path, tab_and_launch: dict[str, FakeTab]) -> None:
    tab = FakeTab({CHECK_URL: [(200, json.dumps(INFO))]}, {REPUTATION_URL: [(200, json.dumps(CLEAN))]})
    _observe(tmp_path, tab, tab_and_launch, FakeClock())
    assert tab.requested == [(REPUTATION_URL, 5000)]


@pytest.mark.parametrize(
    "outcome",
    [
        (200, "<html>not json</html>"),
        (200, ""),
        (200, json.dumps({"status": "fail", "message": "reserved range"})),
        (200, json.dumps({"status": "success", "hosting": "yes", "proxy": 1, "mobile": "no"})),
        (200, json.dumps([1, 2])),
        (429, json.dumps(CLEAN)),
        (500, "oops"),
        PlaywrightTimeoutError("Timeout 5000ms exceeded"),
        PlaywrightError("boom"),
    ],
)
def test_a_flags_request_that_fails_leaves_the_flags_unknown_and_the_result_ok(
    tmp_path: Path, tab_and_launch: dict[str, FakeTab], outcome: Exception | tuple[int, str]
) -> None:
    tab = FakeTab({CHECK_URL: [(200, json.dumps(INFO))]}, {REPUTATION_URL: [outcome]})
    seen = _observe(tmp_path, tab, tab_and_launch, FakeClock())
    assert (seen.hosting, seen.proxy_flag, seen.mobile) == (None, None, None)
    assert seen.country == "RU" and seen.owner == "AS64500 Test Home Net"
    verdict = core.assess(core.Before(visits=0, last_preflight="", network={}, cookie_count=None), seen, strict=True)
    assert verdict.ok is True and any("reputation unknown" in note for note in verdict.problems_soft)


def test_a_context_without_a_request_api_gives_unknown_flags_and_never_a_direct_request(
    tmp_path: Path, tab_and_launch: dict[str, FakeTab]
) -> None:
    tab = FakeTab({CHECK_URL: [(200, json.dumps(INFO))]})
    tab.context = SimpleNamespace(cookies=lambda: [])  # no `request`: the flags must stay unknown, never come from this machine
    seen = _observe(tmp_path, tab, tab_and_launch, FakeClock())
    assert (seen.hosting, seen.proxy_flag, seen.mobile) == (None, None, None)


# ------------------------------------------------------------ one retry of the address page


def test_a_load_timeout_of_the_address_page_is_retried_once_after_a_short_pause(tmp_path: Path, tab_and_launch: dict[str, FakeTab]) -> None:
    tab = FakeTab(
        {CHECK_URL: [PlaywrightTimeoutError("Timeout 5000ms exceeded"), (200, json.dumps(INFO))]},
        {REPUTATION_URL: [(200, json.dumps(CLEAN))]},
    )
    clock = FakeClock()
    seen = _observe(tmp_path, tab, tab_and_launch, clock)
    assert seen.country == "RU" and seen.hosting is False
    assert tab.visited == [CHECK_URL, CHECK_URL]
    assert clock.pauses == [core.RETRY_PAUSE_S] and 0 < core.RETRY_PAUSE_S <= 30


def test_two_load_timeouts_end_in_the_timeout_error_after_exactly_two_attempts(tmp_path: Path, tab_and_launch: dict[str, FakeTab]) -> None:
    tab = FakeTab({CHECK_URL: [PlaywrightTimeoutError("t1"), PlaywrightTimeoutError("t2"), (200, json.dumps(INFO))]})
    clock = FakeClock()
    with pytest.raises(PageTimeoutError):
        _observe(tmp_path, tab, tab_and_launch, clock)
    assert tab.visited == [CHECK_URL, CHECK_URL]
    assert len(clock.pauses) == 1


def test_a_proxy_failure_is_not_retried(tmp_path: Path, tab_and_launch: dict[str, FakeTab]) -> None:
    tab = FakeTab({CHECK_URL: [PlaywrightError("NS_ERROR_PROXY_CONNECTION_REFUSED"), (200, json.dumps(INFO))]})
    clock = FakeClock()
    with pytest.raises(ProxyUnreachableError):
        _observe(tmp_path, tab, tab_and_launch, clock)
    assert tab.visited == [CHECK_URL] and clock.pauses == []


def test_the_default_flags_address_of_the_browser_check_is_the_ip_api_one(tmp_path: Path, tab_and_launch: dict[str, FakeTab]) -> None:
    tab = FakeTab({CHECK_URL: [(200, json.dumps(INFO))]}, {BROWSER_REPUTATION_URL: [(200, json.dumps(CLEAN))]})
    tab_and_launch["tab"] = tab
    profile = SimpleNamespace(user_data_dir=tmp_path / "data")
    seen = core.observe_in_browser(ProxyConfig("127.0.0.1", 1), profile, OpenOptions(out_dir=tmp_path / "out"), CHECK_URL, sleep=FakeClock())  # type: ignore[arg-type]
    assert tab.requested[-1][0] == BROWSER_REPUTATION_URL and seen.hosting is False
