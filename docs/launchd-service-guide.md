# Antigravity Proxy — macOS `launchd` Service Guide

> Comprehensive implementation guide for running the Gemini API Key Rotator Proxy
> as a persistent, self-healing macOS background service via `launchd`.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [File Inventory](#file-inventory)
- [Installation](#installation)
  - [Step 1 — Clone & Prepare the Project](#step-1--clone--prepare-the-project)
  - [Step 2 — Configure API Keys](#step-2--configure-api-keys)
  - [Step 3 — Configure Environment Variables](#step-3--configure-environment-variables)
  - [Step 4 — Install the Launch Agent](#step-4--install-the-launch-agent)
  - [Step 5 — Install Log Rotation](#step-5--install-log-rotation)
  - [Step 6 — Load the Service](#step-6--load-the-service)
- [Plist Reference](#plist-reference)
  - [Full Annotated Plist](#full-annotated-plist)
  - [Key-by-Key Breakdown](#key-by-key-breakdown)
- [Environment Variables](#environment-variables)
- [Logging Architecture](#logging-architecture)
  - [Log Locations](#log-locations)
  - [Application Log Format](#application-log-format)
  - [Log Rotation — Application Logs](#log-rotation--application-logs)
  - [Log Rotation — launchd Logs (newsyslog)](#log-rotation--launchd-logs-newsyslog)
  - [Why Two Layers?](#why-two-layers)
- [Telegram Integration](#telegram-integration)
  - [Setup](#setup)
  - [Bot Commands](#bot-commands)
  - [Resilience Features](#resilience-features)
  - [Disabling Telegram](#disabling-telegram)
- [Proxy Endpoints & Behaviour](#proxy-endpoints--behaviour)
  - [Upstream Proxy (catch-all)](#upstream-proxy-catch-all)
  - [Internal Endpoints](#internal-endpoints)
  - [Key Rotation Logic](#key-rotation-logic)
  - [Streaming Support](#streaming-support)
- [Operations](#operations)
  - [Start the Service](#start-the-service)
  - [Stop the Service](#stop-the-service)
  - [Restart the Service](#restart-the-service)
  - [Check Service Status](#check-service-status)
  - [Health Check](#health-check)
  - [View Logs](#view-logs)
  - [Tail Logs in Real Time](#tail-logs-in-real-time)
  - [Truncate Logs Manually](#truncate-logs-manually)
  - [Reload API Keys Without Restarting](#reload-api-keys-without-restarting)
- [Troubleshooting](#troubleshooting)
  - [Service Won't Start](#service-wont-start)
  - [Port Already in Use](#port-already-in-use)
  - [All Keys Rate-Limited (429)](#all-keys-rate-limited-429)
  - [502 Bad Gateway](#502-bad-gateway)
  - [Telegram Errors in Logs](#telegram-errors-in-logs)
  - [Duplicate Log Lines](#duplicate-log-lines)
  - [Logs Growing Unbounded](#logs-growing-unbounded)
- [Uninstallation](#uninstallation)
- [Bugs Fixed in This Version](#bugs-fixed-in-this-version)
- [Configuration Tuning Reference](#configuration-tuning-reference)
- [Security Considerations](#security-considerations)

---

## Overview

The **Antigravity Proxy** (`com.antigravity.proxy`) is a FastAPI/Uvicorn reverse
proxy that sits between your development tools (Cursor, Roo Code, Windsurf,
Continue, opencode, LibreChat, etc.) and the Google Gemini API. It manages a pool
of API keys, automatically rotating between them to distribute load and avoid
rate limits.

When installed as a macOS **Launch Agent**, the proxy:

- **Starts automatically** when you log in (`RunAtLoad`)
- **Self-heals** — if the process crashes, `launchd` restarts it immediately (`KeepAlive`)
- **Runs in the background** with no terminal window needed
- **Rotates logs** at both the application and system level
- **Sends Telegram alerts** when all keys are exhausted (optional)

---

## Architecture

```log
┌───────────────────────────────────────────────────────────────-───┐
│  macOS Login Session (Aqua)                                       │
│                                                                   │
│  launchd ──► com.antigravity.proxy                                │
│              │                                                    │
│              ├─ /opt/homebrew/bin/uv run uvicorn main:APP         │
│              │      ▲                                             │
│              │      │  KeepAlive=true (auto-restart on crash)     │
│              │      │  RunAtLoad=true  (start on login)           │
│              │                                                    │
│              ├─ stdout ──► ~/Library/Logs/...proxy.log            │
│              ├─ stderr ──► ~/Library/Logs/...proxy.err            │
│              └─ app    ──► ./logs/proxy.log (RotatingFileHandler) │
│                                                                   │
│  ┌───────────────┐    :8888    ┌────────────────────┐             │
│  │  IDE / Client  │──────────►│  Antigravity Proxy  │             │
│  └───────────────┘            │  (FastAPI+Uvicorn)  │             │
│                               └────────┬───────────┘              │
│                                        │                          │
│                          ┌─────────────┼─────────────┐            │
│                          ▼             ▼             ▼            │
│                      Key Pool      Telegram      Dashboard        │
│                    (api_keys.txt)    Bot         (/dashboard)     │
│                          │                                        │
└──────────────────────────┼──────────────────────────────────────-─┘
                           │
                           ▼
              Google Gemini API (v1beta)
              generativelanguage.googleapis.com

```

---

## Prerequisites

| Requirement | Minimum | Verify |
|---|---|---|
| **macOS** | 10.15+ (Catalina) | `sw_vers` |
| **Homebrew** | Any recent version | `brew --version` |
| **uv** (Python package runner) | 0.4+ | `uv --version` |
| **Python** | 3.10+ (managed by uv) | `uv run python --version` |
| **Gemini API keys** | At least 1 | [Google AI Studio](https://aistudio.google.com/apikey) |

Install `uv` if missing:

```sh
brew install uv

```

> **Note:** The plist uses the absolute path `/opt/homebrew/bin/uv`. If you
> installed uv elsewhere, update the plist accordingly. Find your path with
> `which uv`.

---

## File Inventory

Every file involved in the service, its location, and its purpose:

### Project Files (in the repo working directory)

| File | Purpose |
|---|---|
| `main.py` | The proxy application — FastAPI app, key pool, Telegram manager |
| `api_keys.txt` | One Gemini API key per line (gitignored) |
| `requirements.txt` | Python dependencies: `fastapi`, `uvicorn`, `httpx` |
| `logs/proxy.log` | Application-level rotating log (created automatically) |
| `logs/proxy.log.1` … `.3` | Rotated backup logs |

### Service Configuration Files (in `service/` directory)

| File | Mirrors System Path | Purpose |
|---|---|---|
| `service/LaunchAgents/com.antigravity.proxy.plist` | `~/Library/LaunchAgents/` | Launch Agent definition (template) |
| `service/etc/newsyslog.d/com.antigravity.proxy.conf` | `/etc/newsyslog.d/` | Log rotation for launchd stdout/stderr |

### Live System Files

| Path | Purpose |
|---|---|
| `~/Library/LaunchAgents/com.antigravity.proxy.plist` | Active plist loaded by launchd |
| `~/Library/Logs/com.antigravity.proxy.log` | launchd-captured stdout |
| `~/Library/Logs/com.antigravity.proxy.err` | launchd-captured stderr (uvicorn output) |
| `/etc/newsyslog.d/com.antigravity.proxy.conf` | Active newsyslog rotation config |

---

## Installation

### Step 1 — Clone & Prepare the Project

```sh
git clone https://github.com/jwadow/gemini-api-key-rotator-proxy-server.git
cd gemini-api-key-rotator-proxy-server

```

Verify `uv` can resolve dependencies:

```sh
uv run python -c "import fastapi, uvicorn, httpx; print('OK')"

```

### Step 2 — Configure API Keys

Create `api_keys.txt` in the project root with one key per line:

```sh
cat > api_keys.txt << 'EOF'
AIzaSy...your-first-key
AIzaSy...your-second-key
AIzaSy...your-third-key
EOF

```

> **Tip:** The more keys you add, the higher your effective rate limit.
> Free-tier keys each get 15 RPM for Gemini 2.5 Pro.

### Step 3 — Configure Environment Variables

Edit the plist template to set your working directory and (optionally) your
Telegram credentials:

```sh
# Open the template in your editor
open service/LaunchAgents/com.antigravity.proxy.plist

```

You **must** update the `WorkingDirectory` value to match your actual clone
location. If you don't need Telegram alerts, leave the token strings empty.

### Step 4 — Install the Launch Agent

Copy the configured plist to the LaunchAgents directory:

```sh
cp service/LaunchAgents/com.antigravity.proxy.plist \
   ~/Library/LaunchAgents/com.antigravity.proxy.plist

```

Validate the plist syntax:

```sh
plutil -lint ~/Library/LaunchAgents/com.antigravity.proxy.plist
# Expected: ...plist: OK

```

### Step 5 — Install Log Rotation

The `newsyslog` config prevents the launchd-captured stderr/stdout logs from
growing unbounded. This requires `sudo`:

```sh
sudo cp service/etc/newsyslog.d/com.antigravity.proxy.conf \
        /etc/newsyslog.d/com.antigravity.proxy.conf

```

Verify:

```sh
cat /etc/newsyslog.d/com.antigravity.proxy.conf

```

### Step 6 — Load the Service

```sh
launchctl load ~/Library/LaunchAgents/com.antigravity.proxy.plist

```

Verify it's running:

```sh
launchctl list com.antigravity.proxy
# Look for: "PID" = <some number>

curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8888/dashboard
# Expected: 200

```

The proxy is now running at `http://0.0.0.0:8888` and will survive reboots and
crashes automatically.

---

## Plist Reference

### Full Annotated Plist

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- Unique reverse-DNS identifier for this service -->
    <key>Label</key>
    <string>com.antigravity.proxy</string>

    <!-- The command to execute, split into an array of arguments -->
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/uv</string>  <!-- uv package runner -->
        <string>run</string>                    <!-- run a command in a uv env -->
        <string>uvicorn</string>                <!-- ASGI server -->
        <string>main:APP</string>               <!-- module:variable -->
        <string>--host</string>
        <string>0.0.0.0</string>                <!-- listen on all interfaces -->
        <string>--port</string>
        <string>8888</string>                   <!-- listen port -->
    </array>

    <!-- Environment variables injected into the process -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>TELEGRAM_TOKEN</key>
        <string></string>         <!-- Bot token from @BotFather, or empty -->
        <key>TELEGRAM_CHAT_ID</key>
        <string></string>         <!-- Your Telegram numeric chat ID, or empty -->
    </dict>

    <!-- Start automatically when the user logs in -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Restart automatically if the process exits for any reason -->
    <key>KeepAlive</key>
    <true/>

    <!-- Working directory — must contain main.py and api_keys.txt -->
    <key>WorkingDirectory</key>
    <string>/path/to/gemini-api-key-rotator-proxy-server</string>

    <!-- Capture stdout to a log file -->
    <key>StandardOutPath</key>
    <string>/Users/YOURUSER/Library/Logs/com.antigravity.proxy.log</string>

    <!-- Capture stderr to a separate log file -->
    <key>StandardErrorPath</key>
    <string>/Users/YOURUSER/Library/Logs/com.antigravity.proxy.err</string>
</dict>
</plist>

```

### Key-by-Key Breakdown

| Plist Key | Type | Value | Purpose |
|---|---|---|---|
| `Label` | string | `com.antigravity.proxy` | Unique service identifier used by `launchctl` |
| `ProgramArguments` | array | See above | The command and its arguments, split into an array. launchd does not invoke a shell, so each argument must be a separate `<string>`. |
| `EnvironmentVariables` | dict | `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | Injected into the process environment. launchd processes do NOT inherit your shell environment (no `.bashrc`, `.zshrc`, `.envrc`). Every variable the app needs must be declared here. |
| `RunAtLoad` | boolean | `true` | Start the service immediately when the plist is loaded (including at login). |
| `KeepAlive` | boolean | `true` | If the process exits for any reason (crash, signal, OOM), launchd will restart it. |
| `WorkingDirectory` | string | `/path/to/project` | Sets the `cwd` for the process. Relative paths in `main.py` (like `api_keys.txt` and `logs/`) resolve from here. |
| `StandardOutPath` | string | `~/Library/Logs/...log` | File where stdout is redirected. Uvicorn's access log lines go here. |
| `StandardErrorPath` | string | `~/Library/Logs/...err` | File where stderr is redirected. Uvicorn's lifecycle messages and the app's own log output go here. |

> **Important:** launchd runs in a minimal environment. It does **not** source
> `~/.zshrc`, `~/.bashrc`, `~/.envrc`, or any shell configuration. The `PATH`
> is minimal. That's why we use the absolute path `/opt/homebrew/bin/uv` and
> why secrets must be set via `EnvironmentVariables` in the plist.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_TOKEN` | No | `""` (empty) | Telegram Bot API token from [@BotFather](https://t.me/BotFather). Format: `123456789:AABBccdd...` |
| `TELEGRAM_CHAT_ID` | No | `""` (empty) | Your numeric Telegram chat ID. Get it by messaging [@userinfobot](https://t.me/userinfobot). |

**Telegram is entirely optional.** If either variable is empty or unset, the
proxy disables all Telegram functionality silently and logs:

```log
Telegram integration disabled (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set)

```

To add more environment variables (e.g., `DEBUG`, `PYTHONUNBUFFERED`), add
additional `<key>/<string>` pairs inside the `EnvironmentVariables` dict in the
plist.

---

## Logging Architecture

The proxy uses a **dual-layer** logging design: application-level rotation for
structured logs, and system-level `newsyslog` rotation for the raw launchd
capture files.

### Log Locations

| Log File | Written By | Contains | Rotation |
|---|---|---|---|
| `logs/proxy.log` | App (`RotatingFileHandler`) | Application events with request IDs | 5 MB, 3 backups |
| `~/Library/Logs/com.antigravity.proxy.err` | launchd (stderr) | Uvicorn lifecycle + app stderr | newsyslog, 1 MB, 3 backups |
| `~/Library/Logs/com.antigravity.proxy.log` | launchd (stdout) | Uvicorn access log lines | newsyslog, 512 KB, 3 backups |

### Application Log Format

Every line in `logs/proxy.log` follows this format:

```log
2026-02-18 04:20:52,450 [a1b2c3d4] ERROR - TG Listener Error: ReadTimeout('')
│                        │          │       └─ message
│                        │          └─ level (INFO/WARNING/ERROR)
│                        └─ request ID (8-char UUID prefix)
└─ ISO 8601 timestamp

```

The `[a1b2c3d4]` request ID is unique per HTTP request and is also returned to
the client as the `X-Request-ID` response header, making it easy to correlate
client-side issues with server-side log entries.

The special request ID `[init]` is used for messages outside of a request
context (startup, shutdown, Telegram listener).

### Log Rotation — Application Logs

Handled by Python's `RotatingFileHandler` inside `main.py`:

| Setting | Value |
|---|---|
| Max file size | 5 MB (`maxBytes=5*1024*1024`) |
| Backup count | 3 (`backupCount=3`) |
| Rotated files | `proxy.log.1`, `proxy.log.2`, `proxy.log.3` |
| Max disk usage | ~20 MB (1 active + 3 backups) |

Rotation happens automatically in-process. No external cron job or signal is
needed.

### Log Rotation — launchd Logs (newsyslog)

The launchd stderr/stdout files (`~/Library/Logs/com.antigravity.proxy.*`)
are **not** managed by the application — launchd writes to them directly. Without
external rotation, they grow unbounded.

The `newsyslog` config at `/etc/newsyslog.d/com.antigravity.proxy.conf` handles
this:

```
# logfilename                                              [owner:group] mode count size(KB) when flags
/Users/YOU/Library/Logs/com.antigravity.proxy.log           YOU:staff    644  3     512      *    J
/Users/YOU/Library/Logs/com.antigravity.proxy.err           YOU:staff    644  3     1024     *    J

```

| Column | `.log` (stdout) | `.err` (stderr) |
|---|---|---|
| Max size before rotation | 512 KB | 1 MB |
| Backups kept | 3 | 3 |
| Compression | bzip2 (`J` flag) | bzip2 (`J` flag) |
| Trigger | Any time size exceeded (`*`) | Any time size exceeded (`*`) |

macOS runs `newsyslog` periodically via its own launchd job. You can also
trigger it manually:

```sh
sudo newsyslog -v

```

### Why Two Layers?

| Layer | Captures | Rotated By | Why Needed |
|---|---|---|---|
| **Application** (`logs/proxy.log`) | App events, request tracing, errors | Python `RotatingFileHandler` | Structured logs with request IDs; used by the `/dashboard` live tail |
| **System** (`~/Library/Logs/...`) | Uvicorn startup/shutdown, raw stderr | `newsyslog` | Captures crashes, segfaults, and anything that happens before the app logger initialises |

---

## Telegram Integration

### Setup

1. **Create a bot** — Message [@BotFather](https://t.me/BotFather) on Telegram,
   send `/newbot`, and follow the prompts. Copy the token.

2. **Get your chat ID** — Message [@userinfobot](https://t.me/userinfobot) and
   copy the numeric ID it returns.

3. **Add to the plist** — Set both values in `EnvironmentVariables`:

   ```xml
   <key>TELEGRAM_TOKEN</key>
   <string>123456789:AABBccddEEffGGhhIIjj...</string>
   <key>TELEGRAM_CHAT_ID</key>
   <string>987654321</string>
   
   ```

4. **Reload the service:**

   ```sh
   launchctl unload ~/Library/LaunchAgents/com.antigravity.proxy.plist
   launchctl load   ~/Library/LaunchAgents/com.antigravity.proxy.plist
   
   ```

### Bot Commands

| Command / Action | What it Does |
|---|---|
| `/status` | Returns a status report showing each key's preview, usage count, and availability |
| `🔄 Reload Keys` (inline button) | Hot-reloads `api_keys.txt` without restarting the proxy, then sends an updated status report |

### Resilience Features

The Telegram listener was significantly hardened in this version:

| Feature | Details |
|---|---|
| **Correct HTTP timeout** | The `httpx.AsyncClient` uses `timeout=30` seconds, exceeding the Telegram long-poll timeout of 20 seconds. The original code used the httpx default of 5 seconds, causing `ReadTimeout` errors every 5 seconds. |
| **Exponential backoff** | On error, waits 5s → 10s → 20s → 40s → … → 300s (5 min cap) before retrying. Resets to 5s on success. |
| **Descriptive errors** | Uses `repr(e)` instead of `str(e)` so error types are always visible (e.g., `ReadTimeout('')` instead of blank). |
| **API response validation** | Checks `data["ok"]` from Telegram before processing. Invalid tokens now produce clear `RuntimeError: Telegram API error: Unauthorized` messages. |
| **Graceful degradation** | `send_alert()` wraps its HTTP call in try/except and logs failures as warnings, never crashing the main proxy. |
| **Rate-limited alerts** | At most one alert per 10 minutes to avoid Telegram spam. |

### Disabling Telegram

Simply leave both `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` empty (or remove them
from the plist entirely). The proxy will log a single info message at startup and
never attempt any Telegram API calls:

```log
Telegram integration disabled (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set)

```

---

## Proxy Endpoints & Behaviour

### Upstream Proxy (catch-all)

**Any request** that doesn't match an internal endpoint is proxied to the
Google Gemini API:

```log
Client request:  POST http://127.0.0.1:8888/v1beta/models/gemini-2.5-pro:generateContent
Proxied to:      POST https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent

```

Path normalisation strips `v1/` and `v1beta/` prefixes, so all of these work
identically:

```log
http://127.0.0.1:8888/v1beta/models/gemini-2.5-pro:generateContent
http://127.0.0.1:8888/v1/models/gemini-2.5-pro:generateContent
http://127.0.0.1:8888/models/gemini-2.5-pro:generateContent

```

Authentication is injected automatically:

- Keys starting with `AIza` → added as `?key=` query parameter
- Other keys (OAuth tokens) → added as `Authorization: Bearer` header

The client does **not** need to send any API key. Configure your tool with a
dummy key like `proxy-managed-session`.

### Internal Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/dashboard` | GET | Live web dashboard — tails `logs/proxy.log` via SSE in a dark-themed terminal UI |
| `/logs` | GET | Raw SSE stream of log lines (used by the dashboard's `EventSource`) |
| `HEAD /*` | HEAD | Always returns `200 OK` — designed for health checks and uptime monitors |

### Key Rotation Logic

The `KeyPool` manages all loaded keys with a least-usage-first strategy:

```log
1. Filter keys where:
   - Not currently in cooldown (banned_until < now)
   - Usage count < MAX_REQ_PER_KEY (default: 15)

2. Sort available keys by usage_count (ascending)

3. Pick the least-used key

4. If no keys available:
   - Find the key with the soonest cooldown expiry
   - If expired: reset its usage and use it
   - If still cooling down: return 429 to the client
     and fire a Telegram alert

```

On each response:

| Upstream Status | Action |
|---|---|
| `< 400` (success) | `mark_success()` — increment usage counter |
| `429` (rate limit) | `mark_failure()` — 60-second cooldown, try next key |
| Other `>= 400` | `mark_failure()` — 5-second cooldown, try next key |
| Exception | `mark_failure(500)` — 5-second cooldown, try next key |

The proxy retries with the next available key up to `len(pool)` times before
returning `502 Bad Gateway`.

### Streaming Support

Streaming is detected when:

- `alt=sse` is in the query string, **or**
- `:generateContent` is in the URL path

Streamed responses use `StreamingResponse` with `media_type="text/event-stream"`,
forwarding chunks as they arrive from Google with no buffering.

---

## Operations

### Start the Service

```sh
launchctl load ~/Library/LaunchAgents/com.antigravity.proxy.plist

```

### Stop the Service

```sh
launchctl unload ~/Library/LaunchAgents/com.antigravity.proxy.plist

```

### Restart the Service

```sh
launchctl unload ~/Library/LaunchAgents/com.antigravity.proxy.plist
sleep 1
launchctl load   ~/Library/LaunchAgents/com.antigravity.proxy.plist

```

### Check Service Status

```sh
launchctl list com.antigravity.proxy

```

Key fields in the output:

| Field | Meaning |
|---|---|
| `"PID" = 12345` | Process is running with this PID |
| `"LastExitStatus" = 0` | Last exit was clean |
| `"LastExitStatus" = 256` | Last exit was error (code 1) |
| No `PID` key present | Process is not currently running |

### Health Check

```sh
# Quick check — returns HTTP status code
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8888/

# HEAD request (always 200 if service is up)
curl -I http://127.0.0.1:8888/

# Dashboard (browser)
open http://127.0.0.1:8888/dashboard

```

### View Logs

```sh
# Application log (structured, with request IDs)
cat logs/proxy.log

# launchd stderr (uvicorn lifecycle messages)
cat ~/Library/Logs/com.antigravity.proxy.err

# launchd stdout (uvicorn access log)
cat ~/Library/Logs/com.antigravity.proxy.log

```

### Tail Logs in Real Time

```sh
# Application log
tail -f logs/proxy.log

# launchd stderr
tail -f ~/Library/Logs/com.antigravity.proxy.err

# Both simultaneously
tail -f logs/proxy.log ~/Library/Logs/com.antigravity.proxy.err

# Or use the web dashboard
open http://127.0.0.1:8888/dashboard

```

### Truncate Logs Manually

```sh
# Truncate without stopping the service (safe — files stay open)
: > ~/Library/Logs/com.antigravity.proxy.err
: > ~/Library/Logs/com.antigravity.proxy.log
: > logs/proxy.log

```

### Reload API Keys Without Restarting

There are two ways to hot-reload `api_keys.txt`:

1. **Via Telegram** — Send `/status` to your bot, then tap the `🔄 Reload Keys`
   inline button.

2. **Restart the service** — The key file is read at startup:

   ```sh
   launchctl unload ~/Library/LaunchAgents/com.antigravity.proxy.plist
   sleep 1
   launchctl load   ~/Library/LaunchAgents/com.antigravity.proxy.plist

   ```

---

## Troubleshooting

### Service Won't Start

**Symptoms:** No PID in `launchctl list`, or the process starts and immediately exits.

```sh
# 1. Check plist syntax
plutil -lint ~/Library/LaunchAgents/com.antigravity.proxy.plist

# 2. Check launchd status
launchctl list com.antigravity.proxy

# 3. Check stderr for startup errors
cat ~/Library/Logs/com.antigravity.proxy.err

# 4. Check that uv exists at the specified path
ls -la /opt/homebrew/bin/uv

# 5. Check that the working directory exists and contains main.py
ls -la /path/to/gemini-api-key-rotator-proxy-server/main.py

# 6. Try running the command manually
cd /path/to/gemini-api-key-rotator-proxy-server
/opt/homebrew/bin/uv run uvicorn main:APP --host 0.0.0.0 --port 8888

```

### Port Already in Use

```sh
# Find what's using port 8888
lsof -i :8888

# Kill it (replace PID)
kill <PID>

# Or change the port in the plist and reload

```

### All Keys Rate-Limited (429)

**Symptoms:** All requests return `{"error": "all keys rate-limited"}` with HTTP 429.

```sh
# Check key status via the application
curl http://127.0.0.1:8888/dashboard

# Add more keys to api_keys.txt, then restart/reload

```

The default configuration allows 15 requests per key before proactive rotation.
With 5 keys, that's 75 requests before any cooldowns kick in. If you hit 429s
from Google, each affected key enters a 60-second cooldown.

### 502 Bad Gateway

**Symptoms:** Requests return `{"error": "no upstream key succeeded"}` with HTTP 502.

This means every key in the pool was tried and all returned 4xx/5xx from
Google. Common causes:

- All keys are invalid or revoked
- Google API is down
- Network connectivity issue

Check the application log for specific error details:

```sh
grep "Request error\|mark_failure" logs/proxy.log | tail -20

```

### Telegram Errors in Logs

**Symptom:** Lines like `TG Listener Error: ReadTimeout('')` or
`TG Listener Error: RuntimeError('Telegram API error: Unauthorized')`.

| Error | Cause | Fix |
|---|---|---|
| `ReadTimeout('')` | httpx timeout is shorter than Telegram's long-poll timeout | Already fixed in this version (`timeout=30`). If you see this, ensure you're running the latest `main.py`. |
| `Telegram API error: Unauthorized` | The bot token is invalid or revoked | Create a new bot with [@BotFather](https://t.me/BotFather) and update `TELEGRAM_TOKEN` in the plist. |
| `Telegram API error: Not Found` | The `TELEGRAM_CHAT_ID` is wrong | Verify your chat ID with [@userinfobot](https://t.me/userinfobot). |
| `ConnectError(...)` | Network issue reaching `api.telegram.org` | Check DNS, firewall, or VPN settings. The proxy itself still works — only alerts are affected. |

Exponential backoff ensures that transient Telegram errors don't flood the logs.
After the first failure, retry intervals grow: 5s → 10s → 20s → … → 300s max.

### Duplicate Log Lines

**Symptom:** Every line in stderr appears twice — once with `INFO:` prefix and
once without.

This was a bug in the original code that has been **fixed in this version**. The
cause was an extra bare `StreamHandler()` added to the `uvicorn` logger, which
already had its own handler. The fix:

1. Application logs now use a dedicated `"antigravity"` logger (not `"uvicorn"`)
2. `propagate = False` prevents log bubbling to the root logger
3. A single `StreamHandler` with a proper formatter is used

If you still see duplicates, ensure you're running the latest `main.py`.

### Logs Growing Unbounded

**Symptom:** `~/Library/Logs/com.antigravity.proxy.err` grows to hundreds of MB.

1. **Verify newsyslog is installed:**

   ```sh
   cat /etc/newsyslog.d/com.antigravity.proxy.conf
   
   ```

   If missing, install it (see [Step 5](#step-5--install-log-rotation)).

2. **Trigger rotation immediately:**

   ```sh
   sudo newsyslog -v
   
   ```

3. **Truncate now, rotate later:**

   ```sh
   : > ~/Library/Logs/com.antigravity.proxy.err

   ```

---

## Uninstallation

To completely remove the service and all its artifacts:

```sh
# 1. Unload the service
launchctl unload ~/Library/LaunchAgents/com.antigravity.proxy.plist

# 2. Remove the plist
rm ~/Library/LaunchAgents/com.antigravity.proxy.plist

# 3. Remove launchd log files
rm -f ~/Library/Logs/com.antigravity.proxy.log*
rm -f ~/Library/Logs/com.antigravity.proxy.err*

# 4. Remove newsyslog config
sudo rm -f /etc/newsyslog.d/com.antigravity.proxy.conf

# 5. (Optional) Remove application logs
rm -f logs/proxy.log*

```

The project directory itself is untouched — you can still run the proxy manually
with `uv run uvicorn main:APP --port 8888` at any time.

---

## Bugs Fixed in This Version

This section documents every issue found during the audit of the original
service and the fixes applied.

### 1. Massive TG Listener Error Spam (Critical)

**Root cause:** The `httpx.AsyncClient()` default read timeout is 5 seconds, but
the Telegram `getUpdates` endpoint uses long-polling with `timeout=20` (a query
parameter). The HTTP client aborted every request after 5 seconds, raising a
`ReadTimeout` exception — which then had an empty string representation, making
the error message `TG Listener Error:` with no useful detail.

This produced **6,656 empty error lines** in 2 days, at 10-second intervals
(5s timeout + 5s sleep).

**Fixes applied:**

| Change | File | Detail |
|---|---|---|
| Set `httpx.AsyncClient(timeout=30)` | `main.py` L170 | Exceeds the 20s long-poll window |
| Added exponential backoff | `main.py` L198–200 | 5s → 10s → … → 300s cap on errors |
| Used `repr(e)` in error log | `main.py` L197 | Shows `ReadTimeout('')` instead of blank |
| Added Telegram API response validation | `main.py` L173–174 | Raises descriptive error on `ok: false` |
| Guarded with `TG_ENABLED` flag | `main.py` L161–163 | Skips entirely when unconfigured |
| Wrapped `send_alert()` in try/except | `main.py` L148–156 | Telegram failures never crash the proxy |

### 2. Duplicate Log Lines (Moderate)

**Root cause:** The code called `log.addHandler(logging.StreamHandler())` on the
`"uvicorn"` logger, which already had its own `StreamHandler` configured by
uvicorn at startup. Every message was written to stderr twice — once by
uvicorn's handler (with `INFO:` prefix) and once by the bare extra handler
(with no formatting).

**Fixes applied:**

| Change | Detail |
|---|---|
| Switched to `logging.getLogger("antigravity")` | Own logger, doesn't conflict with uvicorn |
| Set `propagate = False` | Prevents bubbling to root logger |
| Added proper `Formatter` to the `StreamHandler` | Consistent format with the file handler |

### 3. No Log Rotation for launchd Files (Moderate)

**Root cause:** The plist's `StandardErrorPath` and `StandardOutPath` are
plain files that launchd appends to indefinitely. The application's own
`RotatingFileHandler` only manages `logs/proxy.log` — it doesn't touch the
launchd files.

**Fix applied:** Created a `newsyslog` config at
`/etc/newsyslog.d/com.antigravity.proxy.conf` that rotates the stderr log at
1 MB and stdout log at 512 KB, keeping 3 bzip2-compressed backups each.

### 4. Hardcoded Secrets in Source Code (Moderate)

**Root cause:** The Telegram bot token and chat ID were hardcoded directly in
`main.py`, making them visible in version control.

**Fixes applied:**

| Change | Detail |
|---|---|
| Replaced hardcoded values with `os.environ.get()` | `main.py` L40–42 |
| Added `EnvironmentVariables` dict to the plist | Secrets live in the plist, not in source code |
| Template plist in repo uses empty strings | Safe to commit |

### 5. Plist Template Out of Date (Minor)

**Root cause:** The template at `service/LaunchAgents/...` used
`/usr/bin/python3 -m uvicorn` (system Python) and lacked `--host`, log paths,
and environment variables.

**Fix applied:** Updated the template to match the live configuration: uses
`/opt/homebrew/bin/uv run`, binds to `0.0.0.0`, includes `StandardOutPath`,
`StandardErrorPath`, and `EnvironmentVariables`.

---

## Configuration Tuning Reference

All tuneable constants in `main.py` and their effects:

| Constant | Default | Location | Description |
|---|---|---|---|
| `KEYS_FILE` | `"api_keys.txt"` | L24 | Path to the key file (relative to `WorkingDirectory`) |
| `UPSTREAM_BASE_GEMINI` | `https://generativelanguage.googleapis.com/v1beta` | L26 | Upstream API base URL |
| `BACKOFF_MIN` | `5` | L33 | Seconds to cool down a key after a non-429 failure |
| `BACKOFF_MAX` | `600` | L34 | Maximum backoff ceiling (currently unused in code; reserved) |
| `COOLDOWN_PERIOD` | `60` | L35 | Seconds to cool down a key after a 429 rate-limit response |
| `MAX_REQ_PER_KEY` | `15` | L36 | Requests per key before proactive rotation to the next key |
| `LOG_DIR` | `"logs"` | L30 | Directory for application log files |
| `LOG_FILE` | `"logs/proxy.log"` | L31 | Application log file path |
| `RotatingFileHandler maxBytes` | `5 MB` | L67 | Max size before the app log file is rotated |
| `RotatingFileHandler backupCount` | `3` | L67 | Number of rotated backup files to retain |
| `httpx.AsyncClient timeout` (proxy) | `300` | L283 | Timeout in seconds for upstream Gemini API requests |
| `httpx.AsyncClient timeout` (Telegram) | `30` | L170 | Timeout for Telegram API calls (must exceed long-poll) |
| `TG long-poll timeout` | `20` | L171 | Telegram `getUpdates` long-poll duration (query parameter) |
| `TG backoff max` | `300` | L167 | Maximum retry delay for Telegram errors (5 minutes) |
| `TG alert rate limit` | `600` | L149 | Minimum seconds between Telegram alert messages |

### Scaling Tips

- **More keys = higher throughput.** With `MAX_REQ_PER_KEY=15` and 10 keys, you
  get 150 requests before any cooldowns.

- **Lower `MAX_REQ_PER_KEY`** if you want more even distribution across keys.
  Set it to `5` for aggressive rotation.

- **Raise `COOLDOWN_PERIOD`** if Google is returning 429s faster than keys
  recover. The default 60 seconds matches most free-tier reset windows.

- **Raise the proxy timeout** (`300`) if you're sending large prompts or using
  long-context models that take more than 5 minutes to respond.

---

## Security Considerations

### Secrets Management

| Secret | Storage | Risk |
|---|---|---|
| Gemini API keys | `api_keys.txt` (gitignored) | File is excluded from version control. Protect with `chmod 600 api_keys.txt`. |
| Telegram bot token | Plist `EnvironmentVariables` | The **live** plist at `~/Library/LaunchAgents/` is not in the repo. The **template** in `service/` uses empty strings. Never commit real tokens. |
| `ADMIN_TOKEN` | Hardcoded in `main.py` | Currently set to `changeme_local_only`. Only relevant if admin endpoints are exposed; safe for local-only use. |

### Network Exposure

The plist configures `--host 0.0.0.0`, meaning the proxy listens on **all
interfaces**, not just localhost. This is convenient for accessing the proxy from
other devices on your LAN (e.g., a VM or container), but it means anyone on your
network can use your API keys.

**To restrict to localhost only**, change the plist:

```xml
<string>--host</string>
<string>127.0.0.1</string>

```

### File Permissions

Recommended permissions for sensitive files:

```sh
chmod 600 api_keys.txt
chmod 644 ~/Library/LaunchAgents/com.antigravity.proxy.plist
chmod 700 logs/

```

The plist needs to be readable by launchd (which runs as your user), so `644`
is appropriate. The API keys file should be owner-read-only.

---

*Last updated: 2026-02-18 — Reflects all audit fixes applied to the original service.*
