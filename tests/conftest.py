"""Shared fixtures: no test may touch the developer's real profile folder."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_profile_store(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Point HIB_PROFILES_DIR at a throw-away folder for every test."""
    root = tmp_path_factory.mktemp("hib-profiles") / "profiles"
    monkeypatch.setenv("HIB_PROFILES_DIR", str(root))
    for name in ("HIB_PROXY_STATE", "HIB_PROXY_CITY", "HIB_PROXY_ID", "HIB_MIN_GAP_S"):
        monkeypatch.delenv(name, raising=False)
    return root
