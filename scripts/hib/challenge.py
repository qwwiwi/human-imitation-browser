"""Recognise anti-bot challenge pages by generic, site-independent signs.

The check never tries to solve a challenge or get past it: it only tells the caller
to stop and hand the page to a person.
"""

from dataclasses import dataclass

# Titles and phrases seen on challenge / block pages. Compared case-insensitively.
CHALLENGE_MARKERS: tuple[str, ...] = (
    "antibot challenge page",
    "antibot captcha",
    "похоже, нет соединения",
)

# Explicit wording of challenge and access-denied pages. Matched in the title only, and only in a
# short title (an article about security checks has a long one). Compared case-insensitively.
TITLE_PHRASES: tuple[str, ...] = (
    "access denied",
    "security check",
    "just a moment",
    "attention required",
    "verify you are human",
    "are you a robot",
    "are you human",
    "request blocked",
    "403 forbidden",
    "доступ ограничен",
    "доступ запрещ",
    "вы не робот",
    "подтвердите, что вы",
    "проверка безопасности",
)
MAX_PHRASE_TITLE_CHARS = 60

# Wording that marks a challenge when it is (nearly) all the page says. Matched in the body only
# when the body is short, so an article that merely talks about captchas is not flagged.
BODY_PHRASES: tuple[str, ...] = (
    "verify you are human",
    "verify that you are human",
    "confirm you are not a robot",
    "unusual traffic",
    "you have been blocked",
    "access denied",
    "captcha",
    "подтвердите, что вы не робот",
    "подозрительная активность",
    "подозрительный трафик",
    "доступ ограничен",
    "вы не робот",
)
SHORT_BODY_CHARS = 600

# HTTP answers that refuse or throttle the visitor; with a short page they count as a denial.
DENIAL_STATUSES = frozenset({401, 403, 429})

# A real page has more visible text than this; a stub or challenge page often does not.
MIN_TEXT_CHARS = 100


@dataclass(frozen=True)
class ChallengeCheck:
    """Result of a challenge check.

    Attributes:
        is_challenge: True when the page looks like a challenge or block page.
        reason: Short English explanation, empty when not a challenge.
    """

    is_challenge: bool
    reason: str = ""


def detect_challenge(title: str, text: str, min_chars: int = MIN_TEXT_CHARS, status: int | None = None) -> ChallengeCheck:
    """Decide whether a loaded page is a challenge page or an explicit access denial.

    A rich-text page is not flagged by the HTTP status alone: some sites answer 403 and still
    render the full page. The rules, in order:

    1. a named marker in the title or the text;
    2. an explicit denial or challenge phrase in a short title;
    3. a challenge phrase in a short body (fewer than SHORT_BODY_CHARS characters);
    4. HTTP 401, 403 or 429 with a short body;
    5. a body shorter than `min_chars`.

    Rules 4 and 5 are meant for a page that has finished rendering; with `min_chars=0` they are
    skipped, so the caller can use that for a first look at a page that may still be loading.

    Args:
        title: Page title.
        text: Visible body text (whitespace already collapsed or not).
        min_chars: Body texts shorter than this count as a challenge; 0 turns rules 4 and 5 off.
        status: HTTP status of the main document, if known.

    Returns:
        A ChallengeCheck with the verdict and the reason.
    """
    title_low = title.strip().lower()
    for marker in CHALLENGE_MARKERS:
        if marker in title_low:
            return ChallengeCheck(True, f"title contains '{marker}'")
    compact = " ".join(text.split())
    text_low = compact.lower()
    for marker in CHALLENGE_MARKERS:
        if marker in text_low:
            return ChallengeCheck(True, f"text contains '{marker}'")
    if len(title_low) <= MAX_PHRASE_TITLE_CHARS:
        for phrase in TITLE_PHRASES:
            if phrase in title_low:
                return ChallengeCheck(True, f"title says '{phrase}'")
    if len(compact) < SHORT_BODY_CHARS:
        for phrase in BODY_PHRASES:
            if phrase in text_low:
                return ChallengeCheck(True, f"short page says '{phrase}'")
    if min_chars > 0:
        if status in DENIAL_STATUSES and len(compact) < SHORT_BODY_CHARS:
            return ChallengeCheck(True, f"HTTP {status} with a short page ({len(compact)} chars)")
        if len(compact) < min_chars:
            return ChallengeCheck(True, f"very short page text ({len(compact)} chars)")
    return ChallengeCheck(False)
