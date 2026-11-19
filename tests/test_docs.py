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


# ---- review blockers H9 and H10 -------------------------------------------------------------

HOME_PROXY = (REPO_ROOT / "docs" / "home-proxy.md").read_text(encoding="utf-8")


def test_h9_firewall_step_finds_the_real_ssh_port_and_tests_a_second_connection_before_the_end() -> None:
    step = HOME_PROXY.split("## Шаг 7.")[1].split("\n## ")[0]
    assert "SSH_CONNECTION" in step  # the port of the session in use
    assert "<ПОРТ_SSH>" in step  # the allow rule takes the real port, not a fixed 22
    assert "port 22 proto" not in step
    assert step.index("<ПОРТ_SSH>") < step.index("sudo ufw enable")  # allowed first
    assert "не закрывайте" in step.lower()  # the current session stays open
    after_enable = step.split("sudo ufw enable")[1]
    assert "второе" in after_enable.lower() and "подключение" in after_enable.lower()
    assert "консол" in step.lower()  # a way back if the test fails


def test_h9_policy_warning_and_backup_come_before_the_policy_is_replaced() -> None:
    step = HOME_PROXY.split("## Шаг 2.")[1].split("\n## ")[0]
    replace_at = step.index("Замените содержимое")
    warning_at = step.index("заменяет все правила")
    backup_at = step.index("резервную копию")
    assert warning_at < replace_at and backup_at < replace_at


def test_h10_a_refusal_on_the_first_route_is_a_stop_not_a_retry_through_the_tool() -> None:
    when = SKILL.split("## When to use")[1].split("## How to call")[0]
    assert "If the same site also refuses through this tool" not in when
    assert "never retry" in when.lower() or "do not retry" in when.lower()
    assert "stop" in when.lower()


def test_h10_skill_has_no_exception_for_handing_over_a_sellers_contact() -> None:
    rule = SKILL.split("## Allowed use")[1].split("## When to use")[0]
    assert "The one exception" not in rule
    assert "contact of one specific seller" not in rule
    assert "Never collect, store or output other people's personal data" in rule


def test_h10_no_advice_to_drop_an_option_when_challenges_appear() -> None:
    assert "drop it if challenges appear" not in SKILL
