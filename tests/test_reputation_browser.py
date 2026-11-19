"""The browser pre-flight reads the reputation flags from an https page and retries a slow address page once.

No browser and no network: a fake tab answers `goto` from a script; the pause is a fake clock. All values are synthetic.
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
from hib.proxy import BROWSER_REPUTATION_URL, ProxyConfig, extract_json, parse_ipapi_is

CHECK_URL = "https://check.example.test/json"
REPUTATION_URL = "https://reputation.example.test/"
INFO = {"country": "RU", "city": "Test City", "org": "AS64500 Test Home Net"}
CLEAN = {"ip": "x", "is_datacenter": False, "is_proxy": False, "is_vpn": False, "is_tor": False, "is_mobile": False}
FLAT_REPUTATION = {"hosting": None, "proxy": None, "mobile": None}


# ------------------------------------------------------------ parsing the https answer


def test_the_default_browser_page_is_https_and_is_ipapi_is() -> None:
    assert BROWSER_REPUTATION_URL == "https://api.ipapi.is/"


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, {"hosting": False, "proxy": False, "mobile": False}),
        ({"is_datacenter": True}, {"hosting": True, "proxy": False, "mobile": False}),
        ({"is_proxy": True}, {"hosting": False, "proxy": True, "mobile": False}),
        ({"is_vpn": True}, {"hosting": False, "proxy": True, "mobile": False}),
        ({"is_tor": True}, {"hosting": False, "proxy": True, "mobile": False}),
        ({"is_mobile": True}, {"hosting": False, "proxy": False, "mobile": True}),
    ],
)
def test_ipapi_is_flags_map_to_hosting_proxy_and_mobile(changes: dict[str, bool], expected: dict[str, bool]) -> None:
    assert parse_ipapi_is({**CLEAN, **changes}) == expected


def test_a_missing_field_is_unknown_for_that_flag_only() -> None:
    answer = {key: value for key, value in CLEAN.items() if key != "is_mobile"}
    assert parse_ipapi_is(answer) == {"hosting": False, "proxy": False, "mobile": None}


def test_a_positive_proxy_field_wins_over_a_missing_sibling() -> None:
    assert parse_ipapi_is({"is_proxy": True, "is_datacenter": False})["proxy"] is True


def test_a_proxy_flag_with_a_missing_sibling_and_no_positive_is_unknown() -> None:
    assert parse_ipapi_is({"is_proxy": False, "is_vpn": False})["proxy"] is None  # is_tor missing


@pytest.mark.parametrize("answer", [None, [], "text", 7, {}, {"is_datacenter": "yes", "is_proxy": 1, "is_mobile": "no"}])
def test_an_unusable_answer_is_unknown_never_a_crash(answer: object) -> None:
    assert parse_ipapi_is(answer) == FLAT_REPUTATION


def test_extract_json_reads_plain_json() -> None:
    assert extract_json(json.dumps(CLEAN)) == CLEAN


def test_extract_json_reads_json_inside_an_html_wrapper() -> None:
    wrapped = f"<html><head></head><body><pre>{json.dumps(CLEAN, indent=2)}</pre></body></html>"
    assert extract_json(wrapped) == CLEAN


@pytest.mark.parametrize("text", ["", "not json at all", "<html><body>Just a moment...</body></html>", "{broken", "x" * 260, "[1, 2]", "}{"])
def test_extract_json_gives_none_for_what_is_not_a_json_object(text: str) -> None:
    assert extract_json(text) is None


# ------------------------------------------------------------ the page body, not the rendered page


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    def text(self) -> str:
        return self._body


class FakeTab:
    """A tab whose `goto` follows a script: url -> list of outcomes (an exception, or (status, body))."""

    def __init__(self, script: dict[str, list[Exception | tuple[int, str]]]) -> None:
        self.script = {url: list(outcomes) for url, outcomes in script.items()}
        self.visited: list[str] = []
        self.context = SimpleNamespace(cookies=lambda: [])

    def goto(self, url: str, wait_until: str = "load", timeout: float = 0) -> FakeResponse:
        self.visited.append(url)
        outcome = self.script[url].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(*outcome)

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


def test_flags_come_from_the_body_of_the_https_page_even_in_an_html_wrapper(tmp_path: Path, tab_and_launch: dict[str, FakeTab]) -> None:
    body = f"<html><body><pre>{json.dumps({**CLEAN, 'is_datacenter': True, 'is_mobile': True})}</pre></body></html>"
    tab = FakeTab({CHECK_URL: [(200, json.dumps(INFO))], REPUTATION_URL: [(200, body)]})
    seen = _observe(tmp_path, tab, tab_and_launch, FakeClock())
    assert (seen.hosting, seen.proxy_flag, seen.mobile) == (True, False, True)
    assert seen.owner == "AS64500 Test Home Net" and seen.country == "RU"
    assert tab.visited == [CHECK_URL, REPUTATION_URL]


@pytest.mark.parametrize(
    "outcome",
    [(200, "x" * 260), (200, ""), (500, "oops"), PlaywrightTimeoutError("Timeout 5000ms exceeded"), PlaywrightError("boom")],
)
def test_a_flags_page_that_fails_leaves_the_flags_unknown_and_the_result_ok(
    tmp_path: Path, tab_and_launch: dict[str, FakeTab], outcome: Exception | tuple[int, str]
) -> None:
    tab = FakeTab({CHECK_URL: [(200, json.dumps(INFO))], REPUTATION_URL: [outcome]})
    seen = _observe(tmp_path, tab, tab_and_launch, FakeClock())
    assert (seen.hosting, seen.proxy_flag, seen.mobile) == (None, None, None)
    assert seen.country == "RU" and seen.owner == "AS64500 Test Home Net"
    verdict = core.assess(core.Before(visits=0, last_preflight="", network={}, cookie_count=None), seen, strict=True)
    assert verdict.ok is True and any("reputation unknown" in note for note in verdict.problems_soft)


# ------------------------------------------------------------ one retry of the address page


def test_a_load_timeout_of_the_address_page_is_retried_once_after_a_short_pause(tmp_path: Path, tab_and_launch: dict[str, FakeTab]) -> None:
    tab = FakeTab(
        {
            CHECK_URL: [PlaywrightTimeoutError("Timeout 5000ms exceeded"), (200, json.dumps(INFO))],
            REPUTATION_URL: [(200, json.dumps(CLEAN))],
        }
    )
    clock = FakeClock()
    seen = _observe(tmp_path, tab, tab_and_launch, clock)
    assert seen.country == "RU" and seen.hosting is False
    assert tab.visited == [CHECK_URL, CHECK_URL, REPUTATION_URL]
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


def test_the_default_flags_page_of_the_browser_check_is_the_https_one(tmp_path: Path, tab_and_launch: dict[str, FakeTab]) -> None:
    tab = FakeTab({CHECK_URL: [(200, json.dumps(INFO))], BROWSER_REPUTATION_URL: [(200, json.dumps(CLEAN))]})
    tab_and_launch["tab"] = tab
    profile = SimpleNamespace(user_data_dir=tmp_path / "data")
    seen = core.observe_in_browser(ProxyConfig("127.0.0.1", 1), profile, OpenOptions(out_dir=tmp_path / "out"), CHECK_URL, sleep=FakeClock())  # type: ignore[arg-type]
    assert tab.visited[-1] == "https://api.ipapi.is/" and seen.hosting is False
