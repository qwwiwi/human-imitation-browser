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


def detect_challenge(title: str, text: str, min_chars: int = MIN_TEXT_CHARS) -> ChallengeCheck:
    """Decide whether a loaded page is a challenge page.

    The HTTP status is deliberately not used: some sites answer 403 or another
    non-200 code and still render the full page.

    Args:
        title: Page title.
        text: Visible body text (whitespace already collapsed or not).
        min_chars: Body texts shorter than this count as a challenge.

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
    if len(compact) < min_chars:
        return ChallengeCheck(True, f"very short page text ({len(compact)} chars)")
    return ChallengeCheck(False)
