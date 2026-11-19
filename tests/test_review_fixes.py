"""Regression tests for the second review's blockers H3, H5, H6, H7, H8 (H1, H2, H4 are in test_profiles.py).

No network, no browser; hosts, logins and tokens are synthetic.
"""

import json
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from hib import browser, proxy
from hib.errors import ProxyApiError
from hib.pace import Pacer
from hib.profiles import ProfileStore
from hib.proxy import ProxyConfig, proxy_from_url
from hib.session import resolve_session_proxy

# ---------------------------------------------------------------- H6: IPv6 literals keep their brackets


@pytest.mark.parametrize("url", ["http://[::1]:8888", "socks5://[::1]:8888"])
def test_h6_ipv6_proxy_literal_round_trips_through_both_serializers(url: str) -> None:
    from urllib.parse import urlsplit

    config = proxy_from_url(url)
    assert config.as_playwright()["server"] == url
    assert config.as_url() == url
    assert urlsplit(config.as_url()).port == 8888


def test_h6_ipv6_proxy_with_a_login_keeps_brackets_in_the_url() -> None:
    config = ProxyConfig(host="2001:db8::1", port=8001, username="demo_user", password="s3cret")
    assert config.as_url() == "http://demo_user:s3cret@[2001:db8::1]:8001"
    assert config.as_playwright()["server"] == "http://[2001:db8::1]:8001"


# ---------------------------------------------------------------- H7: the gap is measured at the navigation


class _Clock:
    """A fake clock: sleeping and browser start-up move it."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_h7_gap_between_navigations_holds_when_the_browser_start_is_slow(
    isolated_profile_store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = _Clock()
    startup = iter([15.0, 1.0])
    starts: list[float] = []

    @contextmanager
    def slow_launch(proxy, options, profile=None) -> Iterator[object]:  # type: ignore[no-untyped-def]
        clock.now += next(startup)
        yield object()

    def fake_open_one(page, url, options, secrets):  # type: ignore[no-untyped-def]
        starts.append(clock.now)
        return browser.PageResult(url, 200, "t", 10, "", False, "")

    monkeypatch.setattr(browser, "launch", slow_launch)
    monkeypatch.setattr(browser, "open_one", fake_open_one)
    monkeypatch.setattr(browser, "_observed_timezone", lambda page: "")
    store = ProfileStore(isolated_profile_store)
    for _ in range(2):
        with store.acquire("default", lambda: {"navigator": {}}) as profile:
            pacer = Pacer(profile.data.last_navigation, 20.0, clock=clock, sleep=clock.sleep, on_change=profile.save)
            browser.open_pages(["https://shop.example.test/"], None, browser.OpenOptions(out_dir=tmp_path), lambda r: None, profile, pacer)
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 20.0


# ---------------------------------------------------------------- H5: the proxy answer cache is bound to its settings

from test_session import PASSWORD, SESSION, FakeApi, _answer, _login  # noqa: E402

from hib.errors import ProxyAuthError  # noqa: E402
from hib.session import CACHE_TTL_S, STALE_MAX_S  # noqa: E402

CACHE_NAME = "proxy-cache.json"


def _call(api: FakeApi, tmp_path: Path, now: float, token: str = "token-1", proxy_id: str | None = "77", **kwargs: object):  # type: ignore[no-untyped-def]
    return resolve_session_proxy(
        token, tmp_path / CACHE_NAME, SESSION, proxy_id=proxy_id, now=lambda: now, sleep=lambda s: None, call=api, **kwargs  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "changed",
    [{"token": "token-2"}, {"proxy_id": "78"}, {"state": "aa"}, {"city": "bb"}],
    ids=["account", "proxy_id", "state", "city"],
)
def test_h5_a_changed_setting_is_not_served_from_the_warm_cache(tmp_path: Path, changed: dict[str, str]) -> None:
    api = FakeApi(_answer(), _answer(state="aa", city="bb"))
    _call(api, tmp_path, 1000.0)
    _call(api, tmp_path, 1001.0, **changed)
    assert len(api.calls) >= 2


def test_h5_cache_holds_no_raw_token(tmp_path: Path) -> None:
    _call(FakeApi(_answer()), tmp_path, 1000.0)
    assert "token-1" not in (tmp_path / CACHE_NAME).read_text(encoding="utf-8")


def test_h5_a_stale_answer_older_than_the_limit_is_not_used_as_a_fallback(tmp_path: Path) -> None:
    api = FakeApi(_answer(), ProxyApiError("proxy API answered HTTP 429"))
    _call(api, tmp_path, 1000.0)
    with pytest.raises(ProxyApiError):
        _call(api, tmp_path, 1000.0 + STALE_MAX_S + 1)


def test_h5_a_fresh_enough_answer_is_still_a_fallback_after_a_rate_limit(tmp_path: Path) -> None:
    api = FakeApi(_answer(), ProxyApiError("proxy API answered HTTP 429"))
    first = _call(api, tmp_path, 1000.0)
    again = _call(api, tmp_path, 1000.0 + CACHE_TTL_S + 1)
    assert again.proxy.username == first.proxy.username


def test_h5_an_authorization_error_never_falls_back_and_drops_the_cache(tmp_path: Path) -> None:
    api = FakeApi(_answer(), ProxyAuthError("proxy API answered HTTP 401 (check MOBILEPROXY_API_TOKEN)"))
    _call(api, tmp_path, 1000.0)
    with pytest.raises(ProxyAuthError):
        _call(api, tmp_path, 1000.0 + CACHE_TTL_S + 1)
    assert not (tmp_path / CACHE_NAME).exists()


def test_h5_a_failed_call_with_changed_settings_does_not_use_the_old_answer(tmp_path: Path) -> None:
    api = FakeApi(_answer(), ProxyApiError("proxy API answered HTTP 429"))
    _call(api, tmp_path, 1000.0)
    with pytest.raises(ProxyApiError):
        _call(api, tmp_path, 1000.0 + CACHE_TTL_S + 1, token="token-2")


def test_h5_old_cache_files_without_a_binding_are_ignored(tmp_path: Path) -> None:
    http = f"http://{_login()}:{PASSWORD}@gw.example.test:9000"
    (tmp_path / CACHE_NAME).write_text(json.dumps({"fetched_at": 1000.0, "proxy_id": "77", "http": http}), encoding="utf-8")
    api = FakeApi(_answer())
    _call(api, tmp_path, 1001.0)
    assert len(api.calls) == 1


def test_h5_http_401_and_403_from_the_api_are_authorization_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    for code in (401, 403):

        def fake_open(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise urllib.error.HTTPError("https://api.example.test/", code, "denied", None, None)  # type: ignore[arg-type]

        monkeypatch.setattr(proxy.urllib.request.OpenerDirector, "open", fake_open)
        with pytest.raises(ProxyAuthError) as info:
            proxy.call_mobileproxy("get_my_proxy", "tok-synthetic")
        assert isinstance(info.value, ProxyApiError)
        assert info.value.exit_code == 5


# ---------------------------------------------------------------- H3: provider text is checked before it is stored


def test_h3_a_secret_echoed_in_country_or_city_is_not_kept(tmp_path: Path) -> None:
    answer = _answer()
    answer["country"] = "p4ssphrase-demo"  # looks like a place, is the proxy password
    answer["city"] = "token-1"  # the API token
    answer["http"] = f"http://{_login()}:p4ssphrase-demo@gw.example.test:9000"
    result = _call(FakeApi(answer), tmp_path, 1000.0)
    assert (result.country, result.city) == ("", "")
    assert "p4ssphrase-demo" not in (tmp_path / CACHE_NAME).read_text(encoding="utf-8").replace(answer["http"], "")  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["a1b2c3d4e5f6", "line\nbreak", "x" * 200, "<script>", "ключ=значение", "https://x.test"])
def test_h3_text_that_is_not_a_place_name_is_dropped(tmp_path: Path, value: str) -> None:
    answer = _answer()
    answer["city"] = value
    assert _call(FakeApi(answer), tmp_path, 1000.0).city == ""


def test_h3_normal_place_names_are_kept(tmp_path: Path) -> None:
    answer = _answer()
    answer["country"], answer["city"] = "RU", "Санкт-Петербург"
    result = _call(FakeApi(answer), tmp_path, 1000.0)
    assert (result.country, result.city) == ("RU", "Санкт-Петербург")


def test_h3_a_profile_file_with_junk_in_last_exit_is_cleaned_in_show(isolated_profile_store: Path) -> None:
    store = ProfileStore(isolated_profile_store)
    with store.acquire("default", lambda: {"navigator": {}}) as profile:
        profile.data.last_exit = {"country": "RU", "city": "tok-abc123-secret"}
        profile.save()
    assert store.load("default").describe()["last_exit"] == {"country": "RU", "city": ""}
