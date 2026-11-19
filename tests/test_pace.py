"""Pace between navigations to one host, across runs."""

import logging

import pytest

from hib.errors import ConfigError
from hib.pace import DEFAULT_MIN_GAP_S, MIN_GAP_FLOOR_S, Pacer, parse_min_gap, wait_seconds


class Clock:
    """A fake clock whose sleep moves time forward."""

    def __init__(self, now: float) -> None:
        self.now = now
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_no_wait_for_a_host_never_seen() -> None:
    assert wait_seconds(None, 1000.0, 20.0) == 0.0


def test_wait_is_the_rest_of_the_gap() -> None:
    assert wait_seconds(990.0, 1000.0, 20.0) == pytest.approx(10.0)


def test_no_wait_once_the_gap_has_passed() -> None:
    assert wait_seconds(900.0, 1000.0, 20.0) == 0.0


def test_a_clock_that_went_backwards_waits_at_most_one_gap() -> None:
    assert wait_seconds(2000.0, 1000.0, 20.0) == 20.0


def test_pacer_sleeps_the_remainder_and_logs_it(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock(1000.0)
    state: dict[str, float] = {"shop.example.test": 995.0}
    pacer = Pacer(state, 20.0, clock=clock.time, sleep=clock.sleep)
    with caplog.at_level(logging.INFO):
        waited = pacer.wait_turn("shop.example.test")
    assert waited == pytest.approx(15.0)
    assert clock.slept == [pytest.approx(15.0)]
    assert "15" in caplog.text


def test_pacer_remembers_the_navigation_for_the_next_run() -> None:
    clock = Clock(1000.0)
    state: dict[str, float] = {}
    saved: list[float] = []
    first = Pacer(state, 20.0, clock=clock.time, sleep=clock.sleep, on_change=lambda: saved.append(1.0))
    assert first.wait_turn("shop.example.test") == 0.0
    clock.now += 8
    second = Pacer(state, 20.0, clock=clock.time, sleep=clock.sleep)  # a later run reads the same saved state
    assert second.wait_turn("shop.example.test") == pytest.approx(12.0)
    assert saved


def test_pacer_does_not_wait_between_different_hosts() -> None:
    clock = Clock(1000.0)
    pacer = Pacer({"a.example.test": 999.0}, 20.0, clock=clock.time, sleep=clock.sleep)
    assert pacer.wait_turn("b.example.test") == 0.0
    assert clock.slept == []


def test_min_gap_default_and_override() -> None:
    assert parse_min_gap(None) == DEFAULT_MIN_GAP_S == 20.0
    assert parse_min_gap("45") == 45.0


def test_min_gap_below_the_floor_is_raised(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert parse_min_gap("1") == MIN_GAP_FLOOR_S == 5.0
    assert "5" in caplog.text


@pytest.mark.parametrize("value", ["abc", "nan", "inf", "-3"])
def test_bad_min_gap_is_a_config_error(value: str) -> None:
    with pytest.raises(ConfigError):
        parse_min_gap(value)
