---
name: human-imitation-browser
description: For the owner's own personal tasks, open a web page (Russian sites such as Ozon, Avito, Cian, Aviasales and similar) in a real browser through a Russian residential proxy, always as one returning visitor (the same named browser profile, fingerprint and proxy session), with human-like pacing; get back status, title, text size and a screenshot, and hand any captcha or anti-bot page to the person instead of solving it. Use when the person asks to look at, check or screenshot a page that does not open from the server directly. Never for collecting data about other people.
---

# human-imitation-browser

A plain-files skill: this text plus a few command-line scripts. It is meant for any agent that can run shell commands on the server: Claude Code, Codex, Hermes, OpenClaw-style agents and similar. No special integration (not tested with each agent separately).

The project lives in `~/human-imitation-browser` (if the person installed it elsewhere, use that path). Run every command from that folder.

## Profile rule (mandatory, read first)

**Never open a site as a new visitor. Always use the same named profile for the same site or task.**

- Pass the same `--profile NAME` every time for the same site or task. Without the flag the profile `default` is used; for a separate task or a separate owner pick a name (a-z, 0-9, `_`, `-`, up to 32 characters) and keep it.
- **Never delete or recreate a profile to get past a challenge or a block.** A new empty profile is a new stranger and makes it worse.
- **One profile per owner and task.** Never create several profiles for one site to look like different people. There is no tool for creating profiles in bulk or rotating between them, and you must not make one by hand.
- After a person has passed a check in the profile, keep using that profile.
- `--ephemeral` (a throw-away browser, a new visitor every time) is only for a neutral check page: example.com, ipinfo.io or ip-api.com. Any other address with it is refused with exit 4. Never use it for a real site.

Why: a site remembers a returning browser (cookies, cache, history, the same fingerprint and the same proxy exit). A fresh browser on every visit is what triggered a check in our own test: the sixth visit of the day to Ozon, each with a new browser, got a slider captcha.

The profile folder (`~/.local/share/hib/profiles/NAME/`) is effectively a key to the session and to any account logged in there. Never print, send, copy, commit or open its files. Delete a profile (`scripts/profiles.py delete NAME`) only when the person asks.

## Allowed use (mandatory)

This tool lets you use an ordinary browser on the owner's behalf, the way they would themselves, for their own personal tasks: find and compare a product, look at prices and listings, check a ticket price, put together a basket or place an order in the owner's own account when they ask.

- **Personal tasks of the owner only.** Not for other people's tasks, not for a business, not for monitoring competitors.
- **Never collect, store or output other people's personal data**: names, phone numbers, contacts, profiles of sellers, landlords, reviewers or any other users. If a page shows such data, do not copy it into your results, notes or files, and do not hand it to the owner either: describe the listing without the person's details and let the owner open the page themselves. In Russia personal data is regulated by law, and collecting it without a legal basis is an offence.
- **No bulk or scheduled collection.** Do not loop over listings, search results or catalogues, do not run on a schedule, do not build datasets or tables of listings. Open only the pages the owner asked for.
- **Never try to get past a login, a paywall, a block or a captcha**: not by program, not through solver services, not by changing the proxy, the profile or the browser.
- **When the site refuses, stop and report.** A block page, a check, a captcha or a login wall ends the work on that site for this task; tell the owner what you saw (see the captcha rule below).
- **Orders and payments only on the owner's explicit instruction for that specific order.** An instruction for one order does not cover the next one.
- **Never enter payment details** (card number, security code, confirmation codes) yourself unless the owner has explicitly set that up.
- No reviews, ratings, messages or any other activity meant to influence the site; one account and one profile per owner.
- For the owner's own seller cabinet, suggest the marketplace's official seller API instead of the browser.

## Pre-flight (mandatory)

**Before the first real site in a session run the pre-flight for the profile. If it fails, stop and tell the owner what changed. Do not switch to another profile or proxy to get around it.**

```bash
.venv/bin/python scripts/preflight.py --profile my-shop
```

- It opens a neutral address-check page in the same profile and through the same proxy the real visit will use, never a real site, and prints one JSON line: `ok`, `proxy_works`, `country`, `city`, `network_owner`, `hosting`, `proxy_flag`, `mobile` (true, false or null when unknown; `mobile` true means a mobile operator's address and never fails the check), `address_changed`, `profile_memory` (`new`, `ok` or `empty`), `problems`, `problems_soft`, `screenshot`. It never prints an IP address, cookie names or values.
- Exit 0: go on. Exit 12: stop (the address changed since the profile's last pre-flight, the memory of a used profile is empty, or with `--strict` the address is flagged as hosting or proxy). Tell the owner the `problems` and, for a change, `address_change` (old and new country, city, owner). A proxy failure keeps its own code (5, 6). If the check page itself shows a refusal or a challenge, the pre-flight stops with exit 3 and is not tried again: show it to the owner.
- `problems_soft` (for example a hosting flag) is a warning: tell the owner, and go on unless the owner said `--strict`.
- `open_page.py` refuses a real address with exit 4 when the profile has no successful pre-flight in the last 12 hours (`HIB_PREFLIGHT_MAX_AGE_H`). Run the pre-flight; do not edit the settings or the profile to silence the refusal. The record belongs to one profile and one proxy configuration (a hash, no login is stored): after the proxy settings change, or after any pre-flight that did not pass, it no longer admits until a pre-flight passes again. With a provider session it is valid for at most 2 hours.
- Never use `--accept-address-change` yourself: only the owner decides that a new address is the profile's normal one.
- A clean pre-flight does not promise that a site will not show a check. A check on a site is passed by a person (captcha rule below); a refusal is a stop.

Why: sites judge the address before the browser, and a profile whose address or memory silently changed is a different visitor. Details for the owner: `docs/check-address.md`.

## When to use

- The person asks you, for their own task, to open, look at, check or screenshot a page on a site that does not open from a server address (Ozon, Avito, Cian, Aviasales and similar Russian services).
- Stop on the first refusal. If a normal fetch from the server already got an access-denied page (for example 403 from a marketplace), a block or a captcha, that is the site's refusal: stop and report it to the owner. Never retry the same site through this tool, by another route or with another profile or proxy (see Allowed use). The tool does the same on its own: a page with a challenge or an explicit denial ends the run with exit code 3, and nothing is retried.

Do not open many pages "just in case" or anything the person did not ask for.

## How to call

```bash
cd ~/human-imitation-browser
.venv/bin/python scripts/open_page.py --profile my-shop https://www.ozon.ru/
```

Several pages in one browser session, opened one after another with pauses:

```bash
.venv/bin/python scripts/open_page.py --profile my-shop https://www.cian.ru/ https://www.avito.ru/
```

Options:

- `--profile NAME` – the profile to use (default `default`). It is created on first use.
- `--no-images` – do not load images to save proxy traffic. Camoufox warns this can raise suspicion on some sites. Never change options or profiles to get past a challenge.
- `--out DIR` – where screenshots go (default `out/`).
- `--timeout SECONDS` – page load timeout (default 60).
- `--pause-min`, `--pause-max` – pause between pages in seconds (default 20 and 40). Do not lower them.
- `--ephemeral`, `--no-proxy` – see the profile rule above; `--no-proxy` works only with `--ephemeral`.

The browser takes about 10 seconds to start. A run with several pages takes minutes because of the pauses; that is intended. Inside one profile the script also keeps at least 20 seconds between navigations to the same host, even across runs, and sleeps the remainder itself.

Profile commands:

```bash
.venv/bin/python scripts/profiles.py list
.venv/bin/python scripts/profiles.py show my-shop
```

To check the proxy without a browser: `.venv/bin/python scripts/check_proxy.py` (prints country, city, provider, the address reputation and `traffic_left_mb`).

## Output

stdout carries one JSON line per page:

```json
{"url": "...", "status": 200, "title": "...", "text_chars": 5120, "screenshot": "out/....png", "challenge": false, "reason": "", "profile": "my-shop", "visits": 3}
```

On an error, one line `{"error": "...", "exit_code": N}`. Diagnostics go to stderr. `screenshot` is empty if the screenshot could not be taken. `visits` is how many runs this profile has made.

For a `socks5://` proxy `check_proxy.py` cannot check the exit address: it prints `{"source", "checked": false, "note"}`; use `scripts/preflight.py --profile <name>` instead. Otherwise `check_proxy.py` prints `{"source", "country", "city", "org", "network_owner", "ru", "traffic_left_mb", "hosting", "proxy_flag", "mobile"}`; if `ru` is false, tell the person the exit address is not in Russia. `hosting`, `proxy_flag` and `mobile` are a third-party reputation service's opinion (`null` when it did not answer) and can be wrong.

Judge the result by the screenshot and `challenge`, not by `status` alone: some sites answer 403 and still render the full page.

## Exit codes

| Code | Meaning | What you do |
|---|---|---|
| 0 | page opened | report the result; send the screenshot if useful |
| 1 | unexpected error | report the `error` text to the person |
| 2 | wrong arguments | fix the command |
| 3 | challenge / captcha page, or an explicit access-denied page | follow the captcha rule below |
| 4 | no token or bad `HIB_PROXY_URL`, or no recent pre-flight for the profile | ask the person to fill in `.env` themselves; for a missing pre-flight run the command named in `error` |
| 5 | proxy provider API error | tell the person: check token, balance, package |
| 6 | proxy unreachable or rejected the login | tell the person: check package and traffic left |
| 7 | page load timeout | you may retry once later, after a pause of a few minutes |
| 8 | browser did not start | tell the person to rerun `bash scripts/install.sh` |
| 9 | the site answered a server error (5xx) | not a block and not a captcha: wait a few minutes, retry once, then report |
| 10 | the profile is in use by another run | wait until that run ends, then retry once; never start a second run on the same profile, never work around the lock |
| 11 | profile problem (missing, damaged, cannot be created) | report the `error` text; do not delete or recreate the profile yourself, ask the person |
| 12 | pre-flight failed (`preflight.py` only) | stop; tell the owner `problems` and `address_change`; do not switch profile or proxy |

## Captcha rule (mandatory)

When the exit code is 3 or `challenge` is `true`:

1. Stop. Do not open more pages on that site in this task. The profile stays as it is.
2. Send the screenshot file from the `screenshot` field to the person in your dialog, with one line: which page, what you were asked to do.
3. Wait for the person's decision. They solve it themselves, tell you to try later, or drop the task.

Never:

- solve a captcha yourself, by any means;
- use captcha-solving services;
- try to get around the check: no proxy or IP changes, no browser restarts, no new or different profile, no repeated attempts, no other tools to reach the same page.

A live hand-off (the person solving the captcha from a phone in the same browser) is not built yet. Do not promise it.

## Pacing rules

- One page at a time. Never run several copies of the script at once (a second run on the same profile is refused with exit code 10).
- Put related pages into one call so they share one session; keep the default pauses.
- If a site starts showing challenges, stop working with it for this task.

## Secrets and accounts

- Never print, log, quote or send the proxy login, password, `MOBILEPROXY_API_TOKEN`, `HIB_PROXY_URL` or the contents of `.env`. Do not open `.env` to show it. The scripts never print these; keep it that way.
- Never print, send or open the files inside a profile folder (cookies, fingerprint, proxy cache).
- If the person pastes a token into the chat, tell them to put it into `.env` themselves and to replace the token in the provider's cabinet, since the chat is not a safe place for it.
- Never log into any account, fill in forms, write messages or click "confirm" on a site without the person's explicit instruction for that exact action (orders and payments: see Allowed use).
- Use only the person's own accounts, and only when they ask.
