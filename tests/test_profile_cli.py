"""open_page.py and profiles.py command lines with a fake browser: default profile, ephemeral, lock, JSON."""

import json
from pathlib import Path

import pytest

import open_page
from hib import browser, profile_cli
from hib.browser import PageResult
from hib.profiles import Profile, ProfileStore

PROXY_URL = "http://demo_user:s3cretpw@proxy.example.test:8001"
URL = "https://shop.example.test/"


class Recorder:
    """Stands in for hib.browser.open_pages; remembers how it was called."""

    def __init__(self, challenge: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.challenge = challenge

    def __call__(self, urls, proxy, options, emit, profile=None, pacer=None):  # type: ignore[no-untyped-def]
        self.calls.append({"urls": urls, "proxy": proxy, "profile": profile, "pacer": pacer})
        emit(PageResult(URL, 200, "t", 10, "", self.challenge, "x" if self.challenge else ""))
        return self.challenge


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(browser, "open_pages", recorder)
    monkeypatch.setattr(browser, "make_fingerprint", lambda: {"navigator": {"userAgent": "synthetic"}})
    monkeypatch.setenv("HIB_PROXY_URL", PROXY_URL)
    return recorder


def _run(tmp_path: Path, *args: str) -> int:
    return open_page.main(["--env-file", str(tmp_path / "missing.env"), *args])


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_without_flags_the_default_profile_is_used(fake: Recorder, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, URL) == 0
    profile = fake.calls[0]["profile"]
    assert isinstance(profile, Profile) and profile.name == "default"
    assert _lines(capsys)[0]["profile"] == "default"


def test_named_profile_is_used(fake: Recorder, tmp_path: Path) -> None:
    assert _run(tmp_path, "--profile", "shop-1", URL) == 0
    assert fake.calls[0]["profile"].name == "shop-1"  # type: ignore[union-attr]


def test_bad_profile_name_is_an_argument_error(fake: Recorder, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as info:
        _run(tmp_path, "--profile", "../evil", URL)
    assert info.value.code == 2
    assert not fake.calls


def test_second_run_on_a_busy_profile_exits_10_with_json(isolated_profile_store: Path, fake: Recorder, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = ProfileStore(isolated_profile_store)
    with store.acquire("default", lambda: {"navigator": {}}):
        code = _run(tmp_path, URL)
    assert code == 10
    out = _lines(capsys)[0]
    assert out["exit_code"] == 10 and "default" in str(out["error"])
    assert not fake.calls


def test_challenge_exits_3_and_keeps_the_profile(isolated_profile_store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    recorder = Recorder(challenge=True)
    monkeypatch.setattr(browser, "open_pages", recorder)
    monkeypatch.setattr(browser, "make_fingerprint", lambda: {"navigator": {}})
    monkeypatch.setenv("HIB_PROXY_URL", PROXY_URL)
    assert _run(tmp_path, URL) == 3
    assert ProfileStore(isolated_profile_store).names() == ["default"]


def test_ephemeral_runs_without_a_profile_and_warns(isolated_profile_store: Path, fake: Recorder, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
    assert _run(tmp_path, "--ephemeral", URL) == 0
    assert fake.calls[0]["profile"] is None
    assert "neutral test page" in caplog.text
    assert "new visitor" in caplog.text
    assert _lines(capsys)[0]["profile"] is None
    assert ProfileStore(isolated_profile_store).names() == []


def test_ephemeral_and_profile_together_are_refused(fake: Recorder, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as info:
        _run(tmp_path, "--ephemeral", "--profile", "x", URL)
    assert info.value.code == 2


def test_no_proxy_needs_ephemeral(fake: Recorder, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as info:
        _run(tmp_path, "--no-proxy", URL)
    assert info.value.code == 2
    assert _run(tmp_path, "--no-proxy", "--ephemeral", URL) == 0


def test_a_pacer_is_given_to_the_browser_layer(fake: Recorder, tmp_path: Path) -> None:
    _run(tmp_path, URL)
    assert fake.calls[0]["pacer"] is not None


def test_no_secret_in_stdout_or_stderr(fake: Recorder, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
    _run(tmp_path, URL)
    captured = capsys.readouterr()
    everything = captured.out + captured.err + caplog.text
    assert "s3cretpw" not in everything and "demo_user" not in everything


def test_profile_list_show_and_delete(isolated_profile_store: Path, fake: Recorder, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _run(tmp_path, "--profile", "shop-1", URL)
    capsys.readouterr()
    assert profile_cli.main(["list"]) == 0
    assert "shop-1" in capsys.readouterr().out
    assert profile_cli.main(["show", "shop-1"]) == 0
    shown = capsys.readouterr().out
    assert "shop-1" in shown and "visits" in shown
    session = ProfileStore(isolated_profile_store).load("shop-1").data.proxy_session
    assert session not in shown
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    assert profile_cli.main(["delete", "shop-1"]) == 1
    assert "shop-1" in ProfileStore(isolated_profile_store).names()
    monkeypatch.setattr("builtins.input", lambda prompt="": "shop-1")
    assert profile_cli.main(["delete", "shop-1"]) == 0
    capsys.readouterr()
    assert profile_cli.main(["list"]) == 0
    assert capsys.readouterr().out == ""


def test_delete_with_yes_asks_nothing(fake: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(tmp_path, "--profile", "shop-2", URL)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not ask"))
    assert profile_cli.main(["delete", "shop-2", "--yes"]) == 0


def test_show_of_a_missing_profile_exits_11(capsys: pytest.CaptureFixture[str]) -> None:
    assert profile_cli.main(["show", "nope"]) == 11


# ---- the real open_pages with a fake browser: visits are counted when the browser started

from contextlib import contextmanager  # noqa: E402
from collections.abc import Iterator  # noqa: E402

from hib.errors import BrowserStartError  # noqa: E402
from hib.pace import Pacer  # noqa: E402


class _Tab:
    """A stand-in for a browser tab."""


@pytest.fixture()
def fake_browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the browser start and the page opening; return the list of opened addresses."""
    opened: list[str] = []

    @contextmanager
    def fake_launch(proxy, options, profile=None) -> Iterator[_Tab]:  # type: ignore[no-untyped-def]
        yield _Tab()

    def fake_open_one(page, url, options, secrets):  # type: ignore[no-untyped-def]
        opened.append(url)
        return PageResult(url, 200, "t", 10, "", False, "")

    monkeypatch.setattr(browser, "launch", fake_launch)
    monkeypatch.setattr(browser, "open_one", fake_open_one)
    monkeypatch.setattr(browser, "_observed_timezone", lambda page: "Europe/Moscow")
    monkeypatch.setattr(browser, "_sleep", lambda low, high: None)
    monkeypatch.setattr("hib.pace.time.sleep", lambda seconds: None)  # the pace between runs must not slow the tests
    monkeypatch.setattr(browser, "make_fingerprint", lambda: {"navigator": {}})
    monkeypatch.setenv("HIB_PROXY_URL", PROXY_URL)
    return opened


def test_visits_are_counted_once_the_browser_started(fake_browser: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(tmp_path, URL)
    first = _lines(capsys)[0]
    _run(tmp_path, URL)
    second = _lines(capsys)[0]
    assert (first["profile"], first["visits"], second["visits"]) == ("default", 1, 2)


def test_a_browser_that_did_not_start_is_not_a_visit(
    isolated_profile_store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    @contextmanager
    def broken_launch(proxy, options, profile=None):  # type: ignore[no-untyped-def]
        raise BrowserStartError("no libraries")
        yield  # pragma: no cover

    monkeypatch.setattr(browser, "launch", broken_launch)
    monkeypatch.setattr(browser, "make_fingerprint", lambda: {"navigator": {}})
    monkeypatch.setenv("HIB_PROXY_URL", PROXY_URL)
    assert _run(tmp_path, URL) == 8
    assert ProfileStore(isolated_profile_store).load("default").data.visits == 0


def test_the_browser_time_zone_is_saved_in_the_profile(isolated_profile_store: Path, fake_browser: list[str], tmp_path: Path) -> None:
    _run(tmp_path, URL)
    assert ProfileStore(isolated_profile_store).load("default").data.timezone == "Europe/Moscow"


def test_pace_between_runs_waits_the_rest_of_the_gap_before_the_browser_starts(
    isolated_profile_store: Path, fake_browser: list[str], tmp_path: Path
) -> None:
    slept: list[float] = []
    store = ProfileStore(isolated_profile_store)
    with store.acquire("default", lambda: {"navigator": {}}) as profile:
        profile.data.last_navigation = {"shop.example.test": 990.0}
        pacer = Pacer(profile.data.last_navigation, 20.0, clock=lambda: 1000.0, sleep=slept.append)
        browser.open_pages(
            [URL, "https://shop.example.test/second"], None, browser.OpenOptions(out_dir=tmp_path), lambda r: None, profile, pacer
        )
    assert slept == [pytest.approx(10.0), pytest.approx(20.0)]  # first page: rest of the old gap; second: a full gap (same clock)
    assert fake_browser == [URL, "https://shop.example.test/second"]


def test_provider_api_proxy_gets_the_profiles_own_session_and_exit_is_remembered(
    isolated_profile_store: Path, fake_browser: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hib.proxy import ProxyConfig
    from hib.session import SessionProxy

    seen: list[tuple[str, str]] = []

    def fake_resolve(token, cache_path, session_id, **kwargs):  # type: ignore[no-untyped-def]
        seen.append((token, session_id))
        return SessionProxy(ProxyConfig("gw.example.test", 9000, "u", "p"), country="RU", city="Test City")

    monkeypatch.delenv("HIB_PROXY_URL")
    monkeypatch.setenv("MOBILEPROXY_API_TOKEN", "tok-1")
    monkeypatch.setattr(open_page, "resolve_session_proxy", fake_resolve)
    assert _run(tmp_path, URL) == 0
    profile = ProfileStore(isolated_profile_store).load("default")
    assert seen == [("tok-1", profile.data.proxy_session)]
    assert profile.data.last_exit == {"country": "RU", "city": "Test City"}


def test_missing_proxy_settings_exit_4_without_creating_a_profile(
    isolated_profile_store: Path, fake_browser: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HIB_PROXY_URL")
    monkeypatch.delenv("MOBILEPROXY_API_TOKEN", raising=False)
    assert _run(tmp_path, URL) == 4
    assert ProfileStore(isolated_profile_store).names() == []
