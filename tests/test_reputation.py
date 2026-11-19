"""The address reputation lookup and the reputation line of check_proxy.py.

No real network: a local fake proxy answers on 127.0.0.1 only; all values are synthetic.
"""

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import check_proxy
from hib.proxy import PROXY_REPUTATION_URL, ProxyConfig, check_reputation, parse_reputation

SYNTHETIC_URL = "http://demo_user:s3cret-pass@127.0.0.1:18932"
FLAGGED = {"status": "success", "hosting": True, "proxy": True}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No proxy variable of the developer's shell may change a test."""
    for name in ("MOBILEPROXY_API_TOKEN", "HIB_PROXY_URL", "NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)


def _handler(answer: object, code: int = 200) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        seen: list[str] = []

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            type(self).seen.append(self.path)
            body = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return None

    return Handler


@contextmanager
def _fake_proxy(handler: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ------------------------------------------------------------ check_reputation


def test_reputation_reads_the_hosting_and_proxy_flags_through_the_proxy() -> None:
    handler = _handler(FLAGGED)
    with _fake_proxy(handler) as port:
        flags = check_reputation(ProxyConfig("127.0.0.1", port), url="http://rep.example.test/json")
    assert flags == {"hosting": True, "proxy": True, "mobile": None}  # this answer has no `mobile` field
    assert handler.seen[0].startswith("http://rep.example.test/")  # the request went through the proxy


def test_reputation_keeps_false_apart_from_unknown() -> None:
    with _fake_proxy(_handler({"status": "success", "hosting": False, "proxy": False})) as port:
        flags = check_reputation(ProxyConfig("127.0.0.1", port), url="http://rep.example.test/json")
    assert flags == {"hosting": False, "proxy": False, "mobile": None}


def test_reputation_reads_the_mobile_flag_when_the_service_gives_it() -> None:
    answer = {"status": "success", "hosting": False, "proxy": False}
    assert parse_reputation({**answer, "mobile": True})["mobile"] is True
    assert parse_reputation({**answer, "mobile": False})["mobile"] is False
    assert parse_reputation({**answer, "mobile": "yes"})["mobile"] is None
    assert parse_reputation({"status": "fail"}) == {"hosting": None, "proxy": None, "mobile": None}


def test_the_plain_http_lookup_of_check_proxy_asks_for_the_mobile_field_too() -> None:
    assert PROXY_REPUTATION_URL.startswith("http://ip-api.com/json/")
    fields = PROXY_REPUTATION_URL.split("fields=")[1].split(",")
    assert {"status", "hosting", "proxy", "mobile"} <= set(fields)


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ({"status": "fail", "message": "reserved range"}, 200),
        ({"status": "success"}, 200),
        ({"status": "success", "hosting": "yes", "proxy": 1}, 200),
        (b"not json", 200),
        ([1, 2], 200),
        ({"status": "fail"}, 429),
    ],
)
def test_reputation_that_cannot_be_read_is_unknown_not_an_error(answer: object, code: int) -> None:
    with _fake_proxy(_handler(answer, code)) as port:
        flags = check_reputation(ProxyConfig("127.0.0.1", port), url="http://rep.example.test/json")
    assert flags == {"hosting": None, "proxy": None, "mobile": None}


def test_reputation_with_a_dead_proxy_is_unknown_not_an_error() -> None:
    flags = check_reputation(ProxyConfig("127.0.0.1", 1), url="http://rep.example.test/json")
    assert flags == {"hosting": None, "proxy": None, "mobile": None}


# ------------------------------------------------------------ check_proxy.py output


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    reputation: dict[str, bool | None],
) -> tuple[dict[str, object], str]:
    monkeypatch.setenv("HIB_PROXY_URL", SYNTHETIC_URL)
    monkeypatch.setattr(check_proxy, "check_exit_ip", lambda proxy, url="": {"country": "RU", "city": "C", "org": "AS1 Demo Net"})
    monkeypatch.setattr(check_proxy, "check_reputation", lambda proxy, url="": reputation)
    with caplog.at_level(logging.INFO):
        assert check_proxy.main(["--env-file", str(tmp_path / "missing.env")]) == 0
    captured = capsys.readouterr()
    return json.loads(captured.out), caplog.text + captured.err


def test_script_prints_owner_and_reputation_in_json_and_in_a_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    out, log = _run(monkeypatch, tmp_path, capsys, caplog, {"hosting": True, "proxy": False, "mobile": True})
    assert out["mobile"] is True
    assert out["network_owner"] == "AS1 Demo Net"
    assert out["hosting"] is True and out["proxy_flag"] is False
    assert out["country"] == "RU" and out["ru"] is True  # the old fields stay
    assert "reputation: hosting=yes, proxy=no" in log


def test_script_says_unknown_when_the_reputation_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    out, log = _run(monkeypatch, tmp_path, capsys, caplog, {"hosting": None, "proxy": None, "mobile": None})
    assert out["hosting"] is None and out["proxy_flag"] is None and out["mobile"] is None
    assert "reputation: unknown" in log


def test_script_never_prints_the_proxy_url_login_or_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    out, log = _run(monkeypatch, tmp_path, capsys, caplog, {"hosting": False, "proxy": False})
    everything = json.dumps(out) + log
    for secret in ("demo_user", "s3cret-pass", "18932"):
        assert secret not in everything


