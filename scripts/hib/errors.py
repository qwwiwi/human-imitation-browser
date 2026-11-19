"""Error types. Each one maps to its own process exit code.

Exit codes (also documented in README and skill/SKILL.md):
    0  page opened, no challenge seen
    1  unexpected error
    2  wrong command-line arguments (argparse)
    3  challenge page (captcha / anti-bot) – a person must decide
    4  configuration missing or invalid (no token, bad HIB_PROXY_URL)
    5  proxy provider API error
    6  proxy unreachable or rejected the login
    7  page load timeout
    8  browser failed to start
    9  the site answered a server error (HTTP 5xx) and the page is not a challenge page
    10 the profile is in use by another run (two runs never share one profile)
    11 profile problem (not found, damaged, cannot be created or deleted)
"""

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_CHALLENGE = 3


class HibError(Exception):
    """Base error with a user-facing message and an exit code."""

    exit_code: int = EXIT_UNEXPECTED


class ConfigError(HibError):
    """Token or proxy settings are missing or malformed."""

    exit_code = 4


class ProxyApiError(HibError):
    """The proxy provider API failed or returned something unexpected."""

    exit_code = 5


class ProxyAuthError(ProxyApiError):
    """The proxy provider API refused the token (HTTP 401 or 403): the account settings must be fixed."""


class ProxyUnreachableError(HibError):
    """The proxy did not accept the connection or the login."""

    exit_code = 6


class PageTimeoutError(HibError):
    """The page did not load in time."""

    exit_code = 7


class BrowserStartError(HibError):
    """Camoufox could not start (missing system libraries, no browser files)."""

    exit_code = 8


class PageServerError(HibError):
    """The site answered with a server error (HTTP 5xx) and the page is not a challenge page."""

    exit_code = 9


class ProfileBusyError(HibError):
    """Another run is using this profile right now."""

    exit_code = 10


class ProfileError(HibError):
    """A profile is missing, damaged or cannot be created, read or deleted."""

    exit_code = 11
