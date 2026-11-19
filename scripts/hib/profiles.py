"""Persistent browser profiles: one profile is one returning visitor.

A profile is a folder outside the repository:

    <root>/<name>/
        profile.json       the choices made once (see ProfileData); no secrets
        fingerprint.json   the browser fingerprint generated once and reused
        proxy-cache.json   the proxy answer kept for a few minutes (has the proxy login)
        user-data/         the browser's own memory: cookies, cache, history

The run that uses a profile holds a lock file outside this folder (`<root>/.locks/<name>.lock`),
so deleting the folder never removes the lock. The folder is the key to the session and to every account logged in there:
it stays private (directories 0700, files 0600) and never goes into git.
"""

import fcntl
import json
import logging
import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from hib.env import REPO_ROOT
from hib.errors import ConfigError, ProfileBusyError, ProfileError

logger = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
DEFAULT_PROFILE = "default"
DEFAULT_LOCALE = "ru-RU"
PROFILE_FILE = "profile.json"
FINGERPRINT_FILE = "fingerprint.json"
LOCKS_DIR = ".locks"
USER_DATA_DIR = "user-data"
SCHEMA_VERSION = 1


def validate_profile_name(name: str) -> str:
    """Check a profile name: 1-32 characters from a-z, 0-9, `_`, `-`, not starting with `_` or `-`.

    Args:
        name: The name to check.

    Returns:
        The same name.

    Raises:
        ProfileError: The name is not allowed (this also keeps `..` and `/` out).
    """
    if not NAME_PATTERN.fullmatch(name):
        raise ProfileError("profile name must be 1-32 characters: a-z, 0-9, _ and -, starting with a letter or a digit")
    return name


def profiles_root(env_value: str | None, home: Path | None = None) -> Path:
    """Return the folder that holds all profiles.

    Args:
        env_value: Value of HIB_PROFILES_DIR, or None for the default.
        home: Home folder (replaceable in tests).

    Returns:
        `~/.local/share/hib/profiles` or the override.

    Raises:
        ConfigError: The folder would be inside the repository (profile data must never be committed).
    """
    if env_value and env_value.strip():
        root = Path(env_value.strip()).expanduser()
    else:
        root = (home or Path.home()) / ".local" / "share" / "hib" / "profiles"
    if root.resolve().is_relative_to(REPO_ROOT):
        raise ConfigError("HIB_PROFILES_DIR is inside the project folder: profile data must stay outside the repository")
    return root


def _utc_now() -> str:
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_private(path: Path, text: str) -> None:
    """Write a file readable by the owner only, replacing it atomically.

    The text goes into a new, exclusively created 0600 file next to the target (a planted
    file or symlink under the temporary name is never opened), then replaces the target.

    Args:
        path: Target file.
        text: Content (UTF-8).
    """
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _private_dir(path: Path) -> None:
    """Create a folder (and missing parents) and make the folder itself owner-only."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


@dataclass
class ProfileData:
    """The choices stored in profile.json.

    Attributes:
        name: Profile name.
        created: Creation time (UTC, ISO 8601).
        last_used: Time of the last run that started the browser, empty if none.
        visits: How many runs started the browser with this profile.
        os: Operating system the fingerprint claims.
        screen: Screen size the fingerprint claims, `{"width", "height"}` or empty.
        locale: Browser language.
        timezone: Time zone seen in the browser on the last run, empty if unknown.
        proxy_session: Id of the sticky proxy session (not shown anywhere).
        last_exit: Country and city the proxy settings pointed to on the last run.
        last_navigation: Host -> time (seconds since epoch) of the last navigation.
    """

    name: str
    created: str
    proxy_session: str = field(repr=False)
    last_used: str = ""
    visits: int = 0
    os: str = ""
    screen: dict[str, int] = field(default_factory=dict)
    locale: str = DEFAULT_LOCALE
    timezone: str = ""
    last_exit: dict[str, str] = field(default_factory=dict)
    last_navigation: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialize to JSON text."""
        return json.dumps({"schema": SCHEMA_VERSION, **self.__dict__}, ensure_ascii=False, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "ProfileData":
        """Parse profile.json text.

        Raises:
            ValueError: The text is not valid profile data.
        """
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        name, created, session, visits = raw.get("name"), raw.get("created"), raw.get("proxy_session"), raw.get("visits", 0)
        if not isinstance(name, str) or not isinstance(created, str) or not isinstance(session, str) or not session:
            raise ValueError("missing fields")
        if not isinstance(visits, int) or isinstance(visits, bool) or visits < 0:
            raise ValueError("bad visits")
        navigation = raw.get("last_navigation", {})
        if not isinstance(navigation, dict) or not all(isinstance(v, (int, float)) for v in navigation.values()):
            raise ValueError("bad last_navigation")
        return cls(
            name=name,
            created=created,
            proxy_session=session,
            last_used=str(raw.get("last_used", "")),
            visits=visits,
            os=str(raw.get("os", "")),
            screen={str(k): int(v) for k, v in dict(raw.get("screen", {})).items()},
            locale=str(raw.get("locale", DEFAULT_LOCALE)),
            timezone=str(raw.get("timezone", "")),
            last_exit={str(k): str(v) for k, v in dict(raw.get("last_exit", {})).items()},
            last_navigation={str(k): float(v) for k, v in navigation.items()},
        )


@dataclass
class Profile:
    """An opened profile.

    Attributes:
        path: Profile folder.
        data: The stored choices.
        fingerprint: The browser fingerprint generated at creation (hidden in repr).
    """

    path: Path
    data: ProfileData
    fingerprint: dict[str, object] = field(repr=False)

    @property
    def name(self) -> str:
        """Profile name."""
        return self.data.name

    @property
    def user_data_dir(self) -> Path:
        """The browser's own folder (cookies, cache, history)."""
        return self.path / USER_DATA_DIR

    @property
    def proxy_cache_path(self) -> Path:
        """Where the short-lived proxy answer is kept."""
        return self.path / "proxy-cache.json"

    def save(self) -> None:
        """Write profile.json."""
        try:
            write_private(self.path / PROFILE_FILE, self.data.to_json())
        except OSError as exc:
            raise ProfileError(f"cannot save profile {self.name}: {type(exc).__name__}") from None

    def record_visit(self) -> None:
        """Count one more run that started the browser and remember when."""
        self.data.visits += 1
        self.data.last_used = _utc_now()
        self.save()

    def note_timezone(self, timezone_name: str) -> None:
        """Remember the time zone the browser showed; log when it differs from the last run.

        Args:
            timezone_name: IANA zone name read from the browser; empty means unknown and is ignored.
        """
        if not timezone_name or timezone_name == self.data.timezone:
            return
        if self.data.timezone:
            logger.warning(
                "the browser time zone changed from %s to %s (the proxy exit moved); the site may notice",
                self.data.timezone,
                timezone_name,
            )
        self.data.timezone = timezone_name
        self.save()

    def describe(self) -> dict[str, object]:
        """Return what `profiles.py show` prints: no proxy session id, no fingerprint body, no secrets."""
        d = self.data
        return {
            "name": d.name,
            "path": str(self.path),
            "created": d.created,
            "last_used": d.last_used,
            "visits": d.visits,
            "os": d.os,
            "screen": d.screen,
            "locale": d.locale,
            "timezone": d.timezone,
            "last_exit": d.last_exit,
        }


class ProfileStore:
    """All profiles under one root folder."""

    def __init__(self, root: Path) -> None:
        """Create a store.

        Args:
            root: Folder that holds the profile folders; created on first use.
        """
        self.root = root

    def _dir(self, name: str) -> Path:
        """Return the folder of a profile after checking that it really is a child of the root.

        Raises:
            ProfileError: The name is bad, the folder is a symlink, or it resolves outside the root
                or onto the project folder (or a parent of it).
        """
        path = self.root / validate_profile_name(name)
        if path.is_symlink():
            raise ProfileError(f"profile {name} is not a folder of this tool")
        resolved = path.resolve()
        if (
            resolved.parent != self.root.resolve()
            or resolved.is_relative_to(REPO_ROOT)
            or REPO_ROOT.is_relative_to(resolved)
        ):
            raise ProfileError(f"profile {name} is not a folder of this tool")
        return path

    def _lock_path(self, name: str) -> Path:
        """Where the lock of a profile lives: outside the profile folder, so deleting it keeps the lock."""
        return self.root / LOCKS_DIR / f"{name}.lock"

    def names(self) -> list[str]:
        """Return the names of complete profiles, sorted."""
        if not self.root.is_dir():
            return []
        return sorted(
            p.name
            for p in self.root.iterdir()
            if NAME_PATTERN.fullmatch(p.name) and not p.is_symlink() and p.is_dir() and (p / PROFILE_FILE).is_file()
        )

    def load(self, name: str) -> Profile:
        """Read a profile without locking it (for `list` and `show`).

        Raises:
            ProfileError: The profile does not exist or is damaged.
        """
        path = self._dir(name)
        if not (path / PROFILE_FILE).is_file():
            raise ProfileError(f"profile {name} does not exist")
        return self._read(path)

    def _read(self, path: Path) -> Profile:
        try:
            data = ProfileData.from_json((path / PROFILE_FILE).read_text(encoding="utf-8"))
            fingerprint = json.loads((path / FINGERPRINT_FILE).read_text(encoding="utf-8"))
            if not isinstance(fingerprint, dict):
                raise ValueError("fingerprint is not an object")
        except (OSError, ValueError, TypeError) as exc:
            raise ProfileError(
                f"profile {path.name} is damaged ({type(exc).__name__}); "
                "restore it from a backup or delete it with scripts/profiles.py (the site will then see a new visitor)"
            ) from None
        return Profile(path=path, data=data, fingerprint=fingerprint)

    def _create(self, path: Path, new_fingerprint: Callable[[], dict[str, object]]) -> Profile:
        fingerprint = new_fingerprint()
        screen = fingerprint.get("screen")
        size = {}
        if isinstance(screen, dict):
            size = {k: int(screen[k]) for k in ("width", "height") if isinstance(screen.get(k), int)}
        data = ProfileData(
            name=path.name,
            created=_utc_now(),
            proxy_session=secrets.token_hex(5),
            os=str(fingerprint.get("os") or ""),
            screen=size,
        )
        try:
            _private_dir(path / USER_DATA_DIR)
            write_private(path / FINGERPRINT_FILE, json.dumps(fingerprint, ensure_ascii=False))
            profile = Profile(path=path, data=data, fingerprint=fingerprint)
            profile.save()  # last: its presence marks a complete profile
        except OSError as exc:
            raise ProfileError(f"cannot create profile {path.name}: {type(exc).__name__}") from None
        logger.info("created profile %s", path.name)
        return profile

    @contextmanager
    def acquire(self, name: str, new_fingerprint: Callable[[], dict[str, object]]) -> Iterator[Profile]:
        """Lock a profile for one run, creating it on first use.

        Args:
            name: Profile name.
            new_fingerprint: Called only when the profile is created, to draw its fingerprint once.

        Yields:
            The profile; the lock is held until the block ends (also on errors or a crash).

        Raises:
            ProfileBusyError: Another run holds the profile.
            ProfileError: The profile cannot be created or is damaged.
        """
        self._dir(name)  # refuse a bad folder before anything is created
        fd = self._lock(name)
        try:
            path = self._dir(name)  # checked again under the lock
            try:
                _private_dir(path)
            except OSError as exc:
                raise ProfileError(f"cannot open profile {name}: {type(exc).__name__}") from None
            if not path.is_dir():
                raise ProfileError(f"profile {name} is not a folder of this tool")
            if (path / PROFILE_FILE).is_file():
                profile = self._read(path)
            else:
                profile = self._create(path, new_fingerprint)
            yield profile
        finally:
            os.close(fd)

    def _lock(self, name: str) -> int:
        """Take the lock of a profile without waiting; return the open descriptor.

        The lock file lives in `<root>/.locks/` and is never deleted, so every run, create and
        delete of one name meets the same inode. Take it before touching the profile folder.
        """
        lock_path = self._lock_path(name)
        try:
            _private_dir(self.root)
            _private_dir(lock_path.parent)
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise ProfileError(f"cannot open profile {name}: {type(exc).__name__}") from None
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ProfileBusyError(
                f"profile {name} is in use by another run: wait until it ends (two runs never share one profile)"
            ) from None
        except OSError as exc:
            os.close(fd)
            raise ProfileError(f"cannot lock profile {name}: {type(exc).__name__}") from None
        return fd

    def delete(self, name: str) -> None:
        """Delete a profile folder with everything in it.

        Only a folder that carries a profile file (profile.json or fingerprint.json) is removed.

        Raises:
            ProfileError: The profile does not exist, is not a profile folder or cannot be removed.
            ProfileBusyError: A run is using it.
        """
        path = self._dir(name)
        if not path.exists():
            raise ProfileError(f"profile {name} does not exist")
        fd = self._lock(name)
        try:
            path = self._dir(name)  # checked again under the lock
            if not path.is_dir() or not any((path / marker).is_file() for marker in (PROFILE_FILE, FINGERPRINT_FILE)):
                raise ProfileError(f"{name} is not a profile folder of this tool: nothing was deleted")
            shutil.rmtree(path)
        except OSError as exc:
            raise ProfileError(f"cannot delete profile {name}: {type(exc).__name__}") from None
        finally:
            os.close(fd)
        logger.info("deleted profile %s", name)
