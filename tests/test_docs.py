"""The skill and the guide must carry the profile rules; no forbidden wording."""

import re

from hib.env import REPO_ROOT

SKILL = (REPO_ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8")
GUIDE = (REPO_ROOT / "docs" / "guide.md").read_text(encoding="utf-8")
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def test_profile_rule_is_the_first_rule_of_the_skill_and_marked_mandatory() -> None:
    first_heading = re.findall(r"^## (.+)$", SKILL, flags=re.M)[0]
    assert "Profile rule" in first_heading and "mandatory" in first_heading
    rule = SKILL.split("## When to use")[0]
    for phrase in (
        "Never open a site as a new visitor",
        "Never delete or recreate a profile to get past a challenge or a block",
        "One profile per owner and task",
        "After a person has passed a check in the profile, keep using that profile",
    ):
        assert phrase in rule


def test_guide_and_readme_state_one_profile_one_owner() -> None:
    assert "Один профиль – один настоящий владелец" in GUIDE
    assert "Один профиль – один настоящий владелец" in README
    assert "фактически ключ" in GUIDE and "фактически ключ" in README


def test_no_wording_that_promises_to_deceive_a_site() -> None:
    for text in (SKILL, GUIDE, README):
        lowered = text.lower()
        assert "undetectable" not in lowered
        assert "deceive" not in lowered
        assert "чтобы сайт не понял" not in lowered
