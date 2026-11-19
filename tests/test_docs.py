"""The skill and the guide must carry the profile rules; no forbidden wording."""

import re

import pytest

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


# ---- «check the address first» page, the pre-flight step and the address list -----------------

import json  # noqa: E402

from hib.preflight import Before, Seen, assess, build_report  # noqa: E402

PAGE = (REPO_ROOT / "docs" / "check-address.md").read_text(encoding="utf-8")


def _section(text: str, title: str) -> str:
    """Return the text under a heading (any level) up to the next heading of the same or a higher level."""
    level = re.search(rf"^(#+) {re.escape(title)}", text, flags=re.M)
    assert level, title
    rest = text[level.end() :]
    following = re.search(rf"^#{{1,{len(level.group(1))}}} ", rest, flags=re.M)
    return rest[: following.start()] if following else rest


def _first_json_block(text: str) -> dict[str, object]:
    return json.loads(re.search(r"```json\n(.+?)\n```", text, flags=re.S).group(1))  # type: ignore[union-attr]


def test_the_address_page_has_two_parts_and_the_measured_facts_exactly() -> None:
    assert PAGE.startswith("# Сначала проверьте адрес")
    assert PAGE.index("## Проверка одной командой") < PAGE.index("## Посмотреть глазами")
    assert "scripts/preflight.py" in _section(PAGE, "Проверка одной командой")
    assert "scripts/check_proxy.py" in PAGE
    measured = _section(PAGE, "Что мы измерили сами")
    for phrase in (
        "2026-11-19",
        "по одной попытке",
        "это не статистика",
        "домашний интернет-адрес: hosting – нет, proxy – нет",
        "hosting – да, proxy – да",
        "Один сайт объявлений отказал уже на самом прокси",
        "один сайт недвижимости не загрузился за 60 секунд",
        "один маркетплейс показал проверку и домашнему адресу при первом заходе",
    ):
        assert phrase in measured, phrase
    assert re.search(r"^\|.+\|$", _section(PAGE, "Как читать результат"), flags=re.M)  # the reading table


def test_the_address_page_names_exactly_three_third_party_pages_to_open_by_hand() -> None:
    by_hand = _section(PAGE, "Посмотреть глазами")
    hosts = {match.split("/")[0] for match in re.findall(r"https?://([^\s)]+)", by_hand)}
    assert hosts == {"ipinfo.io", "ip-api.com", "browserleaks.com"}
    assert "https://ipinfo.io" in by_hand and "http://ip-api.com/json" in by_hand and "https://browserleaks.com/ip" in by_hand
    assert "могут ошибаться" in by_hand and "противоречить" in by_hand
    assert "ipapi.is" not in PAGE and "ipapi.is" not in GUIDE and "ipapi.is" not in README and "ipapi.is" not in SKILL
    flags_line = next(line for line in by_hand.splitlines() if "ip-api.com" in line and line.startswith("- "))
    assert "читает" in flags_line and "hosting" in flags_line and "proxy" in flags_line and "mobile" in flags_line
    assert "http://ip-api.com/json" in flags_line and "https" in flags_line and "откажутся" in flags_line
    owner_line = next(line for line in by_hand.splitlines() if "ipinfo.io" in line and line.startswith("- "))
    assert "владельц" in owner_line


def test_the_address_page_says_how_the_tool_reads_the_flags_and_what_the_service_terms_are() -> None:
    step = _section(PAGE, "Проверка одной командой")
    assert "запрос" in step and "из того же браузера" in step  # a request from the browser context, through the same proxy
    assert "не открывает" in step or "не переходит" in step  # not a page navigation
    assert "45" in PAGE and "некоммерческ" in PAGE and "по словам сервиса" in PAGE and "проверьте их условия" in PAGE


def test_the_address_page_documents_the_flags_address_override_and_its_fields() -> None:
    step = _section(PAGE, "Проверка одной командой")
    assert "HIB_REPUTATION_URL" in step and "http://ip-api.com/json/?fields=status,country,city,isp,as,mobile,proxy,hosting" in step
    for name in ("status", "hosting", "proxy", "mobile"):
        assert f"`{name}`" in step, name
    for name in ("is_datacenter", "is_proxy", "is_vpn", "is_tor", "is_mobile"):
        assert name not in step and name not in GUIDE and name not in README, name
    assert "HIB_REPUTATION_URL" in GUIDE and "`hosting`" in GUIDE and "`status`" in GUIDE
    assert "один раз" in step and "повтор" in step  # a slow address page is tried once more


def test_the_sample_outputs_carry_filled_flags() -> None:
    for text in (PAGE, README, GUIDE):
        sample = re.search(r'\{"ok": true, "profile": "my-shop".+\}', text)
        assert sample, text[:40]
        assert '"hosting": false' in sample.group(0) or '"hosting": true' in sample.group(0)
        assert '"proxy_flag": ' in sample.group(0) and '"mobile": ' in sample.group(0)
        assert "null" not in sample.group(0)


def test_the_reading_table_has_a_mobile_network_row() -> None:
    table = _section(PAGE, "Как читать результат")
    rows = [line for line in table.splitlines() if line.startswith("|") and "mobile" in line.split("|")[1]]
    assert len(rows) == 1  # the first cell (what is seen) names the flag
    assert "мобильного оператора" in rows[0].split("|")[2]
    assert "`mobile`" in _section(PAGE, "Проверка одной командой")


def test_check_proxy_sample_and_skill_name_the_mobile_field() -> None:
    sample = re.search(r'\{"source": "mobileproxy\.space".+\}', GUIDE)
    assert sample and '"mobile"' in sample.group(0)
    assert "`mobile`" in _section(GUIDE, "4.1. Проверьте прокси")
    assert "`mobile`" in SKILL


def test_the_address_page_keeps_the_stop_rule_and_makes_no_promise() -> None:
    assert "не обещает, что сайт не покажет проверку" in PAGE
    rule = _section(PAGE, "Правило остановки")
    assert "Проверку на сайте проходит человек" in rule and "стоп" in rule


def test_the_address_page_has_no_forbidden_wording_no_ip_and_no_legality_claim() -> None:
    lowered = PAGE.lower()
    for word in ("обход", "невидим", "не отличат", "undetect", "bypass", "stealth", "антидетект", "маскир", "evade"):
        assert word not in lowered, word
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", PAGE)
    assert "легальн" not in lowered and "законн" not in lowered
    assert "много аккаунтов" not in lowered and "много профилей" not in lowered


def test_readme_points_to_the_page_and_lists_three_kinds_of_address() -> None:
    assert "docs/check-address.md" in README
    block = _section(README, "Где взять адрес")
    assert len(re.findall(r"^- \*\*", block, flags=re.M)) == 3
    assert "https://proxyline.net?ref=158579 (партнёрская ссылка)" in block
    assert "около 2 долларов США в месяц" in block and "«hosting»" in block
    assert "docs/home-proxy.md" in block
    assert "Резидентный или мобильный" in block


def test_existing_partner_links_keep_their_label() -> None:
    assert "https://mobileproxy.space/?p=286413 (партнёрская ссылка)" in GUIDE
    assert "https://mobileproxy.space/user.html?free&p=286413 (партнёрская ссылка)" in GUIDE


def test_guide_links_the_address_page_where_the_proxy_is_introduced() -> None:
    assert "check-address.md" in _section(GUIDE, "Шаг 2. Прокси")


PREFLIGHT_STEPS = [
    pytest.param(README, "Проверка перед заходом", id="readme"),
    pytest.param(GUIDE, "4.2. Проверка перед заходом", id="guide"),
    pytest.param(PAGE, "Проверка одной командой", id="page"),
]


@pytest.mark.parametrize(("text", "title"), PREFLIGHT_STEPS)
def test_the_pre_flight_step_has_the_command_a_sample_and_the_meaning_of_every_line(text: str, title: str) -> None:
    step = _section(text, title)
    assert "scripts/preflight.py --profile" in step
    sample = _first_json_block(step)
    for field_name in sample:
        assert f"`{field_name}`" in step, field_name  # every printed field is explained in plain words
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", step)
    assert "придуманы" in step  # the sample is said to be made up, not a real run


@pytest.mark.parametrize(("text", "title"), PREFLIGHT_STEPS)
def test_the_sample_output_has_exactly_the_fields_the_command_prints(text: str, title: str) -> None:
    seen = Seen("RU", "C", "O", False, False, 1, 1, True, False, "out/x.png")
    real = set(build_report("p", seen, assess(Before(0, "", {}, None), seen)))
    assert set(_first_json_block(_section(text, title))) == real


def test_the_skill_has_the_mandatory_pre_flight_rule_next_to_the_profile_rule() -> None:
    headings = re.findall(r"^## (.+)$", SKILL, flags=re.M)
    assert headings[:3] == ["Profile rule (mandatory, read first)", "Allowed use (mandatory)", "Pre-flight (mandatory)"]
    rule = _section(SKILL, "Pre-flight (mandatory)")
    for phrase in (
        "Before the first real site in a session run the pre-flight for the profile",
        "If it fails, stop and tell the owner what changed",
        "Do not switch to another profile or proxy to get around it",
        "scripts/preflight.py --profile",
        "Never use `--accept-address-change` yourself",
    ):
        assert phrase in rule, phrase


def test_exit_code_12_is_documented_everywhere_and_8_stays_the_browser() -> None:
    assert re.search(r"^\| 12 \|", SKILL, flags=re.M) and re.search(r"^\| 12 \|", GUIDE, flags=re.M)
    assert re.search(r"^\| 8 \| browser did not start", SKILL, flags=re.M)
    errors = (REPO_ROOT / "scripts" / "hib" / "errors.py").read_text(encoding="utf-8")
    assert "12 pre-flight failed" in errors
