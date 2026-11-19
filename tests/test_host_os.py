"""A new profile's fingerprint claims the host OS; existing profiles keep theirs. Plus the profiles CLI name."""

import importlib.util
import json
from pathlib import Path

import pytest

from hib import browser
from hib.env import REPO_ROOT
from hib.profiles import FINGERPRINT_FILE, ProfileStore

SCRIPTS = REPO_ROOT / "scripts"


@pytest.mark.parametrize(
    ("system", "expected"),
    [("Linux", "linux"), ("Windows", "windows"), ("Darwin", "macos")],
)
def test_new_fingerprint_claims_the_host_os(monkeypatch: pytest.MonkeyPatch, system: str, expected: str) -> None:
    seen: dict[str, object] = {}

    def fake_generate(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"navigator": {}}

    monkeypatch.setattr(browser.platform, "system", lambda: system)
    monkeypatch.setattr(browser, "generate_fingerprint", fake_generate)
    browser.make_fingerprint()
    assert seen["os"] == expected
    assert seen["screen"] is browser.PROFILE_SCREEN


def test_unknown_host_os_falls_back_to_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser.platform, "system", lambda: "FreeBSD")
    assert browser.host_os() == "linux"


def test_existing_profile_keeps_its_stored_fingerprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ProfileStore(tmp_path / "profiles")
    old = {"navigator": {"userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, "os": "windows"}
    with store.acquire("shop", lambda: dict(old)):
        pass
    monkeypatch.setattr(browser.platform, "system", lambda: "Linux")
    monkeypatch.setattr(browser, "generate_fingerprint", lambda **_: pytest.fail("an existing profile must not redraw"))
    with store.acquire("shop", browser.make_fingerprint) as profile:
        assert profile.fingerprint == old
        assert profile.data.os == "windows"
    stored = json.loads((tmp_path / "profiles" / "shop" / FINGERPRINT_FILE).read_text(encoding="utf-8"))
    assert stored == old


def test_profiles_cli_does_not_shadow_the_stdlib_profile_module() -> None:
    assert not (SCRIPTS / "profile.py").exists()
    assert (SCRIPTS / "profiles.py").is_file()
    spec = importlib.util.find_spec("profile")
    assert spec is not None and spec.origin is not None
    assert Path(spec.origin).parent != SCRIPTS


def test_no_mention_of_the_old_cli_name() -> None:
    files = [REPO_ROOT / "README.md", REPO_ROOT / "docs" / "guide.md", REPO_ROOT / "skill" / "SKILL.md"]
    files += sorted(SCRIPTS.rglob("*.py")) + sorted((REPO_ROOT / "tests").glob("*.py"))
    for path in files:
        if path.name == "test_host_os.py":
            continue
        assert "profile.py" not in path.read_text(encoding="utf-8").replace("profiles.py", ""), path
