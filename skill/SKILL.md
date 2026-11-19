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
- `--ephemeral` (a throw-away browser, a new visitor every time) is only for a neutral test page such as https://example.com. Never use it for a real site.

Why: a site remembers a returning browser (cookies, cache, history, the same fingerprint and the same proxy exit). A fresh browser on every visit is what triggered a check in our own test: the sixth visit of the day to Ozon, each with a new browser, got a slider captcha.

The profile folder (`~/.local/share/hib/profiles/NAME/`) is effectively a key to the session and to any account logged in there. Never print, send, copy, commit or open its files. Delete a profile (`scripts/profiles.py delete NAME`) only when the person asks.

## Allowed use (mandatory)

This tool lets you use an ordinary browser on the owner's behalf, the way they would themselves, for their own personal tasks: find and compare a product, look at prices and listings, check a ticket price, put together a basket or place an order in the owner's own account when they ask.

- **Personal tasks of the owner only.** Not for other people's tasks, not for a business, not for monitoring competitors.
- **Never collect, store or output other people's personal data**: names, phone numbers, contacts, profiles of sellers, landlords, reviewers or any other users. If a page shows such data, do not copy it into your results, notes or files. The one exception: the owner needs the contact of one specific seller for their own purchase; then give only that contact, only to the owner. In Russia personal data is regulated by law, and collecting it without a legal basis is an offence.
- **No bulk or scheduled collection.** Do not loop over listings, search results or catalogues, do not run on a schedule, do not build datasets or tables of listings. Open only the pages the owner asked for.
- **Never try to get past a login, a paywall, a block or a captcha**: not by program, not through solver services, not by changing the proxy, the profile or the browser.
- **When the site refuses, stop and report.** A block page, a check, a captcha or a login wall ends the work on that site for this task; tell the owner what you saw (see the captcha rule below).
- **Orders and payments only on the owner's explicit instruction for that specific order.** An instruction for one order does not cover the next one.
- **Never enter payment details** (card number, security code, confirmation codes) yourself unless the owner has explicitly set that up.
- No reviews, ratings, messages or any other activity meant to influence the site; one account and one profile per owner.
- For the owner's own seller cabinet, suggest the marketplace's official seller API instead of the browser.

## When to use

- The person asks you, for their own task, to open, look at, check or screenshot a page on a site that does not open from a server address (Ozon, Avito, Cian, Aviasales and similar Russian services).
- A normal fetch from the server gets an error page (for example 403 from a marketplace). If the same site also refuses through this tool, stop (see Allowed use).

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
- `--no-images` – do not load images to save proxy traffic. Camoufox warns this can raise suspicion on some sites; drop it if challenges appear.
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

To check the proxy without a browser: `.venv/bin/python scripts/check_proxy.py` (prints country, city, provider and `traffic_left_mb`).

## Output

stdout carries one JSON line per page:

```json
{"url": "...", "status": 200, "title": "...", "text_chars": 5120, "screenshot": "out/....png", "challenge": false, "reason": "", "profile": "my-shop", "visits": 3}
```

On an error, one line `{"error": "...", "exit_code": N}`. Diagnostics go to stderr. `screenshot` is empty if the screenshot could not be taken. `visits` is how many runs this profile has made.

`check_proxy.py` prints `{"source", "country", "city", "org", "ru", "traffic_left_mb"}`; if `ru` is false, tell the person the exit address is not in Russia.

Judge the result by the screenshot and `challenge`, not by `status` alone: some sites answer 403 and still render the full page.

## Exit codes

| Code | Meaning | What you do |
|---|---|---|
| 0 | page opened | report the result; send the screenshot if useful |
| 1 | unexpected error | report the `error` text to the person |
| 2 | wrong arguments | fix the command |
| 3 | challenge / captcha page | follow the captcha rule below |
| 4 | no token or bad `HIB_PROXY_URL` | ask the person to fill in `.env` themselves |
| 5 | proxy provider API error | tell the person: check token, balance, package |
| 6 | proxy unreachable or rejected the login | tell the person: check package and traffic left |
| 7 | page load timeout | you may retry once later, after a pause of a few minutes |
| 8 | browser did not start | tell the person to rerun `bash scripts/install.sh` |
| 9 | the site answered a server error (5xx) | not a block and not a captcha: wait a few minutes, retry once, then report |
| 10 | the profile is in use by another run | wait until that run ends, then retry once; never start a second run on the same profile, never work around the lock |
| 11 | profile problem (missing, damaged, cannot be created) | report the `error` text; do not delete or recreate the profile yourself, ask the person |

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
