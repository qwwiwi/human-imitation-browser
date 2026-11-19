"""Shared fixtures: no test may touch the developer's real profile folder."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hib.errors import ProfileError
from hib.profiles import ProfileStore, validate_profile_name

HOME_NETWORK = {"country": "RU", "city": "Test City", "owner": "Test Home Net"}


def preflight_for_args(args: tuple[str, ...]) -> None:
    """Give the profile named in a command line (or `default`) a fresh pre-flight; nothing for `--ephemeral`.

    open_page.py refuses a profile without a recent pre-flight, so tests that run it on a
    synthetic address call this first. A bad profile name is left alone: the command itself must refuse it.
    """
    if "--ephemeral" in args:
        return
    name = args[args.index("--profile") + 1] if "--profile" in args else "default"
    try:
        validate_profile_name(name)
    except ProfileError:
        return
    fresh_preflight(Path(os.environ["HIB_PROFILES_DIR"]), name)


def fresh_preflight(root: Path, name: str, age_h: float = 0.0, network: dict[str, str] | None = None) -> None:
    """Create a profile (if needed) that passed its pre-flight `age_h` hours ago.

    Tests that open a real-looking address through open_page.py need this: the tool refuses
    a profile without a recent pre-flight. The synthetic network is what that pre-flight saw.
    """
    store = ProfileStore(root)
    with store.acquire(name, lambda: {"navigator": {}}) as profile:
        profile.data.last_preflight = (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat(timespec="seconds")
        profile.data.network = dict(HOME_NETWORK if network is None else network)
        profile.save()


@pytest.fixture(autouse=True)
def isolated_profile_store(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Point HIB_PROFILES_DIR at a throw-away folder for every test."""
    root = tmp_path_factory.mktemp("hib-profiles") / "profiles"
    monkeypatch.setenv("HIB_PROFILES_DIR", str(root))
    for name in ("HIB_PROXY_STATE", "HIB_PROXY_CITY", "HIB_PROXY_ID", "HIB_MIN_GAP_S"):
        monkeypatch.delenv(name, raising=False)
    return root
