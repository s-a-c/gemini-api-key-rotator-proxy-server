# Gemini API Key Rotator Proxy Server

A FastAPI/Uvicorn reverse proxy for the Google Gemini API that manages a pool of API keys with automatic rotation, rate-limit handling, and operational tooling. Designed for use with Roo Code, Cline, Cursor, Windsurf, LibreChat, Continue, and any other tool that speaks the Gemini REST API.

## Features

- **API Key Rotation** — Least-usage-first load balancing across a pool of keys. Proactively rotates after `MAX_REQ_PER_KEY` requests per key.
- **Rate-Limit Handling** — 429 responses trigger a 60-second per-key cooldown; other failures use a 5-second backoff. The proxy retries with the next available key transparently.
- **Streaming Support** — Correctly proxies SSE streaming responses (`alt=sse`, `:streamGenerateContent`) using a shared long-lived `httpx.AsyncClient`.
- **Live Metrics** — In-process counters for total requests, success/fail, 429 events, key rotations, and a 100-event benching history.
- **Web Dashboard** — Live key pool status table, metrics summary bar, and SSE log tail at `/dashboard`.
- **HTTP Status Endpoints** — `/health` (200/503), `/status` (JSON), `/pool-status` (dashboard feed), `/reload-keys` (POST hot-reload).
- **Telegram Bot** — 8 commands (`/help`, `/status`, `/rotate`, `/ban`, `/unban`, `/uptime`, `/config`, `/digest`) + inline keyboard buttons + proactive per-key failure and pool health alerts.
- **macOS `launchd` Service** — Runs as `com.antigravity.proxy`, auto-starts on login, restarts on crash, with `newsyslog` log rotation.
- **`.env` Support** — Loads secrets from a `.env` file at startup via `python-dotenv` (soft dependency).

## Requirements

- Python 3.9+
- `fastapi`
- `uvicorn`
- `httpx`
- `python-dotenv` *(optional — for `.env` file support)*

## Quick Start

```sh
# 1. Clone
git clone https://github.com/s-a-c/gemini-api-key-rotator-proxy-server.git
cd gemini-api-key-rotator-proxy-server

# 2. Install dependencies
pip install fastapi uvicorn httpx python-dotenv
# or with uv:
uv sync

# 3. Add your Gemini API keys (one per line)
echo "AIzaSy...key1" >> api_keys.txt
echo "AIzaSy...key2" >> api_keys.txt

# 4. (Optional) Configure secrets
cp .env.example .env
# edit .env with your TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ADMIN_TOKEN

# 5. Run
uv run uvicorn main:APP --host 0.0.0.0 --port 8888
```

## Configuration

### `api_keys.txt`

One key per line. Both API key format (`AIzaSy...`) and OAuth Bearer tokens are supported — the proxy detects the format automatically.

```
AIzaSy...key1
AIzaSy...key2
AIzaSy...key3
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | `""` | Telegram Bot API token from [@BotFather](https://t.me/BotFather). Leave empty to disable Telegram entirely. |
| `TELEGRAM_CHAT_ID` | `""` | Your numeric Telegram chat ID (get it from [@userinfobot](https://t.me/userinfobot)). |
| `ADMIN_TOKEN` | `changeme_local_only` | Bearer token for accessing internal endpoints from non-localhost clients. |

Set these in any of three ways (highest priority wins):

1. **Shell environment** — `export TELEGRAM_TOKEN=...`
2. **launchd plist** `EnvironmentVariables` dict
3. **`.env` file** — copy `.env.example` to `.env` and fill in values

### Key Tuning (`main.py`)

| Constant | Default | Effect |
|---|---|---|
| `MAX_REQ_PER_KEY` | `15` | Proactive rotation threshold |
| `COOLDOWN_PERIOD` | `60s` | Per-key cooldown after a 429 |
| `BACKOFF_MIN` | `5s` | Per-key cooldown after other failures |

## HTTP Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/{path}` | `GET/POST/…` | Proxy to `https://generativelanguage.googleapis.com/v1beta/{path}` |
| `/health` | GET | `200 {"status":"ok"}` / `503 {"status":"degraded"}` |
| `/status` | GET | JSON pool snapshot + full metrics |
| `/pool-status` | GET | Lightweight JSON for dashboard polling |
| `/reload-keys` | POST | Hot-reload `api_keys.txt` without restarting |
| `/dashboard` | GET | Web dashboard (key table + metrics bar + live log tail) |
| `/logs` | GET | Raw SSE stream of `logs/proxy.log` |
| `HEAD /*` | HEAD | Always `200` — for basic uptime monitors |

**Auth:** Requests from `127.0.0.1`/`::1` are always allowed. Remote requests to internal endpoints require `Authorization: Bearer <ADMIN_TOKEN>`.

### Path Normalisation

The proxy accepts any of these equivalent forms and forwards them identically:

```sh
curl http://127.0.0.1:8888/v1beta/models          # explicit v1beta prefix
curl http://127.0.0.1:8888/v1/models               # v1 prefix
curl http://127.0.0.1:8888/models                  # bare path
```

## Telegram Bot Commands

| Command | Description |
|---|---|
| `/help` | List all commands |
| `/status` | Key pool status with inline action buttons |
| `/rotate` | Reset all usage counters and cooldowns |
| `/ban <key> [seconds]` | Bench a key for a given duration (default 24h) |
| `/unban` | Clear all active cooldowns |
| `/uptime` | Uptime, request counts, success rate |
| `/config` | Current runtime configuration values |
| `/digest` | Full health summary with recent benching events |

Inline buttons on the `/status` response: **🔄 Reload Keys** · **🔃 Rotate All** · **✅ Unban All**

### Proactive Alerts

| Alert | Trigger |
|---|---|
| 🔴 Key benched (rate limit) | Key receives HTTP 429 |
| ❌ Key benched (auth error) | Key receives HTTP 401/403 |
| ⚠️ Key benched (other) | Any other upstream failure |
| ⚠️ Pool degraded | >50% of keys simultaneously benched |
| 🚨 All keys exhausted | Pool has no available keys |

Global cap: 5 notifications per minute. Per-key cooldown: 5 minutes.

## macOS `launchd` Service

See **[`docs/launchd-service-guide.md`](docs/launchd-service-guide.md)** for the complete installation and operations guide. Quick reference:

```sh
# Install
cp service/LaunchAgents/com.antigravity.proxy.plist ~/Library/LaunchAgents/
# Edit WorkingDirectory and EnvironmentVariables in the plist, then:
launchctl load ~/Library/LaunchAgents/com.antigravity.proxy.plist

# Status
launchctl list | grep antigravity

# Reload keys (no restart needed)
curl -X POST http://127.0.0.1:8888/reload-keys

# Health check
curl http://127.0.0.1:8888/health

# Unload
launchctl unload ~/Library/LaunchAgents/com.antigravity.proxy.plist
```

Log rotation is provided by `service/etc/newsyslog.d/com.antigravity.proxy.conf` — install to `/etc/newsyslog.d/` to rotate `~/Library/Logs/com.antigravity.proxy.*` automatically.

## Use with Roo Code / Cline / Cursor

**Native Gemini mode (`main.py`):**

- Provider: **Google Gemini**
- Base URL: `http://127.0.0.1:8888`
- API key: *(leave blank — the proxy injects keys from `api_keys.txt`)*

**OpenAI-compatible mode (`main-openai.py`):**

- Provider: **OpenAI Compatible**
- Base URL: `http://127.0.0.1:8888`
- API key: `changeme_local_only`
- Model: e.g. `gemini-2.5-pro`

## Project Structure

```
.
├── main.py                          # Proxy app (native Gemini mode)
├── main-openai.py                   # Proxy app (OpenAI-compatible mode)
├── api_keys.txt                     # Your API keys (gitignored)
├── .env.example                     # Environment variable template
├── requirements.txt                 # Python dependencies
├── logs/
│   └── proxy.log                    # Rotating application log
├── service/
│   ├── LaunchAgents/
│   │   └── com.antigravity.proxy.plist      # launchd service definition
│   └── etc/
│       └── newsyslog.d/
│           └── com.antigravity.proxy.conf   # Log rotation config
└── docs/
    ├── launchd-service-guide.md     # Full service documentation
    └── plans/
        └── 010-proxy-enhancements-implementation-plan.md
```

## License

MIT — see [LICENSE](LICENSE).