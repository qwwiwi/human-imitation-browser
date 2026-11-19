"""HIB_PROXY_URL without credentials, SOCKS forms, the open-proxy warning and the two adapters."""

import json
import logging
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import check_proxy
import open_page
from conftest import preflight_for_args
from hib import browser, proxy, session
from hib.browser import PageResult
from hib.errors import ConfigError, ProxyApiError
from hib.proxy import ProxyConfig, check_exit_ip, proxy_from_url, redact, warn_if_open_proxy

URL = "https://shop.example.test/"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No proxy variable of the developer's shell may change a test."""
    for name in ("MOBILEPROXY_API_TOKEN", "HIB_PROXY_URL", "NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------ accepted forms


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://100.101.102.103:8888", {"server": "http://100.101.102.103:8888"}),
        ("socks5://127.0.0.1:18931", {"server": "socks5://127.0.0.1:18931"}),
        (
            "http://demo_user:s3cr%3Aet@proxy.example.test:8001",
            {"server": "http://proxy.example.test:8001", "username": "demo_user", "password": "s3cr:et"},
        ),
    ],
)
def test_accepted_forms_give_browser_settings_without_empty_credentials(url: str, expected: dict[str, str]) -> None:
    assert proxy_from_url(url).as_playwright() == expected


def test_credential_less_proxy_url_has_no_empty_login_part() -> None:
    config = proxy_from_url("http://100.101.102.103:8888")
    assert config.as_url() == "http://100.101.102.103:8888"
    assert "None" not in config.as_url() and "@" not in config.as_url()
    assert "None" not in str(config.as_playwright())


def test_credentials_still_round_trip_through_as_url() -> None:
    config = proxy_from_url("http://demo_user:s3cr%3Aet@proxy.example.test:8001")
    assert config.as_url() == "http://demo_user:s3cr%3Aet@proxy.example.test:8001"


# ------------------------------------------------------------ refused forms


ACCEPTED_FORMS = ("http://host:port", "http://login:password@host:port", "socks5://host:port")


@pytest.mark.parametrize(
    "url",
    [
        "proxy.example.test:8001",  # no scheme
        "ftp://h:1",  # unknown scheme
        "socks4://h:1",  # unknown scheme
        "http://h",  # no port
        "socks5://h",  # no port
        "http://:8001",  # empty host
        "socks5://:1080",  # empty host
        "http://onlyuser@h:1",  # a login without a password
        "http://:onlypass@h:1",  # a password without a login
    ],
)
def test_bad_forms_are_config_errors_that_list_the_accepted_forms(url: str) -> None:
    with pytest.raises(ConfigError) as info:
        proxy_from_url(url)
    for form in ACCEPTED_FORMS:
        assert form in str(info.value)


def test_socks_with_credentials_is_refused_clearly_and_never_echoes_them() -> None:
    with pytest.raises(ConfigError) as info:
        proxy_from_url("socks5://demo_user:s3cretpw@proxy.example.test:1080")
    text = str(info.value)
    assert "s3cretpw" not in text and "demo_user" not in text
    assert "SOCKS" in text and "login" in text


def test_socks5h_is_refused_with_a_hint_to_use_socks5() -> None:
    with pytest.raises(ConfigError) as info:
        proxy_from_url("socks5h://127.0.0.1:1080")
    assert "socks5://" in str(info.value)


def test_https_proxy_url_is_still_refused() -> None:
    with pytest.raises(ConfigError) as info:
        proxy_from_url("https://127.0.0.1:8888")
    assert "https://" in str(info.value)


# ------------------------------------------------------------ redaction


def test_redaction_does_not_break_without_credentials() -> None:
    config = proxy_from_url("socks5://127.0.0.1:18931")
    assert config.secrets() == []
    assert redact("cannot connect to 127.0.0.1:18931", config.secrets()) == "cannot connect to 127.0.0.1:18931"


def test_redaction_still_hides_credentials_when_present() -> None:
    config = proxy_from_url("http://demo_user:s3cretpw@proxy.example.test:8001")
    assert redact("failed for demo_user:s3cretpw", config.secrets()) == "failed for ***:***"


def test_repr_of_a_credential_less_proxy_is_clean() -> None:
    assert "None" not in repr(proxy_from_url("http://10.0.0.5:8888"))


# ------------------------------------------------------------ open-proxy warning


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "localhost",
        "10.1.2.3",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "100.64.0.1",
        "100.127.255.254",
        "home-box.tail1234.ts.net",
        "printer.local",
        "[::1]",
    ],
)
def test_private_hosts_get_no_warning(host: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert warn_if_open_proxy(proxy_from_url(f"http://{host}:8888")) is False
    assert caplog.text == ""


@pytest.mark.parametrize("host", ["203.0.113.5", "11.0.0.1", "172.32.0.1", "100.63.255.255", "100.128.0.1", "proxy.example.test"])
def test_public_host_without_credentials_gets_one_warning(host: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert warn_if_open_proxy(proxy_from_url(f"http://{host}:8888")) is True
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "anyone" in caplog.text


def test_public_host_with_credentials_gets_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert warn_if_open_proxy(proxy_from_url("http://u:p@203.0.113.5:8888")) is False
    assert caplog.text == ""


# ------------------------------------------------------------ open_page.py


class Recorder:
    """Stands in for hib.browser.open_pages; remembers the proxy it was given."""

    def __init__(self) -> None:
        self.proxies: list[ProxyConfig | None] = []

    def __call__(self, urls, proxy, options, emit, profile=None, pacer=None):  # type: ignore[no-untyped-def]
        self.proxies.append(proxy)
        emit(PageResult(URL, 200, "t", 10, "", False, ""))
        return False


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(browser, "open_pages", rec)
    monkeypatch.setattr(browser, "make_fingerprint", lambda: {"navigator": {"userAgent": "synthetic"}})
    return rec


def _open(tmp_path: Path, *args: str, preflighted: bool = True) -> int:
    if preflighted:
        preflight_for_args(args)  # the tool refuses a profile without a recent pre-flight
    return open_page.main(["--env-file", str(tmp_path / "missing.env"), *args, URL])


def test_open_page_accepts_a_socks_proxy_without_credentials(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HIB_PROXY_URL", "socks5://127.0.0.1:18931")
    assert _open(tmp_path, "--profile", "x") == 0
    assert recorder.proxies[0] is not None
    assert recorder.proxies[0].as_playwright() == {"server": "socks5://127.0.0.1:18931"}  # type: ignore[union-attr]
    assert json.loads(capsys.readouterr().out.splitlines()[0])["profile"] == "x"


def test_open_page_with_a_bad_proxy_url_exits_4_and_lists_the_forms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HIB_PROXY_URL", "http://127.0.0.1")
    assert _open(tmp_path, "--profile", "x") == 4
    out = json.loads(capsys.readouterr().out)
    assert out["exit_code"] == 4
    assert "socks5://host:port" in out["error"]


def test_a_given_proxy_url_means_no_provider_call_and_no_session_substitution(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("the provider API must not be called when HIB_PROXY_URL is set")

    monkeypatch.setenv("HIB_PROXY_URL", "http://100.101.102.103:8888")
    monkeypatch.setenv("MOBILEPROXY_API_TOKEN", "tok-1")
    monkeypatch.setattr(proxy, "call_mobileproxy", forbidden)
    monkeypatch.setattr(session, "call_mobileproxy", forbidden)
    monkeypatch.setattr(open_page, "resolve_session_proxy", forbidden)
    assert _open(tmp_path, "--profile", "x") == 0
    assert _open(tmp_path, "--ephemeral") == 0
    assert [p.as_playwright() for p in recorder.proxies if p] == [{"server": "http://100.101.102.103:8888"}] * 2


def test_open_page_prints_the_open_proxy_warning_once(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HIB_PROXY_URL", "http://203.0.113.5:8888")
    with caplog.at_level(logging.WARNING):
        assert _open(tmp_path, "--ephemeral") == 0
    assert caplog.text.count("anyone") == 1


# ------------------------------------------------------------ check_proxy.py and the urllib adapter


class _ProxyHandler(BaseHTTPRequestHandler):
    """A fake http proxy: remembers the request line and headers, answers a fixed JSON."""

    seen: list[tuple[str, dict[str, str]]] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        type(self).seen.append((self.path, dict(self.headers)))
        body = json.dumps({"country": "RU", "city": "Test City", "org": "Test Org"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return None


@contextmanager
def _fake_proxy() -> Iterator[int]:
    _ProxyHandler.seen = []
    server = HTTPServer(("127.0.0.1", 0), _ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_exit_check_works_through_a_proxy_without_credentials() -> None:
    with _fake_proxy() as port:
        info = check_exit_ip(ProxyConfig("127.0.0.1", port), url="http://ipinfo.example.test/json")
    assert info == {"country": "RU", "city": "Test City", "org": "Test Org"}
    path, headers = _ProxyHandler.seen[0]
    assert path.startswith("http://ipinfo.example.test/")
    assert not any(name.lower() == "proxy-authorization" for name in headers)


def test_check_proxy_script_with_a_plain_http_proxy_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[ProxyConfig] = []

    def fake_check(config: ProxyConfig, url: str = "") -> dict[str, str]:
        seen.append(config)
        return {"country": "RU", "city": "C", "org": "O"}

    monkeypatch.setenv("HIB_PROXY_URL", "http://100.101.102.103:8888")
    monkeypatch.setattr(check_proxy, "check_exit_ip", fake_check)
    monkeypatch.setattr(check_proxy, "check_reputation", lambda proxy, url="": {"hosting": None, "proxy": None})
    assert check_proxy.main(["--env-file", str(tmp_path / "missing.env")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["source"] == "HIB_PROXY_URL" and out["ru"] is True
    assert seen[0].as_url() == "http://100.101.102.103:8888"


def test_check_proxy_script_says_plainly_that_socks_cannot_be_checked_here(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("urllib cannot speak SOCKS: no request may be tried")

    monkeypatch.setenv("HIB_PROXY_URL", "socks5://127.0.0.1:18931")
    monkeypatch.setattr(check_proxy, "check_exit_ip", forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    with caplog.at_level(logging.INFO):
        assert check_proxy.main(["--env-file", str(tmp_path / "missing.env")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["checked"] is False
    assert "scripts/preflight.py --profile <name>" in out["note"]  # open_page.py now needs a pre-flight first
    assert "SOCKS" in out["note"]
    assert "ru" not in out


# ------------------------------------------------------------ provider API address


@pytest.mark.parametrize("address", ["http://gw.example.test:9000", "socks5://gw.example.test:9000"])
def test_a_provider_address_without_a_login_is_an_api_error_not_a_crash(address: str) -> None:
    with pytest.raises(ProxyApiError):
        session._build(address, "abc123", "RU", "Test City")
