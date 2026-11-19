"""Review blockers H1-H3 and H6-H9 of the pre-flight: redirects, one sanitiser, neutral hosts, usable address, flags, refusals, cookie store.

No browser and no network: fake tabs, a fake proxy on 127.0.0.1 and synthetic values only.
"""

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import check_proxy
import open_page
from hib import browser
from hib import preflight as core
from hib.browser import OpenOptions, PlaywrightTimeoutError
from hib.errors import PageRefusedError, ProxyUnreachableError
from hib.profiles import ProfileStore
from hib.proxy import ProxyConfig, check_exit_ip, check_reputation, parse_reputation, redact
from test_preflight import FakeObserver, Recorder, _out, _run, before, observer, opener, seen  # noqa: F401 - fixtures
from test_reputation_browser import CHECK_URL, CLEAN, INFO, REPUTATION_URL, FakeClock, FakeTab, _observe, tab_and_launch  # noqa: F401 - fixtures


# ------------------------------------------------------------ H1: no redirect is followed by a reputation request


class _RedirectingProxy(BaseHTTPRequestHandler):
    """A fake proxy: the first request is answered with a redirect, any later one with a clean reputation answer."""

    seen: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        type(self).seen.append(self.path)
        if len(type(self).seen) == 1:
            self.send_response(302)
            self.send_header("Location", "http://other.example.test/json")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"status": "success", "hosting": False, "proxy": False, "mobile": False, "country": "RU"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return None


@contextmanager
def _redirecting_proxy() -> Iterator[tuple[int, list[str]]]:
    handler = type("Handler", (_RedirectingProxy,), {"seen": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], handler.seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_proxy_path_does_not_follow_a_reputation_redirect() -> None:
    with _redirecting_proxy() as (port, seen_paths):
        flags = check_reputation(ProxyConfig("127.0.0.1", port, "demo_user", "s3cretpw"), url="http://rep.example.test/json")
    assert flags == {"hosting": None, "proxy": None, "mobile": None}
    assert len(seen_paths) == 1  # the redirect target was never asked


def test_the_exit_address_check_refuses_a_redirect_too() -> None:
    with _redirecting_proxy() as (port, seen_paths):
        with pytest.raises(ProxyUnreachableError, match="302"):
            check_exit_ip(ProxyConfig("127.0.0.1", port), url="http://rep.example.test/json")
    assert len(seen_paths) == 1


def test_the_browser_reputation_request_is_made_without_redirects_and_a_3xx_is_unknown() -> None:
    tab = FakeTab({}, {REPUTATION_URL: [(302, "")]})
    flags = browser.fetch_reputation(tab, REPUTATION_URL, 5.0, [])  # type: ignore[arg-type]
    assert flags == {"hosting": None, "proxy": None, "mobile": None}
    assert tab.redirect_limits == [0]


# ------------------------------------------------------------ H7: the flags are read one by one


def test_a_good_hosting_flag_survives_a_malformed_proxy_flag() -> None:
    answer = {"status": "success", "hosting": True, "proxy": "maybe", "mobile": False}
    assert parse_reputation(answer) == {"hosting": True, "proxy": None, "mobile": False}
    assert parse_reputation({**answer, "hosting": 1, "proxy": True}) == {"hosting": None, "proxy": True, "mobile": False}


def test_strict_fails_on_hosting_true_with_a_malformed_proxy_flag_in_the_browser_path() -> None:
    tab = FakeTab({}, {REPUTATION_URL: [(200, json.dumps({**CLEAN, "hosting": True, "proxy": "maybe"}))]})
    flags = browser.fetch_reputation(tab, REPUTATION_URL, 5.0, [])  # type: ignore[arg-type]
    verdict = core.assess(before(), seen(hosting=flags["hosting"], proxy_flag=flags["proxy"]), strict=True)
    assert verdict.ok is False and any("--strict" in problem for problem in verdict.problems)


# ------------------------------------------------------------ H2: one sanitiser on every output path


@pytest.mark.parametrize(
    "literal",
    ["203.0.113.9", "::1", "2001:db8::1", "fe80::1", "::ffff:203.0.113.9", "2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::"],
)
def test_redact_strips_ip_literals_in_every_form(literal: str) -> None:
    text = f"cannot reach [{literal}]:8080 or {literal} now"
    assert literal not in redact(text, [])


def test_redact_keeps_a_network_owner_and_plain_times_and_removes_every_credential() -> None:
    for text in ("AS12345 NAME LTD", "AS64500 Test Home Net", "at 12:30:45 on 2026-11-19"):
        assert redact(text, []) == text
    assert redact("a ab b", ["ab"]) == "a *** b"
    assert redact("x", ["x"]) == "***"


def test_an_owner_with_a_compressed_address_or_a_two_character_credential_is_dropped() -> None:
    assert core.sanitize_owner("AS12345 NAME LTD", ["ab"]) == "AS12345 NAME LTD"
    assert core.sanitize_owner("AS1 Net 2001:db8::1", []) == ""
    assert core.sanitize_owner("AS1 Net ::1", []) == ""
    assert core.sanitize_owner("AS1 Net xcdx", ["cd"]) == ""
    assert core.sanitize_owner("AS1 Net", ["Z"]) == "AS1 Net"


def test_the_result_json_and_the_profile_hold_no_address_and_no_short_credential(
    observer: FakeObserver, monkeypatch: pytest.MonkeyPatch, isolated_profile_store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HIB_PROXY_URL", "http://ab:cd@proxy.example.test:8001")
    observer.observed = seen(owner="AS1 Net 2001:db8::1", cookies_at_start=0)
    assert _run(tmp_path, "--profile", "p-1") == 0
    out = _out(capsys)
    assert out["network_owner"] is None
    observer.observed = seen(owner="AS1 Xcd Net", cookies_at_start=0)
    assert _run(tmp_path, "--profile", "p-2") == 0
    assert _out(capsys)["network_owner"] is None
    for name in ("p-1", "p-2"):
        text = (isolated_profile_store / name / "profile.json").read_text(encoding="utf-8")
        assert "2001:db8" not in text and "Xcd" not in text


@pytest.mark.parametrize("message", ["cannot get through the proxy: 203.0.113.9 refused", "bad answer from 2001:db8::1", "peer ::1 reset"])
def test_an_error_text_of_the_pre_flight_holds_no_address(
    observer: FakeObserver, tmp_path: Path, capsys: pytest.CaptureFixture[str], message: str
) -> None:
    observer.observed = ProxyUnreachableError(message)
    assert _run(tmp_path) == 6
    error = str(_out(capsys)["error"])
    assert "203.0.113.9" not in error and "2001:db8" not in error and "::1" not in error


def test_an_unexpected_error_text_holds_no_address_either(
    observer: FakeObserver, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observer.observed = RuntimeError("boom at 203.0.113.9")  # type: ignore[assignment]
    assert _run(tmp_path) == 1
    assert "203.0.113.9" not in str(_out(capsys)["error"])


def test_a_stored_owner_with_an_address_is_not_shown_by_describe(isolated_profile_store: Path) -> None:
    with ProfileStore(isolated_profile_store).acquire("default", lambda: {"navigator": {}}) as profile:
        profile.data.network = {"country": "RU", "city": "Test City", "owner": "Net 2001:db8::1"}
        profile.save()
    shown = ProfileStore(isolated_profile_store).load("default").describe()
    assert "2001:db8" not in json.dumps(shown)


def _check_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], info: dict[str, str] | Exception
) -> tuple[int, str]:
    monkeypatch.setenv("HIB_PROXY_URL", "http://demo_user:s3cretpw@127.0.0.1:18932")

    def fake_info(proxy: object, url: str = "") -> dict[str, str]:
        if isinstance(info, Exception):
            raise info
        return info

    monkeypatch.setattr(check_proxy, "check_exit_ip", fake_info)
    monkeypatch.setattr(check_proxy, "check_reputation", lambda proxy, url="": {"hosting": False, "proxy": False, "mobile": None})
    code = check_proxy.main(["--env-file", str(tmp_path / "missing.env")])
    return code, capsys.readouterr().out


def test_check_proxy_prints_no_address_from_the_provider_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _check_proxy(monkeypatch, tmp_path, capsys, {"country": "RU", "city": "Test City", "org": "AS1 Net 2001:db8::1 or 203.0.113.9"})
    assert code == 0
    assert "2001:db8" not in out and "203.0.113.9" not in out
    assert json.loads(out)["country"] == "RU"


def test_check_proxy_prints_no_address_in_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _check_proxy(monkeypatch, tmp_path, capsys, ProxyUnreachableError("cannot get through the proxy: 203.0.113.9 and 2001:db8::1"))
    assert code == 6
    assert "203.0.113.9" not in out and "2001:db8" not in out


# ------------------------------------------------------------ H3: --ephemeral only for neutral hosts


@pytest.mark.parametrize("url", ["https://example.com/", "https://ipinfo.io/json", "http://ip-api.com/json/"])
def test_ephemeral_is_allowed_for_the_neutral_check_hosts(opener: Recorder, tmp_path: Path, url: str) -> None:
    assert open_page.main(["--env-file", str(tmp_path / "missing.env"), "--ephemeral", url]) == 0
    assert opener.calls == 1


@pytest.mark.parametrize("extra", [[], ["--no-proxy"]])
def test_ephemeral_with_a_real_address_is_a_configuration_error(
    opener: Recorder, tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    code = open_page.main(["--env-file", str(tmp_path / "missing.env"), "--ephemeral", *extra, "https://example.com/", "https://real-shop.example.test/"])
    assert code == 4
    assert opener.calls == 0
    out = json.loads(capsys.readouterr().out.splitlines()[0])
    assert out["exit_code"] == 4 and "--ephemeral" in out["error"]


def test_the_neutral_hosts_can_be_extended_by_the_environment_for_tests(
    opener: Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HIB_EPHEMERAL_HOSTS", "real-shop.example.test, other.example.test")
    assert open_page.main(["--env-file", str(tmp_path / "missing.env"), "--ephemeral", "https://real-shop.example.test/"]) == 0


# ------------------------------------------------------------ H6: the address page must give a usable address


def test_a_page_answering_an_empty_object_is_not_a_usable_address(tmp_path: Path, tab_and_launch: dict[str, FakeTab]) -> None:
    tab = FakeTab({CHECK_URL: [(200, "{}")]}, {REPUTATION_URL: [(200, json.dumps(CLEAN))]})
    observed = _observe(tmp_path, tab, tab_and_launch, FakeClock())
    verdict = core.assess(core.Before(visits=0, last_preflight="", network={}, cookie_count=None), core.sanitized(observed, []))
    assert verdict.ok is False
    assert any("country" in problem for problem in verdict.problems)


def test_a_country_that_the_sanitiser_drops_is_not_a_usable_address(observer: FakeObserver, tmp_path: Path, isolated_profile_store: Path) -> None:
    observer.observed = seen(country="203.0.113.9", cookies_at_start=0)
    assert _run(tmp_path, "--profile", "p-1") == 12
    assert not ProfileStore(isolated_profile_store).load("p-1").data.last_preflight


# ------------------------------------------------------------ H8: a refusal is a stop, not a retry


@pytest.mark.parametrize(
    ("title", "text"),
    [("", "Access denied"), ("Access denied", ""), ("Just a moment...", "Checking your browser"), ("", "Доступ ограничен")],
)
def test_a_timeout_on_a_page_that_shows_a_refusal_is_a_stop_without_a_retry(
    tmp_path: Path, tab_and_launch: dict[str, FakeTab], title: str, text: str
) -> None:
    tab = FakeTab({CHECK_URL: [PlaywrightTimeoutError("t1"), (200, json.dumps(INFO))]})
    tab.shown_title, tab.shown_text = title, text
    clock = FakeClock()
    with pytest.raises(PageRefusedError) as info:
        _observe(tmp_path, tab, tab_and_launch, clock)
    assert tab.visited == [CHECK_URL] and clock.pauses == []
    assert info.value.exit_code == 3
    assert "retry" in str(info.value) or "not tried again" in str(info.value)


def test_a_timeout_on_an_empty_page_is_still_retried_once(tmp_path: Path, tab_and_launch: dict[str, FakeTab]) -> None:
    tab = FakeTab({CHECK_URL: [PlaywrightTimeoutError("t1"), (200, json.dumps(INFO))]}, {REPUTATION_URL: [(200, json.dumps(CLEAN))]})
    clock = FakeClock()
    assert _observe(tmp_path, tab, tab_and_launch, clock).country == "RU"
    assert tab.visited == [CHECK_URL, CHECK_URL]


# ------------------------------------------------------------ H9: the cookie store is looked at before the launch


@contextmanager
def _launch_that_creates_the_store(data: Path, tab: FakeTab) -> Iterator[FakeTab]:
    (data / core.COOKIE_STORE_FILE).write_bytes(b"")  # the browser recreates an empty store on start
    yield tab


def test_a_store_that_the_launch_recreates_is_still_reported_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "cache.bin").write_bytes(b"x")  # the folder has something, the cookie store is gone
    tab = FakeTab({CHECK_URL: [(200, json.dumps(INFO))]}, {REPUTATION_URL: [(200, json.dumps(CLEAN))]})
    monkeypatch.setattr(browser, "launch", lambda proxy, options, profile: _launch_that_creates_the_store(data, tab))
    observed = core.observe_in_browser(
        ProxyConfig("127.0.0.1", 1), SimpleNamespace(user_data_dir=data), OpenOptions(out_dir=tmp_path / "out"), CHECK_URL,  # type: ignore[arg-type]
        reputation_url=REPUTATION_URL, sleep=FakeClock(),
    )
    assert observed.cookie_store_present is False
    used_with_no_cookies = before(visits=3, cookie_count=0)
    verdict = core.assess(used_with_no_cookies, observed)
    assert verdict.ok is False and verdict.profile_memory == "empty"
    assert any("cookie store" in problem for problem in verdict.problems)


def _store(path: Path, rows: int) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT)")
        db.executemany("INSERT INTO moz_cookies VALUES (?, ?, ?)", [(f"n{i}", "v", "h") for i in range(rows)])


def test_the_persisted_cookies_are_counted_before_the_launch_and_only_counted(tmp_path: Path) -> None:
    _store(tmp_path / core.COOKIE_STORE_FILE, 3)
    assert core.count_persisted_cookies(tmp_path) == 3
    (tmp_path / core.COOKIE_STORE_FILE).write_bytes(b"not a database")
    assert core.count_persisted_cookies(tmp_path) is None
    assert core.count_persisted_cookies(tmp_path / "missing") is None


def test_the_launch_sees_the_store_as_it_was_before_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _store(data / core.COOKIE_STORE_FILE, 4)
    tab = FakeTab({CHECK_URL: [(200, json.dumps(INFO))]}, {REPUTATION_URL: [(200, json.dumps(CLEAN))]})
    monkeypatch.setattr(browser, "launch", lambda proxy, options, profile: _launch_that_creates_the_store(tmp_path, tab))
    observed = core.observe_in_browser(
        ProxyConfig("127.0.0.1", 1), SimpleNamespace(user_data_dir=data), OpenOptions(out_dir=tmp_path / "out"), CHECK_URL,  # type: ignore[arg-type]
        reputation_url=REPUTATION_URL, sleep=FakeClock(),
    )
    assert observed.cookie_store_present is True and observed.cookies_persisted == 4


def test_zero_persisted_cookies_against_a_recorded_count_is_a_lost_memory() -> None:
    verdict = core.assess(before(cookie_count=5), seen(cookies_at_start=3, cookies_persisted=0))
    assert verdict.ok is False and any("cookies" in problem for problem in verdict.problems)
    assert core.assess(before(cookie_count=5), seen(cookies_at_start=3, cookies_persisted=2)).ok is True
    assert core.assess(before(cookie_count=0), seen(cookies_at_start=0, cookies_persisted=0)).ok is True
