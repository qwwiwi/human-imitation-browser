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


def test_readme_states_allowed_use_near_the_top() -> None:
    title = "## Для чего это и для чего нет"
    assert title in README
    assert README.index(title) < README.index("## Что это и для кого") < README.index("## Схема в 5 шагов")
    section = README.split(title)[1].split("\n## ")[0]
    for phrase in (
        "закон о персональных данных",
        "вплоть до уголовной ответственности",
        "официальным API площадки",
        "Это не юридическая консультация",
        "не связан с Ozon",
    ):
        assert phrase in section
    assert not re.search(r"\bст\.|стать[яеи] \d", section)


def test_guide_has_a_short_rules_block_before_the_first_step_and_no_second_copy() -> None:
    title = "## Прежде чем начать: что можно и что нельзя"
    assert title in GUIDE
    assert GUIDE.index(title) < GUIDE.index("## Шаг 1")
    block = GUIDE.split(title)[1].split("\n## ")[0]
    assert "README.md#для-чего-это-и-для-чего-нет" in block
    assert "## Ответственность" not in GUIDE


def test_allowed_use_is_the_second_mandatory_rule_of_the_skill() -> None:
    headings = re.findall(r"^## (.+)$", SKILL, flags=re.M)
    assert headings[1] == "Allowed use (mandatory)"
    rule = SKILL.split("## Allowed use (mandatory)")[1].split("\n## ")[0]
    for phrase in (
        "personal tasks",
        "Never collect, store or output other people's personal data",
        "No bulk or scheduled collection",
        "Never try to get past a login, a paywall, a block or a captcha",
        "stop and report",
        "explicit instruction for that specific order",
        "Never enter payment details",
    ):
        assert phrase in rule


def test_no_wording_that_reads_as_evasion() -> None:
    sources = [SKILL, GUIDE, README]
    sources += [path.read_text(encoding="utf-8") for path in sorted((REPO_ROOT / "scripts").rglob("*.py"))]
    sources.append((REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8"))
    forbidden = ("невидим", "не отличат", "антидетект", "маскир", "undetect", "stealth", "evade", "evasion", "trick")
    for text in sources:
        lowered = text.lower().replace("proxy_bypass", "")  # urllib.request.proxy_bypass is a stdlib name
        for word in forbidden + ("bypass",):
            assert word not in lowered, word
    # «обход» stays only in the one line that says what the tool is not.
    assert README.lower().count("обход") == 1 and "не способ обходить блокировки" in README
    assert "обход" not in GUIDE.lower()
