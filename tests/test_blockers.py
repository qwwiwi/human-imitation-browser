"""Regression tests for the review blockers H1-H6: no real network, no browser.

Local HTTP servers listen on 127.0.0.1 only. All credentials are synthetic.
"""

import json
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import check_proxy
import open_page
from hib import browser, proxy
from hib.browser import OpenOptions, open_one
from hib.errors import ConfigError, PageServerError, ProxyApiError, ProxyUnreachableError
from hib.proxy import ProxyConfig, call_mobileproxy, check_exit_ip, proxy_from_url

LONG_TEXT = "Каталог товаров, доставка, акции и скидки. " * 20
SYNTHETIC_PROXY = ProxyConfig(host="proxy.example.test", port=8001, username="demo_user", password="s3cret")


@pytest.fixture(autouse=True)
def clean_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own proxy variables out of every test."""
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")


@contextmanager
def local_server(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    """Run an HTTP server on a free loopback port and yield its base URL."""
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- H1


def test_api_redirect_does_not_forward_the_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"[]")

        def log_message(self, *args: object) -> None:
            return

    with local_server(Target) as target_url:

        class Redirector(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", target_url + "/stolen")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                return

        with local_server(Redirector) as api_url:
            monkeypatch.setattr(proxy, "MOBILEPROXY_API", api_url + "/api.html")
            with pytest.raises(ProxyApiError):
                call_mobileproxy("get_my_proxy", "tok-synthetic")
    assert seen == []


# ---------------------------------------------------------------- H2


@pytest.mark.parametrize("token", ["abc\ndef", "abc\r\nX-Evil: 1", "abc def", "tokжen", "abc\x00"])
def test_bad_token_gives_fixed_config_error_without_the_value(token: str) -> None:
    with pytest.raises(ConfigError) as info:
        call_mobileproxy("get_my_proxy", token)
    assert "abc" not in str(info.value)
    assert "tok" not in str(info.value)


def test_check_proxy_with_multiline_token_exits_4_with_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("MOBILEPROXY_API_TOKEN", "frag-one\nfrag-two")
    code = check_proxy.main(["--env-file", str(tmp_path / "missing.env")])
    out = capsys.readouterr().out
    assert code == 4
    assert json.loads(out)["exit_code"] == 4
    assert "frag" not in out


# ---------------------------------------------------------------- H3


def test_https_proxy_url_is_rejected_not_downgraded() -> None:
    with pytest.raises(ConfigError) as info:
        proxy_from_url("https://demo_user:s3cret@proxy.example.test:8001")
    assert "s3cret" not in str(info.value)


# ---------------------------------------------------------------- H4


def test_exit_ip_check_fails_closed_when_no_proxy_bypasses_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "ipinfo.io")
    monkeypatch.setenv("no_proxy", "ipinfo.io")
    opened: list[object] = []

    def fake_open(self: urllib.request.OpenerDirector, *args: object, **kwargs: object) -> object:
        opened.append(args)
        raise AssertionError("the request must not be sent")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)
    with pytest.raises(ProxyUnreachableError) as info:
        check_exit_ip(SYNTHETIC_PROXY)
    assert opened == []
    assert "NO_PROXY" in str(info.value)


# ---------------------------------------------------------------- H5


class FakeResponse:
    """Stands in for the Playwright response of the main document."""

    def __init__(self, status: int) -> None:
        self.status = status


class FakeMouse:
    """Counts pointer and wheel actions."""

    def __init__(self) -> None:
        self.actions = 0

    def move(self, *args: object, **kwargs: object) -> None:
        self.actions += 1

    def wheel(self, *args: object, **kwargs: object) -> None:
        self.actions += 1


class FakePage:
    """A page with a fixed title and body text; records what was done to it."""

    url = "https://shop.example.test/"
    viewport_size = {"width": 1280, "height": 720}

    def __init__(self, status: int, title: str, text: str) -> None:
        self._response = FakeResponse(status)
        self._title = title
        self._text = text
        self.mouse = FakeMouse()
        self.shots: list[str] = []

    def goto(self, *args: object, **kwargs: object) -> FakeResponse:
        return self._response

    def title(self) -> str:
        return self._title

    def inner_text(self, *args: object, **kwargs: object) -> str:
        return self._text

    def screenshot(self, path: str) -> None:
        self.shots.append(path)


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "_sleep", lambda low, high: None)


def _options(tmp_path: Path) -> OpenOptions:
    return OpenOptions(out_dir=tmp_path)


def test_server_error_with_long_body_is_a_failure_with_its_own_code(no_sleep: None, tmp_path: Path) -> None:
    page = FakePage(500, "Internal Server Error", LONG_TEXT)
    with pytest.raises(PageServerError) as info:
        open_one(page, "https://shop.example.test/", _options(tmp_path), [])  # type: ignore[arg-type]
    assert info.value.exit_code == 9
    assert "500" in str(info.value)


def test_rendered_403_page_stays_a_normal_result(no_sleep: None, tmp_path: Path) -> None:
    page = FakePage(403, "Магазин", LONG_TEXT)
    result = open_one(page, "https://shop.example.test/", _options(tmp_path), [])  # type: ignore[arg-type]
    assert result.status == 403
    assert not result.challenge


def test_server_error_page_that_is_a_challenge_still_stops_as_challenge(no_sleep: None, tmp_path: Path) -> None:
    page = FakePage(503, "Antibot Challenge Page", LONG_TEXT)
    result = open_one(page, "https://shop.example.test/", _options(tmp_path), [])  # type: ignore[arg-type]
    assert result.challenge


def test_unclosed_bracket_in_proxy_url_is_a_config_error() -> None:
    with pytest.raises(ConfigError) as info:
        proxy_from_url("http://demo_user:s3cret@[::1:8001")
    assert "s3cret" not in str(info.value)


def test_open_page_with_malformed_proxy_url_exits_4_with_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HIB_PROXY_URL", "http://demo_user:s3cret@[::1:8001")
    code = open_page.main(["--env-file", str(tmp_path / "missing.env"), "https://shop.example.test/"])
    out = capsys.readouterr().out
    assert code == 4
    assert json.loads(out)["exit_code"] == 4
    assert "s3cret" not in out


def test_check_proxy_with_malformed_proxy_url_exits_4_with_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HIB_PROXY_URL", "http://demo_user:s3cret@[::1:8001")
    code = check_proxy.main(["--env-file", str(tmp_path / "missing.env")])
    out = capsys.readouterr().out
    assert code == 4
    assert json.loads(out)["exit_code"] == 4


# ---------------------------------------------------------------- H6


def test_challenge_present_at_navigation_is_flagged_before_any_interaction(no_sleep: None, tmp_path: Path) -> None:
    page = FakePage(200, "Antibot Captcha", LONG_TEXT)
    result = open_one(page, "https://shop.example.test/", _options(tmp_path), [])  # type: ignore[arg-type]
    assert result.challenge
    assert page.mouse.actions == 0
    assert len(page.shots) == 1


def test_ordinary_page_still_gets_the_human_like_actions(no_sleep: None, tmp_path: Path) -> None:
    page = FakePage(200, "Магазин", LONG_TEXT)
    result = open_one(page, "https://shop.example.test/", _options(tmp_path), [])  # type: ignore[arg-type]
    assert not result.challenge
    assert page.mouse.actions > 0


def test_challenge_that_appears_after_the_actions_is_still_caught(no_sleep: None, tmp_path: Path) -> None:
    page = FakePage(200, "Магазин", LONG_TEXT)

    def appear_late(*args: object, **kwargs: object) -> None:
        page._title = "Antibot Captcha"

    page.mouse.wheel = appear_late  # type: ignore[method-assign]
    result = open_one(page, "https://shop.example.test/", _options(tmp_path), [])  # type: ignore[arg-type]
    assert result.challenge


def test_slow_rendering_page_is_not_flagged_by_the_early_check(no_sleep: None, tmp_path: Path) -> None:
    # Right after navigation the text may still be empty; only the final check judges the length.
    page = FakePage(200, "Магазин", "")
    texts = iter(["", LONG_TEXT])
    page.inner_text = lambda *a, **k: next(texts, LONG_TEXT)  # type: ignore[method-assign]
    result = open_one(page, "https://shop.example.test/", _options(tmp_path), [])  # type: ignore[arg-type]
    assert not result.challenge
    assert page.mouse.actions > 0
