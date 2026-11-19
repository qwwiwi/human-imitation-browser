"""Per-profile sticky proxy session: user-name substitution, settings calls, cache, no secrets in logs.

All hosts, logins and passwords are synthetic.
"""

import logging
import os
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest

from hib.errors import ProxyApiError
from hib.proxy import build_api_url
from hib.session import CACHE_TTL_S, SessionProxy, new_session_id, resolve_session_proxy, with_session

PASSWORD = "p@ss-w0rd"
BASE_LOGIN = "u123456"
SESSION = "mysession01"


def _login(session: str = "oldsession", minutes: str = "10", tail: str = "") -> str:
    return f"{BASE_LOGIN}-zone-custom-region-ru-session-{session}-sessTime-{minutes}{tail}"


def _answer(mode: str = "sticky", minutes: int = 120, login: str | None = None, state: str = "", city: str = "") -> dict[str, object]:
    user = login or _login(minutes=str(minutes))
    return {
        "status": "ok",
        "session": "oldsession",
        "session_mode": mode,
        "session_time": minutes,
        "country": "RU",
        "state": state,
        "city": city,
        "asn": "",
        "http": f"http://{user}:{PASSWORD}@gw.example.test:9000",
        "socks5": f"socks5://{user}:{PASSWORD}@gw.example.test:9001",
    }


class FakeApi:
    """Records every call and answers from a script."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, command: str, token: str, params: Mapping[str, str] | None = None) -> object:
        self.calls.append((command, dict(params or {})))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


MY_PROXY = [{"proxy_id": 77, "proxy_hostname": "h.example.test", "proxy_http_port": "1", "proxy_login": "x", "proxy_pass": "y"}]


def _resolve(api: FakeApi, tmp_path: Path, now: float = 1000.0, **kwargs: object) -> SessionProxy:
    sleeps: list[float] = []
    return resolve_session_proxy(
        "token-1",
        tmp_path / "proxy-cache.json",
        SESSION,
        proxy_id="77",
        now=lambda: now,
        sleep=sleeps.append,
        call=api,
        **kwargs,  # type: ignore[arg-type]
    )


def test_session_id_is_replaced_and_the_rest_of_the_login_is_kept() -> None:
    assert with_session(_login(), SESSION) == f"{BASE_LOGIN}-zone-custom-region-ru-session-{SESSION}-sessTime-10"


def test_unknown_tail_after_sess_time_is_kept_untouched() -> None:
    result = with_session(_login(tail="-x9-tail"), SESSION)
    assert result.endswith("-sessTime-10-x9-tail")
    assert f"-session-{SESSION}-sessTime-" in result


def test_login_without_a_session_part_is_an_error_that_does_not_show_the_login() -> None:
    with pytest.raises(ProxyApiError) as info:
        with_session("plainlogin123", SESSION)
    assert "plainlogin123" not in str(info.value)


def test_new_session_ids_are_url_safe_and_different() -> None:
    ids = {new_session_id() for _ in range(20)}
    assert len(ids) == 20
    assert all(i.isalnum() and len(i) >= 8 for i in ids)


def test_api_url_carries_only_the_asked_parameters() -> None:
    url = build_api_url("residential_settings", {"proxy_id": "77", "session_mode": "sticky"})
    assert url.endswith("?command=residential_settings&proxy_id=77&session_mode=sticky")


def test_sticky_120_is_read_with_one_call_and_nothing_is_changed(tmp_path: Path) -> None:
    api = FakeApi(_answer())
    result = _resolve(api, tmp_path)
    assert api.calls == [("residential_settings", {"proxy_id": "77"})]
    assert result.proxy.username == with_session(_login(minutes="120"), SESSION)
    assert (result.proxy.host, result.proxy.port) == ("gw.example.test", 9000)
    assert result.proxy.password == PASSWORD
    assert (result.country, result.city) == ("RU", "")


def test_rotating_mode_is_switched_to_sticky_120_once_and_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    api = FakeApi(_answer(mode="rotating", minutes=10), _answer())
    with caplog.at_level(logging.INFO):
        result = _resolve(api, tmp_path)
    assert [c[0] for c in api.calls] == ["residential_settings", "residential_settings"]
    assert api.calls[1][1] == {"proxy_id": "77", "session_mode": "sticky", "session_time": "120"}
    assert "sticky" in caplog.text and "120" in caplog.text
    assert "-sessTime-120" in result.proxy.username


def test_short_session_time_is_raised_to_120(tmp_path: Path) -> None:
    api = FakeApi(_answer(minutes=30), _answer())
    _resolve(api, tmp_path)
    assert api.calls[1][1]["session_time"] == "120"


def test_state_and_city_are_sent_only_when_asked_and_different(tmp_path: Path) -> None:
    api = FakeApi(_answer(state="aa", city="bb"))
    _resolve(api, tmp_path, state="aa", city="bb")
    assert len(api.calls) == 1  # already as wanted: nothing saved

    api = FakeApi(_answer(), _answer())
    (tmp_path / "other").mkdir()
    _resolve(api, tmp_path / "other", state="aa", city="bb")
    assert api.calls[1][1] == {"proxy_id": "77", "state": "aa", "city": "bb"}


def test_answer_without_a_save_result_is_read_again_after_the_rate_limit_pause(tmp_path: Path) -> None:
    saved_no_url = {"status": "ok"}
    api = FakeApi(_answer(mode="rotating", minutes=10), saved_no_url, _answer())
    sleeps: list[float] = []
    resolve_session_proxy("t", tmp_path / "c.json", SESSION, proxy_id="77", now=lambda: 1.0, sleep=sleeps.append, call=api)
    assert [c[1] for c in api.calls][-1] == {"proxy_id": "77"}
    assert sleeps and sleeps[0] >= 5.0


def test_second_resolve_inside_the_ttl_makes_no_api_call(tmp_path: Path) -> None:
    api = FakeApi(_answer())
    first = _resolve(api, tmp_path, now=1000.0)
    second = _resolve(api, tmp_path, now=1000.0 + CACHE_TTL_S - 1)
    assert len(api.calls) == 1
    assert second.proxy.username == first.proxy.username


def test_cache_expires_and_the_api_is_asked_again(tmp_path: Path) -> None:
    api = FakeApi(_answer(), _answer())
    _resolve(api, tmp_path, now=1000.0)
    _resolve(api, tmp_path, now=1000.0 + CACHE_TTL_S + 1)
    assert len(api.calls) == 2


def test_a_cached_proxy_gets_the_current_session_id(tmp_path: Path) -> None:
    api = FakeApi(_answer())
    _resolve(api, tmp_path, now=1000.0)
    other = resolve_session_proxy("token-1", tmp_path / "proxy-cache.json", "othersession", proxy_id="77", now=lambda: 1001.0, call=api)
    assert "-session-othersession-" in other.proxy.username


def test_cache_file_is_private(tmp_path: Path) -> None:
    _resolve(FakeApi(_answer()), tmp_path)
    assert stat.S_IMODE(os.stat(tmp_path / "proxy-cache.json").st_mode) == 0o600


def test_proxy_id_is_taken_from_the_account_once_and_remembered(tmp_path: Path) -> None:
    api = FakeApi(MY_PROXY, _answer(), _answer())
    cache = tmp_path / "c.json"
    kw = {"call": api, "sleep": lambda s: None}
    resolve_session_proxy("t", cache, SESSION, now=lambda: 1000.0, **kw)  # type: ignore[arg-type]
    resolve_session_proxy("t", cache, SESSION, now=lambda: 1000.0 + CACHE_TTL_S + 5, **kw)  # type: ignore[arg-type]
    assert [c[0] for c in api.calls] == ["get_my_proxy", "residential_settings", "residential_settings"]
    assert api.calls[1][1] == {"proxy_id": "77"}


def test_rate_limit_answer_falls_back_to_the_last_cached_proxy(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    api = FakeApi(_answer(), ProxyApiError("proxy API answered HTTP 429"))
    first = _resolve(api, tmp_path, now=1000.0)
    with caplog.at_level(logging.WARNING):
        again = _resolve(api, tmp_path, now=1000.0 + CACHE_TTL_S + 1)
    assert again.proxy.username == first.proxy.username
    assert "429" in caplog.text


def test_api_failure_without_a_cache_is_raised(tmp_path: Path) -> None:
    with pytest.raises(ProxyApiError):
        _resolve(FakeApi(ProxyApiError("proxy API answered HTTP 429")), tmp_path)


def test_no_credential_reaches_logs_repr_or_errors(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    api = FakeApi(_answer(mode="rotating", minutes=10), _answer())
    with caplog.at_level(logging.DEBUG):
        result = _resolve(api, tmp_path)
    everything = caplog.text + repr(result) + repr(result.proxy)
    for secret in (PASSWORD, BASE_LOGIN, result.proxy.username, SESSION, "gw.example.test:9000"):
        assert secret not in everything
