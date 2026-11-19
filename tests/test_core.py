"""Unit tests for the hib helpers: no network, no browser.

All credentials below are synthetic and exist only in this file.
"""

from pathlib import Path

import pytest

from hib.challenge import detect_challenge
from hib.env import load_settings, parse_env_text
from hib.errors import ConfigError, ProxyApiError
from hib.proxy import proxy_from_api_response, proxy_from_url, redact, traffic_left_mb

LONG_TEXT = "Каталог товаров, доставка, акции и скидки. " * 20

# Shape of one item from mobileproxy.space get_my_proxy; values are made up.
API_FIXTURE = [
    {
        "proxy_id": 1,
        "proxy_hostname": "proxy.example.test",
        "proxy_http_port": "8001",
        "proxy_login": "demo_user",
        "proxy_pass": "s3cr:et/pass",
        "residential_traffic_left_mb": "987.5",
    }
]


@pytest.mark.parametrize(
    "title",
    ["Antibot Challenge Page", "Antibot Captcha", "Похоже, нет соединения", "antibot challenge page"],
)
def test_known_challenge_titles_are_detected(title: str) -> None:
    check = detect_challenge(title, LONG_TEXT)
    assert check.is_challenge
    assert check.reason


def test_challenge_phrase_in_body_is_detected() -> None:
    check = detect_challenge("OZON", "Похоже, нет соединения. Попробуйте выключить VPN. " + LONG_TEXT)
    assert check.is_challenge


def test_very_short_body_is_treated_as_challenge() -> None:
    check = detect_challenge("Что-то", "Подождите...")
    assert check.is_challenge
    assert "short" in check.reason


def test_normal_page_is_not_a_challenge() -> None:
    check = detect_challenge("OZON – интернет-магазин", LONG_TEXT)
    assert not check.is_challenge
    assert check.reason == ""


def test_proxy_built_from_api_response() -> None:
    proxy = proxy_from_api_response(API_FIXTURE)
    assert proxy.as_playwright() == {
        "server": "http://proxy.example.test:8001",
        "username": "demo_user",
        "password": "s3cr:et/pass",
    }
    # Special characters in the password are percent-encoded in the URL form.
    assert proxy.as_url() == "http://demo_user:s3cr%3Aet%2Fpass@proxy.example.test:8001"


def test_proxy_repr_hides_password() -> None:
    proxy = proxy_from_api_response(API_FIXTURE)
    assert "s3cr" not in repr(proxy)
    assert "s3cr" not in str(proxy)


@pytest.mark.parametrize("data", [[], {}, [{"proxy_hostname": "h"}], "error", None])
def test_bad_api_response_raises_api_error(data: object) -> None:
    with pytest.raises(ProxyApiError):
        proxy_from_api_response(data)


def test_traffic_left_is_read_when_present() -> None:
    assert traffic_left_mb(API_FIXTURE) == pytest.approx(987.5)
    assert traffic_left_mb({"residential_traffic_left_mb": 12}) == pytest.approx(12.0)
    assert traffic_left_mb({"status": "ok"}) is None


def test_proxy_from_url_round_trip() -> None:
    proxy = proxy_from_url("http://demo_user:s3cr%3Aet%2Fpass@proxy.example.test:8001")
    assert proxy.as_playwright()["password"] == "s3cr:et/pass"
    assert proxy.as_playwright()["server"] == "http://proxy.example.test:8001"


@pytest.mark.parametrize("url", ["proxy.example.test:8001", "ftp://h:1", "http://:8001", "http://h"])
def test_proxy_from_bad_url_raises_config_error(url: str) -> None:
    with pytest.raises(ConfigError):
        proxy_from_url(url)


def test_redact_removes_secrets() -> None:
    text = "connect failed for demo_user:s3cr:et/pass@proxy"
    assert redact(text, ["s3cr:et/pass", "demo_user", ""]) == "connect failed for ***:***@proxy"


def test_parse_env_text() -> None:
    text = (
        "# comment\n"
        "\n"
        "MOBILEPROXY_API_TOKEN=abc123\n"
        "export HIB_PROXY_URL='http://u:p@h:1'\n"
        'QUOTED="with spaces"  \n'
        "EMPTY=\n"
        "not a pair\n"
    )
    assert parse_env_text(text) == {
        "MOBILEPROXY_API_TOKEN": "abc123",
        "HIB_PROXY_URL": "http://u:p@h:1",
        "QUOTED": "with spaces",
        "EMPTY": "",
    }


def test_environment_wins_over_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MOBILEPROXY_API_TOKEN=from_file\nHIB_PROXY_URL=http://u:p@h:1\n", encoding="utf-8")
    settings = load_settings(env_file, {"MOBILEPROXY_API_TOKEN": "from_env"})
    assert settings.mobileproxy_token == "from_env"
    assert settings.proxy_url == "http://u:p@h:1"


def test_missing_env_file_gives_empty_settings(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.env", {})
    assert settings.mobileproxy_token is None
    assert settings.proxy_url is None
