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
