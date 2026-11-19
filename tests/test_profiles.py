"""Profile store: names, file modes, lock, creation and reuse. No browser, no network."""

import json
import stat
from pathlib import Path

import pytest

from hib.env import REPO_ROOT
from hib.errors import ConfigError, ProfileBusyError, ProfileError
from hib.profiles import ProfileStore, profiles_root, validate_profile_name

FAKE_FINGERPRINT = {"navigator": {"userAgent": "Mozilla/5.0 (synthetic)"}, "screen": {"width": 1920, "height": 1080}}


def _generate() -> dict[str, object]:
    return dict(FAKE_FINGERPRINT)


@pytest.fixture()
def store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles")


@pytest.mark.parametrize("name", ["default", "a", "0", "shop-1", "my_task", "a" * 32])
def test_valid_profile_names_are_accepted(name: str) -> None:
    assert validate_profile_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "../x", "a/b", "A", "-a", "_a", "a" * 33, "a b", ".hidden", "ok\n", "..", "a\\b", "имя", "x.y"],
)
def test_bad_profile_names_are_refused(name: str) -> None:
    with pytest.raises(ProfileError):
        validate_profile_name(name)


def test_profile_folder_and_files_are_private(store: ProfileStore) -> None:
    with store.acquire("default", _generate) as profile:
        paths = [profile.path, profile.user_data_dir, store.root]
        files = [profile.path / "profile.json", profile.path / "fingerprint.json", profile.path / "lock"]
    for folder in paths:
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700, folder
    for file in files:
        assert stat.S_IMODE(file.stat().st_mode) == 0o600, file


def test_creation_generates_once_and_reuses_the_same_identity(store: ProfileStore) -> None:
    calls: list[int] = []

    def generate() -> dict[str, object]:
        calls.append(1)
        return _generate()

    with store.acquire("default", generate) as first:
        session = first.data.proxy_session
        assert first.fingerprint == FAKE_FINGERPRINT
    with store.acquire("default", generate) as second:
        assert second.data.proxy_session == session
        assert second.fingerprint == FAKE_FINGERPRINT
    assert len(calls) == 1
    assert len(session) >= 8


def test_two_profiles_get_different_proxy_sessions(store: ProfileStore) -> None:
    with store.acquire("one", _generate) as one, store.acquire("two", _generate) as two:
        assert one.data.proxy_session != two.data.proxy_session


def test_second_run_on_the_same_profile_is_refused_while_the_first_runs(store: ProfileStore) -> None:
    with store.acquire("default", _generate):
        with pytest.raises(ProfileBusyError) as info:
            with store.acquire("default", _generate):
                pass
    assert info.value.exit_code == 10
    assert "default" in str(info.value)
    with store.acquire("default", _generate):
        pass  # the lock is free again after the first run ended


def test_lock_is_released_when_the_run_fails(store: ProfileStore) -> None:
    with pytest.raises(RuntimeError):
        with store.acquire("default", _generate):
            raise RuntimeError("boom")
    with store.acquire("default", _generate):
        pass


def test_visits_and_last_used_are_counted(store: ProfileStore) -> None:
    with store.acquire("default", _generate) as profile:
        assert profile.data.visits == 0
        profile.record_visit()
        assert profile.data.visits == 1
    with store.acquire("default", _generate) as profile:
        assert profile.data.visits == 1
        assert profile.data.last_used
        profile.record_visit()
    assert store.load("default").data.visits == 2


def test_profile_json_has_no_secret_and_show_hides_the_session(store: ProfileStore) -> None:
    with store.acquire("default", _generate) as profile:
        session = profile.data.proxy_session
        profile.data.last_exit = {"country": "RU", "city": "Test City"}
        profile.save()
        shown = json.dumps(profile.describe())
        assert session not in shown
        assert session not in repr(profile)
        assert session not in repr(profile.data)
    shown_after = store.load("default").describe()
    assert shown_after["name"] == "default"
    assert shown_after["last_exit"] == {"country": "RU", "city": "Test City"}
    assert "fingerprint" not in shown_after


def test_storage_root_defaults_to_the_home_folder(tmp_path: Path) -> None:
    assert profiles_root(None, home=tmp_path) == tmp_path / ".local" / "share" / "hib" / "profiles"


def test_storage_root_can_be_overridden(tmp_path: Path) -> None:
    assert profiles_root(str(tmp_path / "custom"), home=tmp_path) == tmp_path / "custom"


def test_storage_inside_the_repository_is_refused() -> None:
    with pytest.raises(ConfigError):
        profiles_root(str(REPO_ROOT / "profiles"), home=Path("/nonexistent"))


def test_damaged_profile_file_gives_a_clear_error(store: ProfileStore) -> None:
    with store.acquire("default", _generate) as profile:
        (profile.path / "profile.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ProfileError) as info:
        with store.acquire("default", _generate):
            pass
    assert info.value.exit_code == 11


def test_listing_shows_only_complete_profiles(store: ProfileStore) -> None:
    with store.acquire("b-one", _generate), store.acquire("a-one", _generate):
        pass
    (store.root / "half").mkdir()
    assert store.names() == ["a-one", "b-one"]


def test_load_of_a_missing_profile_is_an_error(store: ProfileStore) -> None:
    with pytest.raises(ProfileError):
        store.load("nope")


def test_delete_removes_the_whole_folder(store: ProfileStore) -> None:
    with store.acquire("gone", _generate) as profile:
        (profile.user_data_dir / "cookies.sqlite").write_text("x", encoding="utf-8")
    store.delete("gone")
    assert not (store.root / "gone").exists()
    assert store.names() == []


def test_delete_is_refused_while_the_profile_is_in_use(store: ProfileStore) -> None:
    with store.acquire("busy", _generate):
        with pytest.raises(ProfileBusyError):
            store.delete("busy")
    assert (store.root / "busy").exists()


def test_delete_of_a_missing_profile_is_an_error(store: ProfileStore) -> None:
    with pytest.raises(ProfileError):
        store.delete("nope")


def test_delete_does_not_follow_a_symlinked_folder(store: ProfileStore, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    store.root.mkdir(parents=True)
    (store.root / "link").symlink_to(outside)
    with pytest.raises(ProfileError):
        store.delete("link")
    assert (outside / "keep.txt").exists()


def test_timezone_is_remembered_and_a_change_is_logged(store: ProfileStore, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with store.acquire("default", _generate) as profile:
        profile.note_timezone("Europe/Moscow")
        assert store.load("default").data.timezone == "Europe/Moscow"
        with caplog.at_level(logging.WARNING):
            profile.note_timezone("Europe/Moscow")
            assert not caplog.text
            profile.note_timezone("Asia/Yekaterinburg")
        assert "changed" in caplog.text
        assert store.load("default").data.timezone == "Asia/Yekaterinburg"
        profile.note_timezone("")
        assert store.load("default").data.timezone == "Asia/Yekaterinburg"
