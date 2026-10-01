# Обрій Demo Bot — «Микола»

**Unofficial** demo Telegram sales bot for Обрій Тех ([@SampleShopBot](https://t.me/SampleShopBot)).
It answers questions about the catalogue in Ukrainian and takes orders. It hands chats over to a manager in an admin group. All contacts and payment details are fictional (ДЕМО).

Design and decisions: [PLAN.md](PLAN.md) (§12 is the decisions log). Current status: [STATE.md](STATE.md).

## Stack

- Python 3.14
- aiogram 3 webhook on aiohttp
- asyncpg → Neon Postgres
- Anthropic API with prompt caching
- Hosting: Render free web service, kept awake by UptimeRobot pinging `/healthz`

The application is one process (`main.py`). Configuration lives in `config.py`, and texts, the prompt, the tool schema, the catalogue and photo ids live in `data/`.

## Setup (local)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy env.example .env
```

Fill in `.env`; every key is explained in `env.example`. The most important ones:

| Key | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `WEBHOOK_SECRET` | Bot token and a random path/header secret |
| `ADMIN_CHAT_ID`, `PHOTO_CHANNEL_ID` | Admin group (the bot must be an admin there) and the photo storage channel |
| `ANTHROPIC_API_KEY`, `MODEL` | `claude-haiku-4-5` (≈ $0.01/reply) or `claude-sonnet-5-5` (≈ $0.02/reply) |
| `CACHE_TTL` | `1h` (default; survives pauses) or `5m` (cheaper cache writes) |
| `DATABASE_URL`, `TEST_DATABASE_URL` | Two **separate** Neon endpoints (direct URLs, `?sslmode=require`) |
| `ALLOWED_USER_IDS` | Empty = everyone |
| `TESTER_IDS` | Users allowed to use `/clear` |
| `MAX_PARTS` | Max bot messages per reply the model may send, 1–5 (default 3); order templates don't count |

## Run

| Mode | Command |
|---|---|
| Local, no public URL (deletes the webhook) | `python main.py --polling` |
| Webhook mode (what Render runs) | `python main.py` |

## Deploy (Render)

1. On Render, choose **New → Blueprint** and point it at this repo (`render.yaml`). Enter the secrets when asked, plus `MODEL` and `TESTER_IDS`.
2. On startup the bot sets its own webhook to `RENDER_EXTERNAL_URL/tg/<secret>`.
3. Point UptimeRobot at `https://<service>.onrender.com/healthz` every 5 minutes.
4. Check the webhook with `python scrape.py webhook info`: `last_error_message` must be empty.
5. In BotFather, the bot description must say the bot is unofficial.

## Telegram commands

| Where | Command | Effect |
|---|---|---|
| Client chat | `/start` | Greeting |
| Client chat | `/clear` (testers only) | Forget this chat's history, orders and handoffs |
| Admin group | `/orders` | Open orders |
| Admin group | `/paid 17` · `/cancel 17` | Mark order 17 paid (the client is notified) or cancelled |
| Admin group | `/bot 123456789` | Hand a chat back to the bot |
| Admin group | `/stats` · `/stats 168` · `/stats 0` | Replies, rejected attempts, LLM cost, cache share, LLM latency, time from the client's last message to the first reply part (p50/p90), orders, open handoffs. Window: 24 h (default), N hours, or all time (`0`) |

Handoff cards in the admin group have the buttons ✋ Взяти · 🤖 Повернути боту · ✅ Закрити. To answer the client, **reply** to the card or to a relayed message.

## Data refresh

| Command | Effect |
|---|---|
| `python scrape.py catalog [--refresh]` | Scrape and validate → `data/catalog.json` |
| `python scrape.py photos [--only SLUG …]` | Upload photos to the channel → `data/photos.json` |

## Tests

| Shell | Unit + DB tests (no API calls; DB tests need `TEST_DATABASE_URL`) |
|---|---|
| CMD / Git Bash | `python -m pytest -q` |

The live harness runs the 19 PLAN §10 cases against the real model. **It costs money**: about $1.5 for 3 runs of every case on Sonnet 5.5.

| Shell | Live harness |
|---|---|
| CMD | `set RUN_LIVE=1 && python -m pytest tests/test_live.py -q -s` |
| Git Bash | `RUN_LIVE=1 python -m pytest tests/test_live.py -q -s` |

Options:

- `LIVE_RUNS=3`: runs per case
- `LIVE_CASES=TC01,TC09`: run only these cases
- `LIVE_BUDGET=4`: USD cap; once spending passes it, the remaining runs are skipped

## Costs

- **System prompt:** ≈ 15.5k tokens, cached once per model and prompt and shared by all chats.
- **Per reply:** a cache read plus the history, cards and output tokens: ≈ $0.01 on Haiku 4.5 and ≈ $0.02 on Sonnet 5.5.
- **Hosting:** Neon, Render and UptimeRobot are all on free plans.
- **Live figures:** `/stats` shows the real numbers.
