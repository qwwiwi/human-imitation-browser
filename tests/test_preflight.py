"""The pre-flight check: address change, profile memory, flags, the gate in open_page.py.

No browser and no network: the browser part is replaced by a fake that returns what the check page showed.
All values are synthetic.
"""

import json
import logging
from pathlib import Path

import pytest

import open_page
import preflight
from conftest import HOME_NETWORK, fresh_preflight
from hib import browser
from hib import preflight as core
from hib.errors import ConfigError, ProxyUnreachableError
from hib.env import load_settings, parse_preflight_max_age
from hib.preflight import Before, Seen, assess, sanitize_owner
from hib.profiles import ProfileData, ProfileStore

PROXY_URL = "http://demo_user:s3cretpw@proxy.example.test:8001"
URL = "https://shop.example.test/"
OLD = {"country": "RU", "city": "Test City", "owner": "Test Home Net"}


def seen(**changes: object) -> Seen:
    values: dict[str, object] = {
        "country": "RU",
        "city": "Test City",
        "owner": "Test Home Net",
        "hosting": False,
        "proxy_flag": False,
        "mobile": None,
        "cookies_at_start": 4,
        "cookies_at_end": 5,
        "cookie_store_present": True,
        "data_dir_empty": False,
        "screenshot": "out/check.png",
    }
    values.update(changes)
    return Seen(**values)  # type: ignore[arg-type]


def before(**changes: object) -> Before:
    values: dict[str, object] = {
        "visits": 3,
        "last_preflight": "2026-11-19T08:00:00+00:00",
        "network": dict(OLD),
        "cookie_count": 4,
        "visits_at_preflight": 3,
    }
    values.update(changes)
    return Before(**values)  # type: ignore[arg-type]


# ------------------------------------------------------------ assess: the address


def test_first_run_records_and_is_ok_and_new() -> None:
    verdict = assess(Before(visits=0, last_preflight="", network={}, cookie_count=None), seen(cookies_at_start=0), strict=False)
    assert verdict.ok is True
    assert verdict.address_changed is False
    assert verdict.profile_memory == "new"
    assert verdict.problems == []


def test_same_network_is_not_a_change() -> None:
    verdict = assess(before(), seen(), strict=False)
    assert verdict.ok is True and verdict.address_changed is False and verdict.profile_memory == "ok"


@pytest.mark.parametrize(
    ("field", "value"),
    [("country", "DE"), ("city", "Other City"), ("owner", "Other Net")],
)
def test_a_changed_country_city_or_owner_is_reported_with_old_and_new_and_fails(field: str, value: str) -> None:
    verdict = assess(before(), seen(**{field: value}), strict=False)
    assert verdict.ok is False
    assert verdict.address_changed is True
    assert verdict.change is not None
    assert verdict.change["old"][field] == OLD[field]
    assert verdict.change["new"][field] == value
    assert any("address" in problem for problem in verdict.problems)


def test_a_value_the_lookup_did_not_give_is_unknown_not_a_change() -> None:
    verdict = assess(before(), seen(owner="", city=""), strict=False)
    assert verdict.address_changed is False and verdict.ok is True


def test_an_accepted_change_passes_but_is_still_reported() -> None:
    verdict = assess(before(), seen(city="Other City"), strict=False, accept_address_change=True)
    assert verdict.ok is True and verdict.address_changed is True
    assert any("accepted" in note for note in verdict.problems_soft)


# ------------------------------------------------------------ assess: the profile memory


@pytest.mark.parametrize(
    ("b", "s"),
    [
        (before(), seen(data_dir_empty=True)),  # the browser data folder is empty
        (before(), seen(cookie_store_present=False)),  # no cookie store file after the run
        (before(cookie_count=7), seen(cookies_at_start=0)),  # the stored cookies are gone
        (before(visits=0), seen()),  # the visit counter fell back to zero
    ],
    ids=["empty-data-folder", "no-cookie-file", "cookies-gone", "visits-reset"],
)
def test_empty_memory_of_a_used_profile_fails(b: Before, s: Seen) -> None:
    verdict = assess(b, s, strict=False)
    assert verdict.profile_memory == "empty"
    assert verdict.ok is False
    assert any("memory" in problem for problem in verdict.problems)


def test_a_used_profile_whose_sites_set_no_cookies_is_not_empty() -> None:
    verdict = assess(before(cookie_count=0), seen(cookies_at_start=0), strict=False)
    assert verdict.profile_memory == "ok" and verdict.ok is True


def test_a_profile_that_had_only_pre_flights_and_no_visits_is_not_empty() -> None:
    only_checks = before(visits=0, visits_at_preflight=0, cookie_count=0)
    verdict = assess(only_checks, seen(cookies_at_start=0), strict=False)
    assert verdict.profile_memory == "ok" and verdict.ok is True


def test_a_new_profile_with_nothing_stored_yet_is_not_a_failure() -> None:
    verdict = assess(Before(visits=0, last_preflight="", network={}, cookie_count=None), seen(data_dir_empty=True, cookies_at_start=0), strict=False)
    assert verdict.profile_memory == "new" and verdict.ok is True


# ------------------------------------------------------------ assess: the flags


@pytest.mark.parametrize("flags", [{"hosting": True}, {"proxy_flag": True}])
def test_flags_are_a_warning_without_strict(flags: dict[str, object]) -> None:
    verdict = assess(before(), seen(**flags), strict=False)
    assert verdict.ok is True
    assert verdict.problems == []
    assert any("hosting" in note or "proxy" in note for note in verdict.problems_soft)


@pytest.mark.parametrize("flags", [{"hosting": True}, {"proxy_flag": True}])
def test_flags_fail_with_strict(flags: dict[str, object]) -> None:
    verdict = assess(before(), seen(**flags), strict=True)
    assert verdict.ok is False
    assert any("--strict" in problem for problem in verdict.problems)


def test_unknown_flags_are_a_soft_note_even_with_strict() -> None:
    verdict = assess(before(), seen(hosting=None, proxy_flag=None), strict=True)
    assert verdict.ok is True
    assert any("unknown" in note for note in verdict.problems_soft)


# ------------------------------------------------------------ sanitizing what the check page said


def test_an_owner_text_with_an_address_or_a_secret_is_dropped() -> None:
    assert sanitize_owner("AS64500 Test Home Net", []) == "AS64500 Test Home Net"
    assert sanitize_owner("Net 203.0.113.9", []) == ""
    assert sanitize_owner("Net s3cretpw", ["s3cretpw"]) == ""
    assert sanitize_owner("x" * 200, []) == ""
    assert sanitize_owner("Line\nbreak", []) == ""


# ------------------------------------------------------------ the profile record


def test_profile_record_survives_a_round_trip_and_old_files_still_load(isolated_profile_store: Path) -> None:
    store = ProfileStore(isolated_profile_store)
    with store.acquire("default", lambda: {"navigator": {}}) as profile:
        assert profile.preflight_is_fresh(12.0) is False
        profile.record_preflight(dict(OLD), 5)
    loaded = store.load("default").data
    assert loaded.network == OLD and loaded.cookie_count == 5 and loaded.last_preflight
    assert store.load("default").preflight_is_fresh(12.0) is True
    old_text = json.dumps({"name": "x", "created": "2026-01-01T00:00:00+00:00", "proxy_session": "abc", "visits": 2})
    legacy = ProfileData.from_json(old_text)
    assert legacy.last_preflight == "" and legacy.network == {} and legacy.cookie_count is None


def test_a_stale_or_unreadable_pre_flight_time_is_not_fresh(isolated_profile_store: Path) -> None:
    fresh_preflight(isolated_profile_store, "a", age_h=13.0)
    fresh_preflight(isolated_profile_store, "b", age_h=11.0)
    store = ProfileStore(isolated_profile_store)
    assert store.load("a").preflight_is_fresh(12.0) is False
    assert store.load("b").preflight_is_fresh(12.0) is True
    broken = store.load("b")
    broken.data.last_preflight = "not a time"
    assert broken.preflight_is_fresh(12.0) is False


def test_the_max_age_setting_has_a_default_and_refuses_nonsense() -> None:
    assert parse_preflight_max_age(None) == 12.0
    assert parse_preflight_max_age("3") == 3.0
    for bad in ("abc", "-1", "nan", "inf"):
        with pytest.raises(ConfigError):
            parse_preflight_max_age(bad)


def test_settings_carry_the_check_url_and_the_max_age(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.env", {"HIB_CHECK_URL": "https://check.example.test/json", "HIB_PREFLIGHT_MAX_AGE_H": "6"})
    assert settings.check_url == "https://check.example.test/json" and settings.preflight_max_age_h == 6.0
    assert load_settings(tmp_path / "missing.env", {}).check_url is None


# ------------------------------------------------------------ the command


class FakeObserver:
    """Stands in for hib.preflight.observe_in_browser."""

    def __init__(self, observed: Seen | Exception) -> None:
        self.observed = observed
        self.calls: list[dict[str, object]] = []

    def __call__(self, proxy, profile, options, check_url, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"proxy": proxy, "profile": profile, "check_url": check_url, **kwargs})
        if isinstance(self.observed, Exception):
            raise self.observed
        return self.observed


@pytest.fixture()
def observer(monkeypatch: pytest.MonkeyPatch) -> FakeObserver:
    fake = FakeObserver(seen(cookies_at_start=0, cookies_at_end=2))
    monkeypatch.setattr(core, "observe_in_browser", fake)
    monkeypatch.setattr(browser, "make_fingerprint", lambda: {"navigator": {"userAgent": "synthetic"}})
    monkeypatch.setenv("HIB_PROXY_URL", PROXY_URL)
    monkeypatch.delenv("HIB_CHECK_URL", raising=False)
    monkeypatch.delenv("HIB_REPUTATION_URL", raising=False)
    monkeypatch.delenv("HIB_PREFLIGHT_MAX_AGE_H", raising=False)
    return fake


def _run(tmp_path: Path, *args: str) -> int:
    return preflight.main(["--env-file", str(tmp_path / "missing.env"), "--out", str(tmp_path / "out"), *args])


def _out(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out.splitlines()[0])


def test_the_first_run_exits_0_prints_one_json_line_and_records_the_network(
    observer: FakeObserver, isolated_profile_store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, "--profile", "shop-1") == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    out = json.loads(lines[0])
    assert out["ok"] is True and out["profile"] == "shop-1" and out["proxy_works"] is True
    assert (out["country"], out["city"], out["network_owner"]) == ("RU", "Test City", "Test Home Net")
    assert out["hosting"] is False and out["proxy_flag"] is False
    assert out["address_changed"] is False and out["profile_memory"] == "new"
    assert out["screenshot"] == "out/check.png"
    assert out["problems"] == [] and out["problems_soft"] == []
    profile = ProfileStore(isolated_profile_store).load("shop-1")
    assert profile.data.network == OLD
    assert profile.data.cookie_count == 2
    assert profile.preflight_is_fresh(12.0)
    assert observer.calls[0]["check_url"] == "https://ipinfo.io/json"


def test_the_default_profile_and_the_check_url_override(
    observer: FakeObserver, monkeypatch: pytest.MonkeyPatch, isolated_profile_store: Path, tmp_path: Path
) -> None:
    monkeypatch.setenv("HIB_CHECK_URL", "https://check.example.test/json")
    assert _run(tmp_path) == 0
    assert observer.calls[0]["check_url"] == "https://check.example.test/json"
    assert ProfileStore(isolated_profile_store).names() == ["default"]


def test_the_flags_page_defaults_to_the_https_one_and_can_be_overridden_by_env(
    observer: FakeObserver, monkeypatch: pytest.MonkeyPatch, isolated_profile_store: Path, tmp_path: Path
) -> None:
    assert _run(tmp_path) == 0
    assert observer.calls[0]["reputation_url"] == "https://api.ipapi.is/"
    monkeypatch.setenv("HIB_REPUTATION_URL", "https://flags.example.test/json")
    assert _run(tmp_path, "--profile", "other") == 0
    assert observer.calls[1]["reputation_url"] == "https://flags.example.test/json"


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "ftp://flags.example.test/", "flags.example.test", "https://"])
def test_a_reputation_url_that_is_not_http_is_a_config_error(
    observer: FakeObserver, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    monkeypatch.setenv("HIB_REPUTATION_URL", bad)
    assert _run(tmp_path) == 4
    assert not observer.calls
    assert _out(capsys)["exit_code"] == 4


def test_the_settings_carry_the_reputation_url(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.env", {"HIB_REPUTATION_URL": "https://flags.example.test/json"})
    assert settings.reputation_url == "https://flags.example.test/json"
    assert load_settings(tmp_path / "missing.env", {}).reputation_url is None


def test_the_report_carries_the_mobile_flag_true_false_or_null(
    observer: FakeObserver, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for index, mobile in enumerate((True, False, None)):
        observer.observed = seen(mobile=mobile, cookies_at_start=0)
        assert _run(tmp_path, "--profile", f"mob-{index}") == 0
        out = _out(capsys)
        assert "mobile" in out and out["mobile"] is mobile


def test_unknown_flags_do_not_fail_even_with_strict_but_a_positive_flag_does(
    observer: FakeObserver, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observer.observed = seen(hosting=None, proxy_flag=None, mobile=None, cookies_at_start=0)
    assert _run(tmp_path, "--strict") == 0
    out = _out(capsys)
    assert out["ok"] is True and out["hosting"] is None
    assert any("reputation unknown" in note for note in out["problems_soft"])  # type: ignore[attr-defined]
    observer.observed = seen(hosting=True, proxy_flag=None, cookies_at_start=0)
    assert _run(tmp_path, "--profile", "other", "--strict") == 12


def test_a_mobile_address_is_never_a_strict_failure() -> None:
    assert assess(before(), seen(mobile=True), strict=True).ok is True


def test_a_check_url_that_is_not_http_is_a_config_error(
    observer: FakeObserver, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HIB_CHECK_URL", "file:///etc/passwd")
    assert _run(tmp_path) == 4
    assert not observer.calls
    assert _out(capsys)["exit_code"] == 4


def test_a_changed_address_exits_12_reports_old_and_new_and_keeps_the_old_record(
    observer: FakeObserver, isolated_profile_store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fresh_preflight(isolated_profile_store, "default", age_h=20.0)
    observer.observed = seen(city="Other City", cookies_at_start=0)
    with ProfileStore(isolated_profile_store).acquire("default", lambda: {}) as profile:
        profile.data.visits = 2
        profile.data.cookie_count = 0
        profile.save()
    assert _run(tmp_path) == 12
    out = _out(capsys)
    assert out["ok"] is False and out["address_changed"] is True
    assert out["address_change"] == {"old": OLD, "new": {**OLD, "city": "Other City"}}
    after = ProfileStore(isolated_profile_store).load("default")
    assert after.data.network == OLD  # a changed address is not silently accepted by running the check again
    assert after.preflight_is_fresh(12.0) is False  # and the pre-flight does not count as passed


def test_the_owner_can_accept_a_changed_address_with_a_flag(
    observer: FakeObserver, isolated_profile_store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fresh_preflight(isolated_profile_store, "default", age_h=20.0)
    observer.observed = seen(city="Other City", cookies_at_start=0)
    with ProfileStore(isolated_profile_store).acquire("default", lambda: {}) as profile:
        profile.data.visits = 2
        profile.save()
    assert _run(tmp_path, "--accept-address-change") == 0
    assert ProfileStore(isolated_profile_store).load("default").data.network["city"] == "Other City"
    assert _out(capsys)["address_changed"] is True


def test_empty_memory_of_a_used_profile_exits_12(
    observer: FakeObserver, isolated_profile_store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fresh_preflight(isolated_profile_store, "default", age_h=1.0)
    with ProfileStore(isolated_profile_store).acquire("default", lambda: {}) as profile:
        profile.data.visits = 3
        profile.data.cookie_count = 9
        profile.save()
    observer.observed = seen(cookies_at_start=0)
    assert _run(tmp_path) == 12
    out = _out(capsys)
    assert out["profile_memory"] == "empty" and out["ok"] is False
    assert isinstance(out["cookies_stored"], int)  # a count only: never names or values


def test_flags_warn_by_default_and_fail_with_strict(
    observer: FakeObserver, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observer.observed = seen(hosting=True, proxy_flag=True, cookies_at_start=0)
    assert _run(tmp_path) == 0
    out = _out(capsys)
    assert out["ok"] is True and out["problems"] == [] and out["problems_soft"]
    assert out["hosting"] is True and out["proxy_flag"] is True
    assert _run(tmp_path, "--profile", "other", "--strict") == 12
    assert _out(capsys)["ok"] is False


def test_a_proxy_failure_keeps_its_own_exit_code_and_a_secret_stays_hidden(
    observer: FakeObserver, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    observer.observed = ProxyUnreachableError("cannot get through the proxy: s3cretpw refused")
    with caplog.at_level(logging.INFO):
        assert _run(tmp_path) == 6
    captured = capsys.readouterr()
    assert json.loads(captured.out)["exit_code"] == 6
    assert "s3cretpw" not in captured.out + captured.err + caplog.text


def test_no_address_and_no_secret_in_the_output(
    observer: FakeObserver, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    observer.observed = seen(owner="Test Net 203.0.113.9", cookies_at_start=0)
    with caplog.at_level(logging.INFO):
        assert _run(tmp_path) == 0
    captured = capsys.readouterr()
    everything = captured.out + captured.err + caplog.text
    for forbidden in ("203.0.113.9", "s3cretpw", "demo_user", "proxy.example.test", "8001"):
        assert forbidden not in everything, forbidden


def test_the_pre_flight_is_not_a_site_visit(
    observer: FakeObserver, isolated_profile_store: Path, tmp_path: Path
) -> None:
    assert _run(tmp_path) == 0
    data = ProfileStore(isolated_profile_store).load("default").data
    assert data.visits == 0  # the counter counts visits to sites
    assert data.last_navigation == {}  # and the pace of real hosts is untouched


# ------------------------------------------------------------ the gate in open_page.py


class Recorder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, urls, proxy, options, emit, profile=None, pacer=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        emit(browser.PageResult(URL, 200, "t", 10, "", False, ""))
        return False


@pytest.fixture()
def opener(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(browser, "open_pages", rec)
    monkeypatch.setattr(browser, "make_fingerprint", lambda: {"navigator": {}})
    monkeypatch.setenv("HIB_PROXY_URL", PROXY_URL)
    monkeypatch.delenv("HIB_PREFLIGHT_MAX_AGE_H", raising=False)
    return rec


def _open(tmp_path: Path, *args: str) -> int:
    return open_page.main(["--env-file", str(tmp_path / "missing.env"), *args, URL])


def test_a_real_address_without_a_pre_flight_is_refused_with_exit_4_and_the_command_to_run(
    opener: Recorder, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _open(tmp_path, "--profile", "shop-1") == 4
    out = json.loads(capsys.readouterr().out.splitlines()[0])
    assert out["exit_code"] == 4
    assert "scripts/preflight.py --profile shop-1" in out["error"]
    assert opener.calls == 0


def test_an_old_pre_flight_is_refused_and_a_fresh_one_lets_the_page_open(
    opener: Recorder, isolated_profile_store: Path, tmp_path: Path
) -> None:
    fresh_preflight(isolated_profile_store, "shop-1", age_h=13.0)
    assert _open(tmp_path, "--profile", "shop-1") == 4
    fresh_preflight(isolated_profile_store, "shop-1", age_h=11.0)
    assert _open(tmp_path, "--profile", "shop-1") == 0
    assert opener.calls == 1


def test_the_max_age_can_be_changed(
    opener: Recorder, monkeypatch: pytest.MonkeyPatch, isolated_profile_store: Path, tmp_path: Path
) -> None:
    fresh_preflight(isolated_profile_store, "shop-1", age_h=2.0)
    monkeypatch.setenv("HIB_PREFLIGHT_MAX_AGE_H", "1")
    assert _open(tmp_path, "--profile", "shop-1") == 4
    monkeypatch.setenv("HIB_PREFLIGHT_MAX_AGE_H", "3")
    assert _open(tmp_path, "--profile", "shop-1") == 0


def test_the_default_profile_name_is_in_the_message(opener: Recorder, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _open(tmp_path) == 4
    assert "scripts/preflight.py --profile default" in capsys.readouterr().out


def test_an_ephemeral_test_page_needs_no_pre_flight(opener: Recorder, tmp_path: Path) -> None:
    assert _open(tmp_path, "--ephemeral") == 0
    assert opener.calls == 1


def test_a_refusal_happens_before_any_proxy_call(
    opener: Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("no proxy call before the pre-flight is checked")

    monkeypatch.delenv("HIB_PROXY_URL")
    monkeypatch.setenv("MOBILEPROXY_API_TOKEN", "tok-1")
    monkeypatch.setattr(open_page, "resolve_session_proxy", forbidden)
    assert _open(tmp_path, "--profile", "shop-1") == 4


# ------------------------------------------------------------ real visits keep the cookie count for the next pre-flight


def test_open_pages_remembers_how_many_cookies_the_profile_holds(
    isolated_profile_store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from contextlib import contextmanager

    class Context:
        def cookies(self) -> list[dict[str, str]]:
            return [{"name": "n", "value": "v"}] * 3

    class Tab:
        context = Context()

    @contextmanager
    def fake_launch(proxy, options, profile=None):  # type: ignore[no-untyped-def]
        yield Tab()

    monkeypatch.setattr(browser, "launch", fake_launch)
    monkeypatch.setattr(browser, "open_one", lambda page, url, options, secrets: browser.PageResult(url, 200, "t", 10, "", False, ""))
    monkeypatch.setattr(browser, "_observed_timezone", lambda page: "")
    with ProfileStore(isolated_profile_store).acquire("default", lambda: {"navigator": {}}) as profile:
        browser.open_pages([URL], None, browser.OpenOptions(out_dir=tmp_path), lambda r: None, profile)
    assert ProfileStore(isolated_profile_store).load("default").data.cookie_count == 3
