"""Review blockers H4 and H5: the pre-flight record is bound to the profile and the proxy, and a failed check revokes it.

No browser and no network: the check and the opener are fakes (see test_preflight.py); all values are synthetic.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import open_page
from conftest import fresh_preflight
from hib import preflight as core
from hib.profiles import ProfileData, ProfileStore
from hib.proxy import ProxyConfig
from hib.session import SessionProxy
from test_preflight import OLD, PROXY_URL, FakeObserver, Recorder, _open, _run, observer, opener, seen  # noqa: F401 - fixtures

OTHER_PROXY_URL = "http://demo_user:other-pass@proxy.example.test:8001"


def _load(root: Path, name: str) -> ProfileData:
    return ProfileStore(root).load(name).data


# ------------------------------------------------------------ the binding is a hash of the configuration


def _profile(root: Path, name: str, fingerprint: dict[str, object] | None = None):  # type: ignore[no-untyped-def]
    store = ProfileStore(root)
    with store.acquire(name, lambda: fingerprint or {"navigator": {}}):
        pass
    return store.load(name)


def test_the_binding_is_a_hash_that_changes_with_the_profile_and_every_part_of_the_proxy(isolated_profile_store: Path) -> None:
    profile = _profile(isolated_profile_store, "a")
    base = ProxyConfig("proxy.example.test", 8001, "demo_user", "s3cretpw")
    binding = core.proxy_binding(profile, base)
    assert len(binding) == 64 and set(binding) <= set("0123456789abcdef")
    for forbidden in ("demo_user", "s3cretpw", "proxy.example.test", "8001"):
        assert forbidden not in binding
    assert core.proxy_binding(profile, ProxyConfig("proxy.example.test", 8001, "demo_user", "s3cretpw")) == binding
    for other in (
        ProxyConfig("proxy2.example.test", 8001, "demo_user", "s3cretpw"),
        ProxyConfig("proxy.example.test", 8002, "demo_user", "s3cretpw"),
        ProxyConfig("proxy.example.test", 8001, "demo_user2", "s3cretpw"),
        ProxyConfig("proxy.example.test", 8001, "demo_user", "other-pass"),
        ProxyConfig("proxy.example.test", 8001),
        ProxyConfig("proxy.example.test", 8001, scheme="socks5"),
    ):
        assert core.proxy_binding(profile, other) != binding
    assert core.proxy_binding(_profile(isolated_profile_store, "b"), base) != binding  # another profile folder
    assert core.proxy_binding(_profile(isolated_profile_store, "c", {"navigator": {"userAgent": "x"}}), base) != core.proxy_binding(
        _profile(isolated_profile_store, "d", {"navigator": {"userAgent": "x"}}), base
    )


def test_the_record_round_trips_loads_from_old_files_and_ignores_a_bad_value(isolated_profile_store: Path) -> None:
    profile = _profile(isolated_profile_store, "a")
    profile.record_preflight(dict(OLD), 2, binding="ab" * 32)
    assert _load(isolated_profile_store, "a").preflight_binding == "ab" * 32
    assert "preflight_binding" not in json.dumps(profile.describe())
    base = {"name": "x", "created": "2026-01-01T00:00:00+00:00", "proxy_session": "abc"}
    assert ProfileData.from_json(json.dumps(base)).preflight_binding == ""
    assert ProfileData.from_json(json.dumps({**base, "preflight_binding": "not-a-hash"})).preflight_binding == ""
    assert ProfileData.from_json(json.dumps({**base, "preflight_binding": 5})).preflight_binding == ""


def test_admission_needs_the_same_binding_and_a_fresh_time(isolated_profile_store: Path) -> None:
    profile = _profile(isolated_profile_store, "a")
    now = datetime.now(timezone.utc)
    profile.record_preflight(dict(OLD), 2, binding="ab" * 32)
    assert profile.admits(12.0, "ab" * 32) is True
    assert profile.admits(12.0, "cd" * 32) is False
    assert profile.admits(12.0, "") is False
    assert profile.admits(12.0, "ab" * 32, now + timedelta(hours=13)) is False
    profile.revoke_preflight()
    assert profile.admits(12.0, "ab" * 32) is False
    assert profile.data.network == OLD and profile.data.cookie_count == 2 and profile.data.last_preflight  # the baseline stays


# ------------------------------------------------------------ H5: a failed check revokes the admission and keeps the baseline


def test_a_pass_then_a_strict_failure_revokes_the_admission_and_keeps_the_baseline(
    observer: FakeObserver, opener: Recorder, isolated_profile_store: Path, tmp_path: Path
) -> None:
    observer.observed = seen(cookies_at_start=0, cookies_at_end=2)
    assert _run(tmp_path, "--profile", "shop-1") == 0
    assert _open(tmp_path, "--profile", "shop-1") == 0  # admitted
    observer.observed = seen(hosting=True, cookies_at_start=2, cookies_at_end=7)
    assert _run(tmp_path, "--profile", "shop-1", "--strict") == 12
    assert _open(tmp_path, "--profile", "shop-1") == 4  # the same proxy, but the admission is gone
    assert opener.calls == 1
    data = _load(isolated_profile_store, "shop-1")
    assert data.network == OLD and data.cookie_count == 2 and data.last_preflight  # the earlier baseline stays


def test_a_pass_then_a_changed_address_revokes_and_does_not_move_the_baseline(
    observer: FakeObserver, opener: Recorder, isolated_profile_store: Path, tmp_path: Path
) -> None:
    observer.observed = seen(cookies_at_start=0, cookies_at_end=2)
    assert _run(tmp_path, "--profile", "shop-1") == 0
    observer.observed = seen(city="Other City", cookies_at_start=2, cookies_at_end=2)
    assert _run(tmp_path, "--profile", "shop-1") == 12
    assert _open(tmp_path, "--profile", "shop-1") == 4
    assert _load(isolated_profile_store, "shop-1").network == OLD
    # running the check again must not turn the changed address into the normal one
    assert _run(tmp_path, "--profile", "shop-1") == 12


@pytest.mark.parametrize("failure", [RuntimeError("boom"), KeyboardInterrupt()])
def test_a_check_that_dies_halfway_leaves_no_admission(
    observer: FakeObserver, opener: Recorder, isolated_profile_store: Path, tmp_path: Path, failure: BaseException
) -> None:
    observer.observed = seen(cookies_at_start=0, cookies_at_end=2)
    assert _run(tmp_path, "--profile", "shop-1") == 0
    observer.observed = failure  # type: ignore[assignment]
    try:
        _run(tmp_path, "--profile", "shop-1")
    except KeyboardInterrupt:
        pass
    assert _open(tmp_path, "--profile", "shop-1") == 4
    assert _load(isolated_profile_store, "shop-1").network == OLD


def test_a_passing_recheck_admits_again(observer: FakeObserver, opener: Recorder, tmp_path: Path) -> None:
    observer.observed = seen(cookies_at_start=0, cookies_at_end=2)
    assert _run(tmp_path, "--profile", "shop-1") == 0
    observer.observed = seen(hosting=True, cookies_at_start=2)
    assert _run(tmp_path, "--profile", "shop-1", "--strict") == 12
    observer.observed = seen(cookies_at_start=2)
    assert _run(tmp_path, "--profile", "shop-1", "--strict") == 0
    assert _open(tmp_path, "--profile", "shop-1") == 0


# ------------------------------------------------------------ H4: the record belongs to this profile and this proxy


def test_changing_the_configured_proxy_after_a_pass_is_refused_and_the_old_proxy_still_works(
    observer: FakeObserver, opener: Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observer.observed = seen(cookies_at_start=0, cookies_at_end=2)
    assert _run(tmp_path, "--profile", "shop-1") == 0
    capsys.readouterr()
    monkeypatch.setenv("HIB_PROXY_URL", OTHER_PROXY_URL)
    assert _open(tmp_path, "--profile", "shop-1") == 4
    out = json.loads(capsys.readouterr().out.splitlines()[0])
    assert out["exit_code"] == 4 and "proxy" in out["error"] and "scripts/preflight.py --profile shop-1" in out["error"]
    assert "other-pass" not in out["error"] and "switch" not in out["error"].lower()
    assert opener.calls == 0
    monkeypatch.setenv("HIB_PROXY_URL", PROXY_URL)  # a refusal revokes nothing
    assert _open(tmp_path, "--profile", "shop-1") == 0


def test_a_record_copied_from_another_profile_does_not_admit(
    observer: FakeObserver, opener: Recorder, isolated_profile_store: Path, tmp_path: Path
) -> None:
    observer.observed = seen(cookies_at_start=0, cookies_at_end=2)
    assert _run(tmp_path, "--profile", "shop-a") == 0
    _profile(isolated_profile_store, "shop-b")
    (isolated_profile_store / "shop-b" / "profile.json").write_text(
        (isolated_profile_store / "shop-a" / "profile.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert _open(tmp_path, "--profile", "shop-b") == 4
    assert opener.calls == 0
    assert _open(tmp_path, "--profile", "shop-a") == 0


def test_an_old_record_without_a_binding_does_not_admit(opener: Recorder, isolated_profile_store: Path, tmp_path: Path) -> None:
    fresh_preflight(isolated_profile_store, "shop-1")
    with ProfileStore(isolated_profile_store).acquire("shop-1", lambda: {}) as profile:
        profile.data.preflight_binding = ""
        profile.save()
    assert _open(tmp_path, "--profile", "shop-1") == 4


def test_with_a_provider_session_the_admission_lasts_no_longer_than_the_sticky_session(
    opener: Recorder, monkeypatch: pytest.MonkeyPatch, isolated_profile_store: Path, tmp_path: Path
) -> None:
    api_proxy = ProxyConfig("provider.example.test", 9000, "user-session-abc", "pw")
    monkeypatch.delenv("HIB_PROXY_URL")
    monkeypatch.setenv("MOBILEPROXY_API_TOKEN", "tok-1")
    monkeypatch.setattr(open_page, "resolve_session_proxy", lambda *args, **kwargs: SessionProxy(proxy=api_proxy))
    fresh_preflight(isolated_profile_store, "shop-1", age_h=3.0, proxy=api_proxy)  # the setting allows 12 h, the session lasts 2 h
    assert _open(tmp_path, "--profile", "shop-1") == 4
    fresh_preflight(isolated_profile_store, "shop-1", age_h=1.0, proxy=api_proxy)
    assert _open(tmp_path, "--profile", "shop-1") == 0
    # a record made for the password the provider handed out earlier
    fresh_preflight(isolated_profile_store, "shop-1", age_h=1.0, proxy=ProxyConfig("provider.example.test", 9000, "user-session-abc", "rotated"))
    assert _open(tmp_path, "--profile", "shop-1") == 4  # the provider rotated the password: another configuration
