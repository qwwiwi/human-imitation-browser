"""Pace between navigations to the same host, kept across runs of one profile."""

import logging
import math
import time
from collections.abc import Callable

from hib.errors import ConfigError

logger = logging.getLogger(__name__)

DEFAULT_MIN_GAP_S = 20.0
MIN_GAP_FLOOR_S = 5.0


def parse_min_gap(value: str | None) -> float:
    """Read the minimal gap between two navigations to one host (env HIB_MIN_GAP_S).

    Args:
        value: Text from the environment, or None for the default.

    Returns:
        Seconds, never below the floor of 5.

    Raises:
        ConfigError: The text is not a finite non-negative number.
    """
    if value is None or not value.strip():
        return DEFAULT_MIN_GAP_S
    try:
        gap = float(value)
    except ValueError:
        raise ConfigError("HIB_MIN_GAP_S must be a number of seconds") from None
    if not math.isfinite(gap) or gap < 0:
        raise ConfigError("HIB_MIN_GAP_S must be a number of seconds, zero or more")
    if gap < MIN_GAP_FLOOR_S:
        logger.warning("HIB_MIN_GAP_S=%g is below the minimum, using %g s", gap, MIN_GAP_FLOOR_S)
        return MIN_GAP_FLOOR_S
    return gap


def wait_seconds(last: float | None, now: float, min_gap: float) -> float:
    """Return how long to wait before the next navigation to a host.

    Args:
        last: Time of the previous navigation to this host, or None.
        now: Current time.
        min_gap: Minimal gap in seconds.

    Returns:
        Seconds to wait, from 0 up to min_gap. A saved time in the future (the
        clock went back) counts as a navigation just now.
    """
    if last is None:
        return 0.0
    elapsed = min(max(now - last, 0.0), min_gap)
    return min_gap - elapsed


class Pacer:
    """Keeps at least `min_gap` seconds between navigations to one host.

    The state maps a host name to the time of its last navigation. The caller
    keeps it in the profile file, so the gap holds across separate runs.
    """

    def __init__(
        self,
        state: dict[str, float],
        min_gap: float,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """Create a pacer.

        Args:
            state: Host -> time of the last navigation; changed in place.
            min_gap: Minimal gap in seconds.
            clock: Returns the current time; default `time.time` (replaceable in tests).
            sleep: Sleeps for seconds; default `time.sleep` (replaceable in tests).
            on_change: Called after the state changed, for example to save it.
        """
        self._state = state
        self._min_gap = min_gap
        self._clock = clock or time.time
        self._sleep = sleep or time.sleep
        self._on_change = on_change

    def wait_turn(self, host: str) -> float:
        """Sleep the rest of the gap for this host, then record the navigation.

        Args:
            host: Host name of the address about to be opened.

        Returns:
            Seconds slept.
        """
        host = host.lower()
        waited = wait_seconds(self._state.get(host), self._clock(), self._min_gap)
        if waited > 0:
            logger.info("pacing: waiting %.0f s before opening %s again", waited, host)
            self._sleep(waited)
        self._state[host] = self._clock()
        if self._on_change:
            self._on_change()
        return waited
