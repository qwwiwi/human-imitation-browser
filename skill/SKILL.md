---
name: human-imitation-browser
description: Open a web page (Russian sites such as Ozon, Avito, Cian, Aviasales and similar) in a real browser through a Russian residential proxy with human-like pacing, get back status, title, text size and a screenshot, and hand any captcha or anti-bot page to the person instead of solving it. Use when the person asks to look at, check or screenshot a page that blocks server traffic.
---

# human-imitation-browser

A plain-files skill: this text plus one command-line script. It works from any agent that can run shell commands on the server: Claude Code, Codex, Hermes, OpenClaw-style agents and similar. No special integration.

The project lives in `~/human-imitation-browser` (if the person installed it elsewhere, use that path). Run every command from that folder.

## When to use

- The person asks you to open, look at, check or screenshot a page on a site that blocks server addresses (Ozon, Avito, Cian, Aviasales and similar Russian services).
- A normal fetch from the server returns a block page (for example 403 from a marketplace).

Do not use it for bulk collection, for many pages "just in case", or for anything the person did not ask for. For a seller's own shop data, suggest the marketplace's official seller API instead.

## How to call

```bash
cd ~/human-imitation-browser
.venv/bin/python scripts/open_page.py https://www.ozon.ru/
```

Several pages in one browser session, opened one after another with pauses:

```bash
.venv/bin/python scripts/open_page.py https://www.cian.ru/ https://www.avito.ru/
```

Options:

- `--no-images` – do not load images to save proxy traffic. Camoufox warns this can raise suspicion on some sites; drop it if challenges appear.
- `--out DIR` – where screenshots go (default `out/`).
- `--timeout SECONDS` – page load timeout (default 60).
- `--pause-min`, `--pause-max` – pause between pages in seconds (default 20 and 40). Do not lower them.

The browser takes about 10 seconds to start. A run with several pages takes minutes because of the pauses; that is intended.

To check the proxy without a browser: `.venv/bin/python scripts/check_proxy.py` (prints country, city, provider and `traffic_left_mb`).

## Output

stdout carries one JSON line per page:

```json
{"url": "...", "status": 200, "title": "...", "text_chars": 5120, "screenshot": "out/....png", "challenge": false, "reason": ""}
```

On an error, one line `{"error": "...", "exit_code": N}`. Diagnostics go to stderr.

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

## Captcha rule (mandatory)

When the exit code is 3 or `challenge` is `true`:

1. Stop. Do not open more pages on that site in this task.
2. Send the screenshot file from the `screenshot` field to the person in your dialog, with one line: which page, what you were asked to do.
3. Wait for the person's decision. They solve it themselves, tell you to try later, or drop the task.

Never:

- solve a captcha yourself, by any means;
- use captcha-solving services;
- try to get around the check: no proxy or IP changes, no browser restarts, no repeated attempts, no other tools to reach the same page.

A live hand-off (the person solving the captcha from a phone in the same browser) is not built yet. Do not promise it.

## Pacing rules

- One page at a time. Never run several copies of the script at once.
- Put related pages into one call so they share one session; keep the default pauses.
- Do not loop over pages, search results or catalogues. Open only what the person asked for.
- If a site starts showing challenges, stop working with it for this task.

## Secrets and accounts

- Never print, log, quote or send the proxy login, password, `MOBILEPROXY_API_TOKEN`, `HIB_PROXY_URL` or the contents of `.env`. Do not open `.env` to show it. The scripts never print these; keep it that way.
- If the person pastes a token into the chat, tell them to put it into `.env` themselves and to replace the token in the provider's cabinet, since the chat is not a safe place for it.
- Never log into any account, fill in forms, buy, order, write messages or click "confirm" on a site without the person's explicit instruction for that exact action.
- Use only the person's own accounts, and only when they ask.
