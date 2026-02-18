# 010 — Antigravity Proxy Enhancements: Implementation Plan

> A comprehensive, step-by-step implementation plan for all recommended
> improvements to the Gemini API Key Rotator Proxy, covering bug fixes,
> infrastructure upgrades, Telegram bot enhancements, HTTP endpoints,
> dashboard improvements, and operational hardening.

---

## Table of Contents

- [Executive Summary](#executive-summary)
- [Scope & Inventory](#scope--inventory)
- [Architecture Decisions](#architecture-decisions)
- [Dependency Graph](#dependency-graph)
- [Phase 1 — Bug Fixes](#phase-1--bug-fixes)
  - [1.1 Fix path normalisation](#11-fix-path-normalisation)
  - [1.2 Remove dead internal routes](#12-remove-dead-internal-routes)
  - [1.3 Fix streaming client lifecycle](#13-fix-streaming-client-lifecycle)
- [Phase 2 — Infrastructure Foundations](#phase-2--infrastructure-foundations)
  - [2.1 Add Metrics class](#21-add-metrics-class)
  - [2.2 Migrate to lifespan + shared httpx client](#22-migrate-to-lifespan--shared-httpx-client)
  - [2.3 Per-topic alert rate limiting](#23-per-topic-alert-rate-limiting)
  - [2.4 Graceful shutdown](#24-graceful-shutdown)
  - [2.5 Telegram command router refactor](#25-telegram-command-router-refactor)
- [Phase 3 — Telegram Bot Enhancements](#phase-3--telegram-bot-enhancements)
  - [3.1 /help command](#31-help-command)
  - [3.2 /rotate command](#32-rotate-command)
  - [3.3 /ban command](#33-ban-keypreview-duration-command)
  - [3.4 /unban command](#34-unban-command)
  - [3.5 /uptime command](#35-uptime-command)
  - [3.6 /config command](#36-config-command)
  - [3.7 /digest command](#37-digest-command)
  - [3.8 Per-key failure notifications](#38-per-key-failure-notifications)
  - [3.9 Pool health threshold alerts](#39-pool-health-threshold-alerts)
- [Phase 4 — HTTP Endpoints & Security](#phase-4--http-endpoints--security)
  - [4.1 /status endpoint](#41-status-endpoint-json)
  - [4.2 /reload-keys endpoint](#42-reload-keys-endpoint)
  - [4.3 /health endpoint](#43-health-endpoint)
  - [4.4 Protect internal endpoints](#44-protect-internal-endpoints)
  - [4.5 .env file support](#45-env-file-support)
- [Phase 5 — Dashboard Enhancements](#phase-5--dashboard-enhancements)
  - [5.1 Key pool status table](#51-key-pool-status-table)
  - [5.2 Pool status SSE feed](#52-pool-status-sse-feed)
  - [5.3 Request metrics summary bar](#53-request-metrics-summary-bar)
- [Phase 6 — Validation & Rollout](#phase-6--validation--rollout)
  - [6.1 Smoke tests per feature](#61-smoke-tests-per-feature)
  - [6.2 Streaming verification](#62-streaming-verification)
  - [6.3 Concurrent load test](#63-concurrent-load-test)
  - [6.4 launchd reload test](#64-launchd-reload-test)
  - [6.5 Documentation update](#65-documentation-update)
- [Appendix A — New Environment Variables](#appendix-a--new-environment-variables)
- [Appendix B — Updated requirements.txt](#appendix-b--updated-requirementstxt)
- [Appendix C — File Change Summary](#appendix-c--file-change-summary)

---

## Executive Summary

This plan delivers **25 discrete improvements** across 6 phases, transforming
the Antigravity Proxy from a functional but fragile single-feature proxy into a
robust, observable, remotely manageable service. The highest-value changes are:
fixing a latent streaming bug that will eventually cause client errors, adding
8 new Telegram bot commands (`/help`, `/rotate`, `/ban`, `/unban`, `/uptime`,
`/config`, `/digest`, plus inline-button variants), implementing intelligent
per-key failure notifications with storm protection, and exposing proper
`/status`, `/health`, and `/reload-keys` HTTP endpoints. Every phase is
independently deployable — you can stop after any phase and have a working,
improved system.

---

## Scope & Inventory

All 25 changes, grouped by category:

### Bug Fixes (3)
| # | Change &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | Severity |
| --- | --- | --- |
| 1 | Fix path normalisation (`.replace` → `.removeprefix`) | Moderate |
| 2 | Remove dead internal routes (`/status`, `/reload-keys` → 404) | Minor |
| 3 | Fix streaming client lifecycle (httpx client closed before stream consumed) | Critical |

### Infrastructure (5)
| # | Change | Impact |
| --- | --- | --- |
| 4 | Add `Metrics` class (counters, uptime, benching history) | Foundation |
| 5 | Migrate to FastAPI lifespan + shared `httpx.AsyncClient` | Foundation |
| 6 | Per-topic Telegram alert rate limiting | Foundation |
| 7 | Graceful shutdown (cancel tasks, close clients) | Resilience |
| 8 | Telegram command router refactor (dispatcher dict) | Maintainability |

### Telegram Bot (9)
| # | Change | Type |
| --- | --- | --- |
| 9 | `/help` command | New command |
| 10 | `/rotate` command (reset all counters/cooldowns) | New command |
| 11 | `/ban <key> [duration]` command | New command |
| 12 | `/unban` command | New command |
| 13 | `/uptime` command | New command |
| 14 | `/config` command | New command |
| 15 | `/digest` command (on-demand summary) | New command |
| 16 | Per-key failure notifications (429, 4xx, 5xx) | New notification |
| 17 | Pool health threshold alerts (>50% benched) | New notification |

### HTTP Endpoints (5)
| # | Change | Type |
| --- | --- | --- |
| 18 | `/status` JSON endpoint | New endpoint |
| 19 | `/reload-keys` hot-reload endpoint | New endpoint |
| 20 | `/health` health-check endpoint (200/503) | New endpoint |
| 21 | Protect internal endpoints (localhost OR ADMIN_TOKEN) | Security |
| 22 | `.env` file support (python-dotenv) | Configuration |

### Dashboard (3)
| # | Change | Type |
| --- | --- | --- |
| 23 | Key pool status table in dashboard | UI |
| 24 | Pool status SSE feed (`/pool-status`) | Data |
| 25 | Request metrics summary bar | UI |

---

## Architecture Decisions

Key design decisions made during brainstorming, with alternatives considered
and rationale for the chosen approach.

### AD-1: Single shared httpx client vs. separate proxy + TG clients

| Option | Pros | Cons |
| --- | --- | --- |
| **A: Single client** (timeout=300, per-request override for TG) | Simple, one lifecycle | Shared connection pool |
| B: Two clients (proxy=300s, TG=30s) | Isolated pools | Two things to lifecycle-manage |

**Decision: Option A.** httpx supports per-request `timeout=` overrides. TG uses
1 connection; proxy uses 20-30 at peak. The default pool of 100 is plenty. One
client to create, one to close.

### AD-2: /ban duration semantics

| Option | UX |
| --- | --- |
| A: Fixed 24h ban | Simple but inflexible |
| **B: `/ban <key> [seconds]`** with 24h default | Flexible, good defaults |
| C: Ban until /unban | Dangerous — easy to forget |

**Decision: Option B.** `/ban AIzaSy` → 24h. `/ban AIzaSy 3600` → 1 hour.

### AD-3: Notification storm protection

| Option | Strategy |
| --- | --- |
| A: Global max 5 notifications per minute | Simple cap |
| B: If >2 keys fail within 10s, send summary | Smart batching |
| **C: Per-key 5-min cooldown + global 5/min cap** | Layered protection |

**Decision: Option C.** Two layers: each key can only trigger one notification
per 5 minutes, AND a global cap of 5 notifications per minute across all keys.
Prevents both per-key spam and mass-failure floods.

### AD-4: Metrics storage

| Option | Access pattern |
| --- | --- |
| **A: Module-level global `METRICS`** | Simple, accessible everywhere |
| B: `app.state.metrics` | Cleaner, but needs request context |
| C: Inside `KeyPool` | Tight coupling |

**Decision: Option A.** Same pattern as `POOL` and `TG`. Accessed from route
handlers, TG commands, dashboard — a global is simplest.

### AD-5: Internal endpoint protection

| Option | Strategy |
| --- | --- |
| A: Require ADMIN_TOKEN header always | Annoying for local dev |
| B: Localhost-only | Can't access remotely |
| **C: Allow localhost OR valid ADMIN_TOKEN** | Best of both worlds |

**Decision: Option C.** `curl http://127.0.0.1:8888/status` works without a
token. Remote access requires `Authorization: Bearer <ADMIN_TOKEN>` header.

### AD-6: .env file loading

| Option | Dependency |
| --- | --- |
| **A: python-dotenv** | Standard, 1 tiny package |
| B: Custom .env parser | No dependency, but fragile |
| C: Skip it | Plist-only; manual runs are harder |

**Decision: Option A.** `python-dotenv` is the ecosystem standard. One line at
top of `main.py`: `load_dotenv()`. Works in launchd (ignored, plist provides
env) AND manual runs (`.env` provides env). Add to `requirements.txt`.

### AD-7: Dashboard architecture

| Option | Approach |
| --- | --- |
| **A: Inline HTML/JS in Python string** | Zero dependencies, one file |
| B: Separate static files | Needs static file serving |
| C: Jinja2 templates | Overkill for a dev tool |

**Decision: Option A.** Keep everything in `main.py`. The dashboard is a
single-page developer tool, not a production UI. Inline HTML keeps deployment
to a single file with zero static assets.

---

## Dependency Graph

Shows what must be completed before each item can begin.

```log
Phase 1 (Bug Fixes) ─── no dependencies, can start immediately
  1.1 Path normalisation .............. standalone
  1.2 Dead routes ..................... standalone
  1.3 Streaming fix .................. ⤷ superseded by 2.2 (shared client)

Phase 2 (Infrastructure) ─── after Phase 1
  2.1 Metrics class .................. standalone
  2.2 Lifespan + shared client ....... depends on 2.1 (wires metrics)
                                       ⤷ also fixes 1.3 as side effect
  2.3 Per-topic rate limiting ........ standalone
  2.4 Graceful shutdown .............. depends on 2.2 (lifespan)
  2.5 Command router refactor ........ depends on 2.3 (alert helpers)

Phase 3 (Telegram) ─── after Phase 2
  3.1 /help .......................... depends on 2.5 (router)
  3.2 /rotate ........................ depends on 2.5
  3.3 /ban ........................... depends on 2.5
  3.4 /unban ......................... depends on 2.5
  3.5 /uptime ........................ depends on 2.1 (metrics) + 2.5
  3.6 /config ........................ depends on 2.5
  3.7 /digest ........................ depends on 2.1 (metrics) + 2.5
  3.8 Per-key failure notifications .. depends on 2.1 + 2.3 + 2.5
  3.9 Pool health alerts ............. depends on 3.8

Phase 4 (HTTP) ─── after Phase 2
  4.1 /status ........................ depends on 2.1 (metrics)
  4.2 /reload-keys ................... standalone after Phase 1
  4.3 /health ........................ standalone after Phase 1
  4.4 Protect endpoints .............. depends on 4.1-4.3 existing
  4.5 .env support ................... standalone (can do any time)

Phase 5 (Dashboard) ─── after Phase 4
  5.1 Pool status table .............. depends on 5.2 (SSE feed)
  5.2 Pool status SSE ................ depends on 2.1 (metrics)
  5.3 Metrics summary bar ............ depends on 2.1 + 5.2

```

---

## Phase 1 — Bug Fixes

> **Goal:** Fix correctness issues before adding features.
> **Risk:** Low — minimal code changes, no new behaviour.
> **Rollback:** `git checkout main.py`

### 1.1 Fix path normalisation

**What:** Replace global `.replace()` with prefix-only stripping.

**Why:** `.replace("v1/", "")` strips `v1/` from *anywhere* in the path, not
just the prefix. A path like `/models/v1/something` would incorrectly become
`/models/something`.

**Where:** `main.py` line 249.

**Current code:**

```python
clean_path = path.replace("v1/", "").replace("v1beta/", "").lstrip("/").rstrip("/")

```

**New code:**

```python
clean_path = path.lstrip("/")
clean_path = clean_path.removeprefix("v1beta/").removeprefix("v1/")
clean_path = clean_path.strip("/")

```

**Requires:** Python 3.9+ (`str.removeprefix`). Already satisfied — project
uses Python 3.10+.

**Test:**

```sh
# Should all proxy to the same upstream path:
curl http://127.0.0.1:8888/v1beta/models
curl http://127.0.0.1:8888/v1/models
curl http://127.0.0.1:8888/models

```

**Risk:** None. Gemini API paths never have `v1/` or `v1beta/` mid-path.

---

### 1.2 Remove dead internal routes

**What:** Remove `"status"` and `"reload-keys"` from the internal route list.
They will be re-added when implemented in Phase 4.

**Why:** These routes are checked in the routing guard but `handle_internal()`
doesn't handle them — they silently return 404. This is confusing: a request to
`/status` gets intercepted as "internal" but then 404s, instead of being
proxied to Google (which would also fail, but at least the behaviour would be
consistent).

**Where:** `main.py` line 252.

**Current code:**

```python
if clean_path in ("dashboard", "logs", "status", "reload-keys"):
    return await handle_internal(request, clean_path)

```

**New code:**

```python
if clean_path in ("dashboard", "logs"):
    return await handle_internal(request, clean_path)

```

**Note:** Phase 4 will add `"status"`, `"reload-keys"`, and `"health"` back,
with actual implementations.

**Test:**

```sh
curl http://127.0.0.1:8888/dashboard  # → 200 (unchanged)
curl http://127.0.0.1:8888/status     # → now proxied to Google (will fail, but not 404)

```

**Risk:** None.

---

### 1.3 Fix streaming client lifecycle

**What:** Prevent `httpx.AsyncClient` from being closed before a streaming
response is fully consumed.

**Why:** The current code creates an `AsyncClient` inside an `async with` block,
defines a `stream_wrapper()` generator that uses that client, then `return`s a
`StreamingResponse` wrapping the generator. When `return` executes, the
`async with` exits and the client is closed. But Starlette reads from the
generator *after* the route handler returns — using a closed client.

**Where:** `main.py` lines 269–279.

**Current code (simplified):**

```python
async with httpx.AsyncClient(timeout=300) as client:
    if is_stream:
        async def stream_wrapper():
            async with client.stream(...) as r:  # client is CLOSED here
                async for chunk in r.aiter_bytes():
                    yield chunk
        return StreamingResponse(stream_wrapper(), ...)

```

**Interim fix (standalone, before Phase 2):**

Move client creation inside the generator:

```python
if is_stream:
    async def stream_wrapper():
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream(request.method, upstream_url,
                                     headers=h, params=p, content=body) as r:
                if r.status_code >= 400:
                    key_state.mark_failure(r.status_code)
                    yield f"data: {json.dumps({'error': 'stream error'})}\n\n".encode()
                    return
                key_state.mark_success()
                async for chunk in r.aiter_bytes():
                    yield chunk
    return StreamingResponse(stream_wrapper(), media_type="text/event-stream")

```

**Permanent fix (Phase 2.2):** The shared app-level `httpx.AsyncClient` is
never closed during request processing — only during app shutdown. This
eliminates the bug entirely without needing to restructure the generator. Step
2.2 supersedes this interim fix.

**Test:**

```sh
# Stream a response and verify it completes without "Client is closed" errors:
curl -N "http://127.0.0.1:8888/v1beta/models/gemini-2.5-flash:generateContent?alt=sse" \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"parts":[{"text":"Say hello in one word"}]}]}'

```

**Risk:** Low. The interim fix creates a client per streaming request (slightly
less efficient), but correct. Phase 2.2 resolves the efficiency concern.

---

## Phase 2 — Infrastructure Foundations

> **Goal:** Build the scaffolding that all subsequent features depend on.
> **Risk:** Medium — architectural changes to client lifecycle and startup.
> **Rollback:** Revert `main.py` to Phase 1 state, reload service.

### 2.1 Add Metrics class

**What:** Create a `Metrics` class to track request counters, uptime, and
recent key-benching events.

**Why:** Required by `/uptime`, `/digest`, `/status`, pool health alerts,
and dashboard enhancements. Must exist before those features can be built.

**Where:** `main.py` — new class, insert after `POOL` definition (after line
~130). Instantiate as module-level `METRICS = Metrics()`.

**Code:**

```python
class Metrics:
    def __init__(self):
        self.start_time = time.time()
        self.total_requests = 0
        self.total_success = 0
        self.total_fail = 0
        self.total_429 = 0
        self.rotations = 0
        self.benchings: List[Dict[str, Any]] = []

    def record_success(self):
        self.total_requests += 1
        self.total_success += 1

    def record_failure(self, status_code: int):
        self.total_requests += 1
        self.total_fail += 1
        if status_code == 429:
            self.total_429 += 1

    def record_benching(self, key_preview: str, status_code: int, duration: float):
        self.benchings.append({
            "time": time.time(),
            "key": key_preview,
            "status": status_code,
            "duration": duration,
        })
        # Keep only the last 100 events to bound memory
        if len(self.benchings) > 100:
            self.benchings = self.benchings[-100:]

    def record_rotation(self):
        self.rotations += 1

    def uptime(self) -> float:
        return time.time() - self.start_time

    def uptime_str(self) -> str:
        s = int(self.uptime())
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        parts = []
        if d: parts.append(f"{d}d")
        if h: parts.append(f"{h}h")
        if m: parts.append(f"{m}m")
        parts.append(f"{s}s")
        return " ".join(parts)

    def summary(self) -> Dict[str, Any]:
        total = max(self.total_requests, 1)
        return {
            "uptime_seconds": round(self.uptime(), 1),
            "uptime_human": self.uptime_str(),
            "total_requests": self.total_requests,
            "total_success": self.total_success,
            "total_fail": self.total_fail,
            "total_429": self.total_429,
            "success_rate_pct": round(self.total_success / total * 100, 1),
            "rotations": self.rotations,
            "recent_benchings": self.benchings[-10:],
        }

METRICS = Metrics()

```

**Wire into `catch_all()` (line ~280–290):**

```python
# After key_state.mark_success():
METRICS.record_success()

# After key_state.mark_failure(r.status_code):
METRICS.record_failure(r.status_code)
benching_dur = COOLDOWN_PERIOD if r.status_code == 429 else BACKOFF_MIN
METRICS.record_benching(key_state.key[:12] + "...", r.status_code, benching_dur)

```

**Note on thread safety:** In asyncio's single-threaded event loop, simple
`+= 1` operations are atomic. No lock needed for counter increments. The
`benchings` list append is also safe since Python's GIL protects list.append.

**Test:** Call `/status` (Phase 4) or add a temporary `print(METRICS.summary())`
in `catch_all` after a few requests.

**Risk:** None. Purely additive.

---

### 2.2 Migrate to lifespan + shared httpx client

**What:** Replace the deprecated `@APP.on_event("startup")` pattern with
FastAPI's modern `lifespan` context manager. Create a single shared
`httpx.AsyncClient` that lives for the entire application lifetime.

**Why:**
1. `@APP.on_event("startup")` is deprecated in FastAPI ≥0.109
2. A per-request `httpx.AsyncClient` wastes resources: every request creates a
   new client, opens a new TLS connection, and tears it down. A shared client
   reuses connections (HTTP/2 multiplexing, connection pooling).
3. **Fixes Bug 1.3:** The shared client is never closed during request
   processing, so the streaming generator always has a live client.

**Where:** `main.py` — replace lines 209–228 (APP definition + startup event)
and lines 269–280 (per-request client creation in `catch_all`).

**New imports (top of file):**

```python
from contextlib import asynccontextmanager

```

**New lifespan (replaces APP definition + startup event):**

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    app.state.tg_task = None
    if TG_ENABLED:
        app.state.tg_task = asyncio.create_task(TG.handle_commands())
        log.info("Telegram listener started")
    else:
        log.info("Telegram integration disabled (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set)")

    log.info(f"{PROJECT_NAME} started — {len(POOL.states)} keys loaded")

    yield  # ── App is running ──

    # ── Shutdown ──
    if app.state.tg_task:
        app.state.tg_task.cancel()
        try:
            await app.state.tg_task
        except asyncio.CancelledError:
            pass
    await app.state.http_client.aclose()
    log.info(f"{PROJECT_NAME} shutdown complete")


APP = FastAPI(title="Native Gemini Proxy (Fully Loaded)", lifespan=lifespan)

```

**Delete the old startup event:**

```python
# DELETE these lines:
@APP.on_event("startup")
async def startup_event():
    if TG_ENABLED:
        asyncio.create_task(TG.handle_commands())
    else:
        log.info("Telegram integration disabled ...")

```

**Update `catch_all()` to use the shared client:**

```python
# BEFORE (creates client per request):
async with httpx.AsyncClient(timeout=300) as client:
    ...

# AFTER (uses shared client):
client = request.app.state.http_client
if is_stream:
    async def stream_wrapper():
        async with client.stream(request.method, upstream_url,
                                 headers=h, params=p, content=body) as r:
            if r.status_code >= 400:
                key_state.mark_failure(r.status_code)
                yield f"data: {json.dumps({'error': 'stream error'})}\n\n".encode()
                return
            key_state.mark_success()
            METRICS.record_success()
            async for chunk in r.aiter_bytes():
                yield chunk
    return StreamingResponse(stream_wrapper(), media_type="text/event-stream")
else:
    r = await client.request(request.method, upstream_url,
                             headers=h, params=p, content=body)
    ...

```

**Update `TelegramManager` methods to use the shared client:**

The TG `handle_commands` loop currently creates a new `httpx.AsyncClient` per
poll cycle. It should receive the shared client or create its own long-lived
client. Since TG needs a different timeout (30s for long-polling) and the
shared client has 300s, use per-request timeout override:

```python
# In handle_commands(), change:
async with httpx.AsyncClient(timeout=30) as client:
    resp = await client.get(...)

# To:
resp = await client.get(..., timeout=30)

```

But `handle_commands` runs as a background task, not within a request context,
so it can't access `request.app.state`. Instead, pass the client at startup:

```python
# In lifespan:
app.state.tg_task = asyncio.create_task(
    TG.handle_commands(app.state.http_client)
)

# In TelegramManager.handle_commands:
async def handle_commands(self, client: httpx.AsyncClient):
    ...
    while True:
        try:
            resp = await client.get(
                f"{self.base_url}/getUpdates",
                params={"offset": offset, "timeout": 20},
                timeout=30,
            )
            ...

```

Similarly update `send_alert` to accept an optional client parameter with
fallback to creating a short-lived one (for calls from `catch_all` where we
can pass `request.app.state.http_client`):

```python
async def send_alert(self, text: str, topic: str = "general",
                     client: Optional[httpx.AsyncClient] = None):
    ...
    _client = client or httpx.AsyncClient()
    try:
        await _client.post(f"{self.base_url}/sendMessage", json=payload)
    finally:
        if not client:
            await _client.aclose()

```

**Test:**

```sh
# 1. Verify startup message in logs
tail -f ~/Library/Logs/com.antigravity.proxy.err

# 2. Verify proxy still works
curl http://127.0.0.1:8888/dashboard

# 3. Verify streaming works (the critical bug fix)
curl -N "http://127.0.0.1:8888/v1beta/models/gemini-2.5-flash:generateContent?alt=sse" \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"parts":[{"text":"Count to 5"}]}]}'

# 4. Verify graceful shutdown
launchctl unload ~/Library/LaunchAgents/com.antigravity.proxy.plist
# Check logs for "shutdown complete" message

```

**Risk:** Medium. This is the most invasive change. If something breaks, the
service won't start (but `KeepAlive` will keep trying). Mitigation: test
manually with `uv run uvicorn main:APP --port 8888` before reloading the
launch agent.

---

### 2.3 Per-topic alert rate limiting

**What:** Replace the single `last_notify_time` timestamp with a per-topic
cooldown dictionary and a global rate limiter.

**Why:** Currently `send_alert()` has one 600-second cooldown for ALL alerts.
If "all keys exhausted" fires, it suppresses every other alert type for 10
minutes. With per-key failure notifications (Phase 3.8), we need independent
rate limits per event type.

**Where:** `main.py` — `TelegramManager.__init__` and `send_alert`.

**Code:**

```python
class TelegramManager:
    def __init__(self, token, chat_id):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        # Per-topic cooldowns: topic_name → last_send_timestamp
        self.alert_cooldowns: Dict[str, float] = {}
        # Global rate limiter: list of send timestamps (last minute)
        self.global_sends: List[float] = []
        self.global_max_per_minute = 5

    def _can_send(self, topic: str, cooldown: int = 600) -> bool:
        """Check both per-topic cooldown and global rate limit."""
        now = time.time()
        # Per-topic check
        if now - self.alert_cooldowns.get(topic, 0) < cooldown:
            return False
        # Global rate limit: max N sends per minute
        self.global_sends = [t for t in self.global_sends if now - t < 60]
        if len(self.global_sends) >= self.global_max_per_minute:
            return False
        return True

    def _mark_sent(self, topic: str):
        """Record that a notification was sent."""
        now = time.time()
        self.alert_cooldowns[topic] = now
        self.global_sends.append(now)

    async def send_alert(self, text: str, topic: str = "general",
                         cooldown: int = 600,
                         client: Optional[httpx.AsyncClient] = None):
        if not TG_ENABLED:
            return
        if not self._can_send(topic, cooldown):
            return
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": f"🚨 *{PROJECT_NAME}*\n{text}",
                "parse_mode": "Markdown",
            }
            _client = client or httpx.AsyncClient()
            try:
                await _client.post(f"{self.base_url}/sendMessage", json=payload)
                self._mark_sent(topic)
            finally:
                if not client:
                    await _client.aclose()
        except Exception as e:
            log.warning(f"TG send_alert failed: {repr(e)}")

```

**Update existing `send_alert` call sites** to include a topic:

```python
# In catch_all, when all keys exhausted:
asyncio.create_task(TG.send_alert("All keys exhausted!", topic="exhausted"))

```

**Test:** Trigger two different alert types within 10 minutes and verify both
are sent (previously the second would be suppressed).

**Risk:** Low. Backward-compatible — existing calls without `topic` default to
`"general"`.

---

### 2.4 Graceful shutdown

**What:** Ensure the TG listener task is cancelled and the shared httpx client
is closed when the service shuts down.

**Why:** Without this, `launchctl unload` sends SIGTERM, uvicorn stops, but
background tasks and network connections may leak. On macOS this is mostly
cosmetic (the OS cleans up), but it prevents log noise and is correct practice.

**Where:** Already implemented in the `lifespan` context manager from Step 2.2.
The `yield` → shutdown section handles:
1. Cancel the TG listener task
2. Await its `CancelledError`
3. Close the shared httpx client
4. Log the shutdown

**Additional step:** Handle `CancelledError` gracefully in `handle_commands`:

```python
async def handle_commands(self, client):
    ...
    while True:
        try:
            ...
        except asyncio.CancelledError:
            log.info("TG Listener shutting down")
            return  # exit cleanly
        except Exception as e:
            log.error(f"TG Listener Error: {repr(e)}")
            ...

```

**Test:**

```sh
launchctl unload ~/Library/LaunchAgents/com.antigravity.proxy.plist
grep "shutdown complete" ~/Library/Logs/com.antigravity.proxy.err
# Should see the graceful shutdown message

```

**Risk:** None.

---

### 2.5 Telegram command router refactor

**What:** Replace the flat `if/elif` command handling in `handle_commands` with
a dispatcher dictionary mapping command strings to handler methods.

**Why:** The current single `if` block (line ~183) handles both `/status` and
the `reload` callback in one tangled conditional. Adding 6+ new commands to
this would be unmaintainable. A dispatcher pattern is cleaner, extensible, and
self-documenting.

**Where:** `main.py` — inside `TelegramManager.handle_commands`.

**Code — add a `_send_message` helper:**

```python
async def _send_message(self, client: httpx.AsyncClient, text: str,
                        keyboard: Optional[Dict] = None):
    """Send a formatted Telegram message with optional inline keyboard."""
    payload: Dict[str, Any] = {
        "chat_id": self.chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if keyboard:
        payload["reply_markup"] = keyboard
    await client.post(f"{self.base_url}/sendMessage", json=payload,
                      timeout=10)

```

**Code — dispatcher in `handle_commands`:**

```python
# Command dispatch tables (defined inside handle_commands or as class attrs)
cmd_handlers = {
    "/help":    self._cmd_help,
    "/status":  self._cmd_status,
    "/rotate":  self._cmd_rotate,
    "/ban":     self._cmd_ban,
    "/unban":   self._cmd_unban,
    "/uptime":  self._cmd_uptime,
    "/config":  self._cmd_config,
    "/digest":  self._cmd_digest,
}
cb_handlers = {
    "reload":   self._cb_reload,
    "rotate":   self._cb_rotate,
    "unban":    self._cb_unban,
}

# Inside the update loop, replace the flat if-block:
for u in updates:
    offset = u["update_id"] + 1
    msg = u.get("message", {})
    cb = u.get("callback_query", {})
    cid = str(msg.get("chat", {}).get("id",
              cb.get("from", {}).get("id", "")))

    if cid != self.chat_id:
        continue

    # Handle callback queries (inline button presses)
    cb_data = cb.get("data", "")
    if cb_data and cb_data in cb_handlers:
        await cb_handlers[cb_data](client, cb)
        continue

    # Handle text commands
    text = msg.get("text", "").strip()
    cmd = text.split()[0].lower() if text else ""
    if cmd in cmd_handlers:
        await cmd_handlers[cmd](client, msg)

```

**Stub all handlers initially** (implemented in Phase 3):

```python
async def _cmd_help(self, client, msg):
    await self._send_message(client, "🤖 Commands: /help /status /rotate "
                                     "/ban /unban /uptime /config /digest")

async def _cmd_status(self, client, msg):
    report = f"📊 *{PROJECT_NAME} Status*\n" + \
        "\n".join(f"• `{k['key_preview']}`: {k['usage']} reqs, "
                  f"{k['success']}✓ {k['fail']}✗"
                  for k in POOL.status())
    kb = {"inline_keyboard": [
        [{"text": "🔄 Reload Keys", "callback_data": "reload"},
         {"text": "🔄 Rotate", "callback_data": "rotate"}],
        [{"text": "✅ Unban All", "callback_data": "unban"}],
    ]}
    await self._send_message(client, report, keyboard=kb)

async def _cb_reload(self, client, cb):
    global POOL
    POOL = KeyPool(load_keys_from_file(KEYS_FILE))
    await client.post(f"{self.base_url}/answerCallbackQuery",
                      json={"callback_query_id": cb["id"],
                            "text": "Keys Reloaded!"}, timeout=10)
    await self._cmd_status(client, cb)  # show updated status

# ... remaining handlers are stubs returning "Not implemented yet"
# They get real implementations in Phase 3

```

**Test:** Send `/help` to the bot, verify it responds with the command list.
Send `/status`, verify it shows key pool status with inline buttons.

**Risk:** Low. The refactor changes control flow but not behaviour for existing
commands.

---

## Phase 3 — Telegram Bot Enhancements

> **Goal:** Add all new Telegram commands and notification types.
> **Risk:** Low — purely additive features on the router from Phase 2.
> **Rollback:** Revert individual handler methods; router still works.

### 3.1 /help command

**What:** List all available bot commands with brief descriptions.

**Handler:**

```python
async def _cmd_help(self, client, msg):
    text = (
        f"🤖 *{PROJECT_NAME} Bot Commands*\n\n"
        "/help — Show this message\n"
        "/status — Key pool status with action buttons\n"
        "/rotate — Reset all usage counters and cooldowns\n"
        "/ban `<key>` `[seconds]` — Bench a key (default 24h)\n"
        "/unban — Clear all cooldowns\n"
        "/uptime — Proxy uptime and request stats\n"
        "/config — Show current configuration\n"
        "/digest — On-demand health summary\n"
    )
    await self._send_message(client, text)

```

**Test:** Send `/help` to the bot.

---

### 3.2 /rotate command

**What:** Reset all key usage counters and cooldowns, forcing a fresh
round-robin from scratch.

**Why:** Useful after editing `api_keys.txt`, or when you suspect a key is
stuck in an unexpected state.

**Handler:**

```python
async def _cmd_rotate(self, client, msg):
    async with POOL.lock:
        for s in POOL.states:
            s.usage_count = 0
            s.banned_until = 0.0
    METRICS.record_rotation()
    await self._send_message(client, "🔄 All keys reset — usage counters "
                                     "and cooldowns cleared.")
    await self._cmd_status(client, msg)

async def _cb_rotate(self, client, cb):
    await client.post(f"{self.base_url}/answerCallbackQuery",
                      json={"callback_query_id": cb["id"],
                            "text": "Keys Rotated!"}, timeout=10)
    await self._cmd_rotate(client, cb)

```

**Test:** Send `/rotate`, verify all keys show `usage: 0` and
`available_in: 0` in the subsequent status report.

---

### 3.3 /ban <key_preview> [duration] command

**What:** Manually bench a specific key for a given duration. Useful when you
know a key is about to be revoked or is producing bad results.

**Handler:**

```python
async def _cmd_ban(self, client, msg):
    parts = msg.get("text", "").split()
    if len(parts) < 2:
        await self._send_message(client,
            "Usage: `/ban <key_preview> [seconds]`\n"
            "Example: `/ban AIzaSyCDpw 3600` (ban for 1h)\n"
            "Default duration: 86400s (24h)")
        return

    preview = parts[1].rstrip(".")
    duration = int(parts[2]) if len(parts) > 2 else 86400

    for s in POOL.states:
        if s.key.startswith(preview) or s.key[:len(preview)] == preview:
            s.banned_until = time.monotonic() + duration
            dur_str = f"{duration}s"
            if duration >= 3600:
                dur_str = f"{duration // 3600}h {(duration % 3600) // 60}m"
            await self._send_message(client,
                f"🚫 Banned `{s.key[:12]}...` for {dur_str}")
            METRICS.record_benching(s.key[:12] + "...", 0, duration)
            return

    await self._send_message(client,
        f"❌ No key matching `{preview}` found.\n"
        f"Hint: use first few chars, e.g. `/ban AIzaSy`")

```

**Test:** Send `/ban AIzaSy 60`, then `/status` to verify the key shows a
60-second cooldown.

---

### 3.4 /unban command

**What:** Clear all cooldowns immediately, making every key available.

**Handler:**

```python
async def _cmd_unban(self, client, msg):
    async with POOL.lock:
        count = 0
        for s in POOL.states:
            if s.banned_until > time.monotonic():
                s.banned_until = 0.0
                count += 1
    if count:
        await self._send_message(client,
            f"✅ Cleared cooldowns on {count} key(s)")
    else:
        await self._send_message(client,
            "✅ No keys were in cooldown")
    await self._cmd_status(client, msg)

async def _cb_unban(self, client, cb):
    await client.post(f"{self.base_url}/answerCallbackQuery",
                      json={"callback_query_id": cb["id"],
                            "text": "Cooldowns Cleared!"}, timeout=10)
    await self._cmd_unban(client, cb)

```

**Test:** Ban a key, then `/unban`, then `/status` — all should show
`available_in: 0`.

---

### 3.5 /uptime command

**What:** Show proxy uptime, total requests, success rate, and key pool health
at a glance.

**Depends on:** Step 2.1 (Metrics class).

**Handler:**

```python
async def _cmd_uptime(self, client, msg):
    m = METRICS.summary()
    available = sum(1 for s in POOL.states if s.is_available())
    total_keys = len(POOL.states)
    text = (
        f"⏱ *{PROJECT_NAME} Uptime*\n\n"
        f"Uptime: `{m['uptime_human']}`\n"
        f"Requests: `{m['total_requests']:,}` "
        f"({m['total_success']:,}✓ {m['total_fail']:,}✗)\n"
        f"Success rate: `{m['success_rate_pct']}%`\n"
        f"429 events: `{m['total_429']}`\n"
        f"Key rotations: `{m['rotations']}`\n"
        f"Keys available: `{available}/{total_keys}`"
    )
    await self._send_message(client, text)

```

**Test:** Run a few requests through the proxy, then `/uptime`.

---

### 3.6 /config command

**What:** Display the current runtime configuration values.

**Handler:**

```python
async def _cmd_config(self, client, msg):
    text = (
        f"⚙️ *{PROJECT_NAME} Config*\n\n"
        f"Keys loaded: `{len(POOL.states)}`\n"
        f"Max req/key: `{MAX_REQ_PER_KEY}`\n"
        f"Cooldown (429): `{COOLDOWN_PERIOD}s`\n"
        f"Backoff (other): `{BACKOFF_MIN}s`\n"
        f"Upstream timeout: `300s`\n"
        f"TG enabled: `{TG_ENABLED}`\n"
        f"Host: `0.0.0.0:8888`"
    )
    await self._send_message(client, text)

```

**Test:** Send `/config`.

---

### 3.7 /digest command

**What:** On-demand health summary combining uptime, key status, and recent
events into a single comprehensive report.

**Depends on:** Steps 2.1 (Metrics) and 2.5 (router).

**Handler:**

```python
async def _cmd_digest(self, client, msg):
    m = METRICS.summary()
    available = sum(1 for s in POOL.states if s.is_available())
    total_keys = len(POOL.states)

    lines = [
        f"📊 *{PROJECT_NAME} Digest*\n",
        f"Uptime: `{m['uptime_human']}`",
        f"Requests: `{m['total_requests']:,}` "
        f"({m['success_rate_pct']}% success)",
        f"429 events: `{m['total_429']}`",
        f"Keys: `{available}/{total_keys}` available\n",
    ]

    # Recent benching events
    recent = m.get("recent_benchings", [])
    if recent:
        lines.append("*Recent events:*")
        now = time.time()
        for ev in reversed(recent[-5:]):
            ago = int(now - ev["time"])
            if ago < 60:
                ago_str = f"{ago}s ago"
            elif ago < 3600:
                ago_str = f"{ago // 60}m ago"
            else:
                ago_str = f"{ago // 3600}h {(ago % 3600) // 60}m ago"
            lines.append(
                f"• {ago_str}: `{ev['key']}` benched {ev['duration']}s "
                f"(HTTP {ev['status']})")
    else:
        lines.append("_No recent events._")

    await self._send_message(client, "\n".join(lines))

```

**Test:** Run some requests (including ones that trigger 429s if possible),
then `/digest`.

---

### 3.8 Per-key failure notifications

**What:** Send a Telegram notification when a key is benched due to an
upstream failure (429, 403, 500, etc.). Each key can only trigger one
notification per 5 minutes, with a global cap of 5 per minute.

**Depends on:** Steps 2.1 (Metrics) and 2.3 (per-topic rate limiting).

**Why this matters:** This is the core "notify on rotation" feature. Normal
proactive rotation (usage counter) is silent — it's healthy behaviour. But
failure-triggered rotation (429, errors) means something is wrong and the
operator should know.

**Add a new method to `TelegramManager`:**

```python
async def notify_key_benched(self, key_preview: str, status_code: int,
                             duration: float,
                             client: Optional[httpx.AsyncClient] = None):
    """Notify when a key is benched due to upstream failure."""
    topic = f"bench_{key_preview}"
    cooldown = 300  # 5 minutes per key

    if status_code == 429:
        emoji = "🔴"
        reason = "rate limit"
    elif status_code in (401, 403):
        emoji = "❌"
        reason = "auth error"
        cooldown = 600  # 10 min for auth errors (less likely to resolve)
    else:
        emoji = "⚠️"
        reason = f"HTTP {status_code}"

    text = f"{emoji} Key `{key_preview}` benched for {int(duration)}s — {reason}"
    await self.send_alert(text, topic=topic, cooldown=cooldown, client=client)

```

**Wire into `catch_all()` — after every `mark_failure` call:**

```python
# Non-streaming failure:
key_state.mark_failure(r.status_code)
METRICS.record_failure(r.status_code)
benching_dur = COOLDOWN_PERIOD if r.status_code == 429 else BACKOFF_MIN
METRICS.record_benching(key_state.key[:12] + "...", r.status_code, benching_dur)
asyncio.create_task(TG.notify_key_benched(
    key_state.key[:12] + "...", r.status_code, benching_dur,
    client=request.app.state.http_client,
))

# Streaming failure (inside stream_wrapper):
key_state.mark_failure(r.status_code)
METRICS.record_failure(r.status_code)
benching_dur = COOLDOWN_PERIOD if r.status_code == 429 else BACKOFF_MIN
METRICS.record_benching(key_state.key[:12] + "...", r.status_code, benching_dur)
# Note: can't create_task from generator; use fire-and-forget
asyncio.get_event_loop().call_soon(
    lambda: asyncio.create_task(TG.notify_key_benched(
        key_state.key[:12] + "...", r.status_code, benching_dur)))

# Exception handler:
key_state.mark_failure(500)
METRICS.record_failure(500)
METRICS.record_benching(key_state.key[:12] + "...", 500, BACKOFF_MIN)
asyncio.create_task(TG.notify_key_benched(
    key_state.key[:12] + "...", 500, BACKOFF_MIN,
    client=request.app.state.http_client,
))
```

**Storm protection (from Step 2.3):** The `_can_send()` method enforces:
1. Per-key: max 1 notification per 5 minutes per key
2. Global: max 5 notifications per minute across all keys

If Google is down and all 5 keys fail simultaneously, you get 5 messages in
the first minute, then silence until keys recover and fail again.

**Test:**
1. Temporarily set `MAX_REQ_PER_KEY = 1` and make 2 rapid requests to force
   rotation and benching.
2. Verify Telegram receives a benching notification.
3. Make 2 more rapid requests — verify NO second notification (per-key cooldown).
4. Wait 5 minutes, repeat — verify notification fires again.

**Risk:** Low. Notifications are fire-and-forget (`create_task`); failures are
caught and logged as warnings. The proxy's core functionality is never affected
by TG notification failures.

---

### 3.9 Pool health threshold alerts

**What:** Send an alert when more than half the keys in the pool are
simultaneously benched.

**Depends on:** Step 3.8 (per-key notifications) — uses the same alert infra.

**Where:** Check pool health after every benching event.

**Add to `catch_all()`, after each `mark_failure` + `record_benching` block:**

```python
# Check pool health after benching
benched = sum(1 for s in POOL.states if not s.is_available())
total = len(POOL.states)
if benched > total // 2:
    asyncio.create_task(TG.send_alert(
        f"⚠️ Pool degraded: {benched}/{total} keys currently benched",
        topic="pool_health", cooldown=300,
        client=request.app.state.http_client,
    ))

```

**Test:** Ban 3 out of 5 keys using `/ban`, then make a request that triggers
a failure on a 4th key. Verify the pool health alert fires.

**Risk:** None.

---

## Phase 4 — HTTP Endpoints & Security

> **Goal:** Expose machine-readable status and control endpoints; secure them.
> **Risk:** Low — additive endpoints; security is opt-in.
> **Rollback:** Remove new routes from `handle_internal`.

### 4.1 /status endpoint (JSON)

**What:** Return a JSON object with pool status, metrics summary, and
configuration — the HTTP equivalent of `/status` on Telegram.

**Depends on:** Step 2.1 (Metrics).

**Where:** `main.py` — add to `handle_internal` and update the route guard.

**Update route guard (line ~252):**

```python
INTERNAL_ROUTES = {"dashboard", "logs", "status", "reload-keys", "health",
                   "pool-status"}

if clean_path in INTERNAL_ROUTES:
    return await handle_internal(request, clean_path)
```

**Add to `handle_internal`:**
```python
if path == "status":
    available = sum(1 for s in POOL.states if s.is_available())
    return JSONResponse({
        "project": PROJECT_NAME,
        "uptime": METRICS.uptime_str(),
        "keys": {
            "total": len(POOL.states),
            "available": available,
            "pool": POOL.status(),
        },
        "metrics": METRICS.summary(),
    })

```

**Test:**

```sh
curl -s http://127.0.0.1:8888/status | python3 -m json.tool

```

---

### 4.2 /reload-keys endpoint

**What:** Hot-reload `api_keys.txt` via an HTTP POST request without restarting
the service or using Telegram.

**Where:** Add to `handle_internal`.

**Code:**

```python
if path == "reload-keys":
    if request.method != "POST":
        return JSONResponse({"error": "POST required"}, status_code=405)
    try:
        new_keys = load_keys_from_file(KEYS_FILE)
        if not new_keys:
            return JSONResponse({"error": "keys file is empty"},
                                status_code=400)
        global POOL
        POOL = KeyPool(new_keys)
        log.info(f"Keys reloaded via HTTP: {len(new_keys)} keys")
        return JSONResponse({"status": "ok", "keys_loaded": len(new_keys)})
    except FileNotFoundError:
        return JSONResponse({"error": f"{KEYS_FILE} not found"},
                            status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

```

**Test:**

```sh
curl -X POST http://127.0.0.1:8888/reload-keys
# Expected: {"status": "ok", "keys_loaded": 5}

```

---

### 4.3 /health endpoint

**What:** A proper health-check endpoint that returns 200 when healthy and 503
when degraded (no keys available).

**Why:** The current `HEAD /*` catch-all always returns 200, even when all
keys are exhausted. Monitoring tools need a real health signal.

**Code:**
```python
if path == "health":
    available = sum(1 for s in POOL.states if s.is_available())
    total = len(POOL.states)
    healthy = available > 0
    return JSONResponse(
        {
            "status": "ok" if healthy else "degraded",
            "available_keys": available,
            "total_keys": total,
            "uptime": METRICS.uptime_str(),
        },
        status_code=200 if healthy else 503,
    )

```

**Test:**

```sh
curl -s -w "\nHTTP %{http_code}\n" http://127.0.0.1:8888/health
# Expected: {"status": "ok", ...}  HTTP 200

# After banning all keys:
curl -s -w "\nHTTP %{http_code}\n" http://127.0.0.1:8888/health
# Expected: {"status": "degraded", ...}  HTTP 503

```

---

### 4.4 Protect internal endpoints

**What:** Gate internal endpoints so that requests from non-localhost require
a valid `ADMIN_TOKEN` in the `Authorization` header.

**Why:** With `--host 0.0.0.0`, anyone on the network can view keys, reload
config, and access the dashboard. This adds a lightweight auth layer.

**Where:** Add a guard at the top of `handle_internal`.

**Code:**

```python
async def handle_internal(request: Request, path: str):
    # Allow localhost without auth; require token for remote access
    client_ip = request.client.host if request.client else "unknown"
    is_local = client_ip in ("127.0.0.1", "::1", "localhost")

    if not is_local:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {ADMIN_TOKEN}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    # ... existing handlers

```

**Test:**

```sh
# From localhost — no auth needed:
curl http://127.0.0.1:8888/status

# From another machine on the LAN — requires auth:
curl -H "Authorization: Bearer changeme_local_only" http://192.168.1.X:8888/status

# Without auth from LAN:
curl http://192.168.1.X:8888/status
# Expected: 401

```

**Risk:** Low. Localhost access is always allowed, preventing lockout.

---

### 4.5 .env file support

**What:** Add `python-dotenv` to load environment variables from a `.env` file
in the working directory at startup.

**Why:** Currently, secrets must be set via plist `EnvironmentVariables` (for
launchd) or shell exports (for manual runs). A `.env` file provides a single
source of truth that works in both contexts.

**Changes:**

1. **Add to `requirements.txt`:**

   ```log
   python-dotenv
   ```

2. **Add to top of `main.py` (after standard imports, before config section):**

```python
   try:
       from dotenv import load_dotenv
       load_dotenv()
   except ImportError:
       pass  # python-dotenv not installed; rely on system env vars

```

   The `try/except` makes it a soft dependency — the proxy still works without
   python-dotenv installed.

3. **Create `.env.example` in the project root:**

   ```ini
   # Antigravity Proxy — Environment Variables
   # Copy to .env and fill in your values.

   # Telegram Bot (optional — leave empty to disable)
   TELEGRAM_TOKEN=
   TELEGRAM_CHAT_ID=
   
   ```

4. **Add `.env` to `.gitignore`:**
   ```
   .env

   ```

**Test:**

```sh
# Create .env with test values
echo 'TELEGRAM_TOKEN=test123' > .env
echo 'TELEGRAM_CHAT_ID=test456' >> .env

# Run manually — should pick up the values
uv run python -c "
from dotenv import load_dotenv; load_dotenv()
import os; print(os.environ.get('TELEGRAM_TOKEN'))
"
# Expected: test123

# Clean up
rm .env

```

**Risk:** None. Purely additive. The `try/except` ensures no breakage if
python-dotenv is not installed.

---

## Phase 5 — Dashboard Enhancements

> **Goal:** Make the web dashboard a useful operational tool, not just a log tail.
> **Risk:** Low — purely UI changes; no proxy logic affected.
> **Rollback:** Revert `handle_internal` dashboard HTML.

### 5.1 Key pool status table

**What:** Add a live-updating HTML table above the log tail showing each key's
preview, usage count, success/fail counts, and cooldown status.

**Depends on:** Step 5.2 (SSE feed for pool data).

**Where:** Update the inline HTML in `handle_internal` `"dashboard"` handler.

**Sketch:**

```html
<div id="pool" style="margin-bottom:20px;">
  <h2>Key Pool</h2>
  <table style="width:100%;border-collapse:collapse;">
    <thead>
      <tr>
        <th>Key</th><th>Usage</th><th>Success</th>
        <th>Fail</th><th>Cooldown</th><th>Status</th>
      </tr>
    </thead>
    <tbody id="pool-body"></tbody>
  </table>
</div>

<script>
// Poll pool status every 3 seconds
setInterval(async () => {
  const resp = await fetch('/pool-status');
  const data = await resp.json();
  const tbody = document.getElementById('pool-body');
  tbody.innerHTML = data.keys.map(k => `
    <tr>
      <td><code>${k.key_preview}</code></td>
      <td>${k.usage}</td>
      <td style="color:#4ec9b0">${k.success}</td>
      <td style="color:#f44747">${k.fail}</td>
      <td>${k.available_in > 0 ? k.available_in + 's' : '-'}</td>
      <td>${k.available_in > 0 ? '🔴' : '🟢'}</td>
    </tr>
  `).join('');
}, 3000);
</script>

```

**Test:** Open `http://127.0.0.1:8888/dashboard` in a browser. The key table
should appear and update every 3 seconds.

---

### 5.2 Pool status SSE feed

**What:** Add a `/pool-status` endpoint that returns the current pool status
and metrics as JSON, polled by the dashboard's JavaScript.

**Why:** The dashboard needs a data source for the key pool table and metrics
bar. A simple JSON endpoint polled every few seconds is simpler and more
reliable than SSE for this use case (SSE is better for the log tail where
new data arrives unpredictably).

**Where:** Add to `handle_internal` and the `INTERNAL_ROUTES` set.

**Code:**

```python
if path == "pool-status":
    available = sum(1 for s in POOL.states if s.is_available())
    return JSONResponse({
        "keys": POOL.status(),
        "available": available,
        "total": len(POOL.states),
        "metrics": METRICS.summary(),
    })

```

**Test:**

```sh
curl -s http://127.0.0.1:8888/pool-status | python3 -m json.tool

```

**Risk:** None. Lightweight JSON serialisation on every poll. At 3-second
intervals from a single dashboard tab, this is negligible.

---

### 5.3 Request metrics summary bar

**What:** Add a summary bar between the key pool table and the log tail
showing total requests, success rate, 429 count, and uptime.

**Depends on:** Step 5.2 (the `/pool-status` response already includes metrics).

**Where:** Update the inline HTML/JS in the dashboard handler.

**Sketch:**

```html
<div id="metrics" style="display:flex;gap:20px;margin-bottom:20px;
    padding:10px;background:#2d2d2d;border-radius:4px;">
  <span>⏱ <span id="m-uptime">-</span></span>
  <span>📨 <span id="m-reqs">0</span> reqs</span>
  <span style="color:#4ec9b0">✓ <span id="m-ok">0</span></span>
  <span style="color:#f44747">✗ <span id="m-fail">0</span></span>
  <span>🔴 <span id="m-429">0</span> rate-limited</span>
  <span>📊 <span id="m-rate">-</span>% success</span>
</div>

<script>
// Inside the existing setInterval callback that fetches /pool-status:
document.getElementById('m-uptime').textContent = data.metrics.uptime_human;
document.getElementById('m-reqs').textContent = data.metrics.total_requests.toLocaleString();
document.getElementById('m-ok').textContent = data.metrics.total_success.toLocaleString();
document.getElementById('m-fail').textContent = data.metrics.total_fail.toLocaleString();
document.getElementById('m-429').textContent = data.metrics.total_429;
document.getElementById('m-rate').textContent = data.metrics.success_rate_pct;
</script>

```

**Test:** Open the dashboard and make several requests through the proxy.
The metrics bar should update every 3 seconds with live numbers.

**Risk:** None.

---

## Phase 6 — Validation & Rollout

> **Goal:** Verify all changes work correctly, then deploy to the live service.
> **Risk:** Low — testing before deployment, with clear rollback.

### 6.1 Smoke tests per feature

Run each of these manually after implementation:

| Feature | Test Command / Action | Expected Result |
| --- | --- | --- |
| Path normalisation | `curl http://127.0.0.1:8888/v1beta/models` | Proxied to Google, no mid-path stripping |
| Shared client | Check logs for absence of `"Client is closed"` errors | Clean logs |
| `/help` | Send `/help` to TG bot | List of 8 commands |
| `/rotate` | Send `/rotate` to TG bot | "All keys reset" + status report |
| `/ban` | Send `/ban AIzaSy 60` to TG bot | "Banned key..." message |
| `/unban` | Send `/unban` to TG bot | "Cleared cooldowns" + status |
| `/uptime` | Send `/uptime` to TG bot | Uptime, request counts, success rate |
| `/config` | Send `/config` to TG bot | Configuration values |
| `/digest` | Send `/digest` to TG bot | Full health summary with recent events |
| Failure notifications | Trigger a 429 (use up a key) | TG receives benching notification |
| Pool health alert | Ban >50% of keys, trigger one more failure | TG receives pool degraded alert |
| `/status` HTTP | `curl http://127.0.0.1:8888/status` | JSON with pool + metrics |
| `/reload-keys` HTTP | `curl -X POST http://127.0.0.1:8888/reload-keys` | `{"status": "ok", ...}` |
| `/health` HTTP | `curl http://127.0.0.1:8888/health` | `{"status": "ok", ...}` with 200 |
| `/health` degraded | Ban all keys, then `curl /health` | `{"status": "degraded"}` with 503 |
| Endpoint auth | `curl http://192.168.x.x:8888/status` (no token) | 401 Unauthorized |
| Endpoint auth | `curl -H "Authorization: Bearer changeme_local_only" http://192.168.x.x:8888/status` | 200 OK |
| `.env` support | Create `.env` with `TELEGRAM_TOKEN=test`, verify it's read | Token loaded |
| Dashboard pool table | Open `/dashboard` in browser | Key table visible, updating |
| Dashboard metrics bar | Open `/dashboard` after some requests | Metrics visible, updating |

---

### 6.2 Streaming verification

Streaming is the area most affected by the shared-client refactor (Phase 2.2).
Test thoroughly:

```sh
# 1. Simple stream — verify chunks arrive incrementally
curl -N "http://127.0.0.1:8888/v1beta/models/gemini-2.5-flash:generateContent?alt=sse" \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"parts":[{"text":"Count from 1 to 10 slowly"}]}]}'

# 2. Concurrent streams — verify no cross-contamination
for i in 1 2 3; do
  curl -sN "http://127.0.0.1:8888/v1beta/models/gemini-2.5-flash:generateContent?alt=sse" \
    -H "Content-Type: application/json" \
    -d "{\"contents\":[{\"parts\":[{\"text\":\"Say the number $i\"}]}]}" &
done
wait

# 3. Check logs for any httpx errors
grep -i "client.*closed\|RuntimeError\|stream.*error" logs/proxy.log
# Expected: no matches

```

---

### 6.3 Concurrent load test

Verify key rotation works correctly under concurrent load:

```sh
# Send 50 rapid requests, verify rotation happens and no errors
for i in $(seq 1 50); do
  curl -s -o /dev/null -w "%{http_code} " \
    "http://127.0.0.1:8888/v1beta/models/gemini-2.5-flash:generateContent" \
    -H "Content-Type: application/json" \
    -d '{"contents":[{"parts":[{"text":"Hi"}]}]}' &
done
wait
echo ""

# Check metrics
curl -s http://127.0.0.1:8888/status | python3 -c "
import sys, json
d = json.load(sys.stdin)
m = d['metrics']
print(f\"Requests: {m['total_requests']}, Success: {m['total_success']}, Fail: {m['total_fail']}\")
print(f\"429s: {m['total_429']}, Rotations: {m['rotations']}\")
"

```

---

### 6.4 launchd reload test

Verify the service works correctly through a full launchd lifecycle:

```sh
# 1. Unload
launchctl unload ~/Library/LaunchAgents/com.antigravity.proxy.plist
sleep 2

# 2. Verify stopped
launchctl list com.antigravity.proxy 2>&1
# Expected: "Could not find service"

# 3. Load
launchctl load ~/Library/LaunchAgents/com.antigravity.proxy.plist
sleep 3

# 4. Verify running
launchctl list com.antigravity.proxy | grep PID
# Expected: "PID" = <number>

# 5. Verify startup logs
tail -5 ~/Library/Logs/com.antigravity.proxy.err
# Expected: startup messages, "X keys loaded", no errors

# 6. Verify functional
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8888/health
# Expected: 200

# 7. Check for graceful shutdown message from step 1
grep "shutdown complete" ~/Library/Logs/com.antigravity.proxy.err
# Expected: present

```

---

### 6.5 Documentation update

After all changes are implemented and tested, update the documentation:

1. **`docs/launchd-service-guide.md`** — Add sections for:
   - New environment variables (`.env` support)
   - New HTTP endpoints (`/status`, `/reload-keys`, `/health`, `/pool-status`)
   - Updated Telegram bot commands table (all 8 commands)
   - Updated notification types (per-key, pool health)
   - Updated dashboard description (pool table, metrics bar)
   - Updated Configuration Tuning Reference (new `METRICS` constants)

2. **`README.md`** — Add:
   - Mention of Telegram bot commands
   - Mention of `/health` endpoint for monitoring
   - Updated endpoint list

3. **`requirements.txt`** — Already updated in Step 4.5.

4. **`.env.example`** — Already created in Step 4.5.

---

## Appendix A — New Environment Variables

Summary of all environment variables after full implementation:

| Variable | Required | Default | Phase | Description |
| --- | --- | --- | --- | --- |
| `TELEGRAM_TOKEN` | No | `""` | Existing | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | No | `""` | Existing | Numeric chat ID |

No new environment variables are introduced. All new configuration (metrics
thresholds, rate limits) uses in-code constants that can be promoted to env
vars later if needed.

**Constants that could become env vars in the future:**

| Constant | Current Value | Where |
| --- | --- | --- |
| `MAX_REQ_PER_KEY` | `15` | main.py |
| `COOLDOWN_PERIOD` | `60` | main.py |
| `BACKOFF_MIN` | `5` | main.py |
| `ADMIN_TOKEN` | `"changeme_local_only"` | main.py |
| `global_max_per_minute` | `5` | TelegramManager |
| `pool_health_threshold` | `50%` | catch_all |

---

## Appendix B — Updated requirements.txt

After Phase 4.5:

```log
fastapi
uvicorn
httpx
python-dotenv

```

No other new dependencies. All new features use the Python standard library
and the three existing packages.

---

## Appendix C — File Change Summary

Every file touched across all 6 phases:

| File | Phases | Changes |
| --- | --- | --- |
| `main.py` | 1–5 | Bug fixes, Metrics class, lifespan migration, shared client, TG command router, 8 new TG handlers, notification system, 4 new HTTP endpoints, endpoint auth, .env loading, dashboard HTML/JS |
| `requirements.txt` | 4 | Add `python-dotenv` |
| `.env.example` | 4 | New file — template for environment variables |
| `.gitignore` | 4 | Add `.env` entry |
| `docs/launchd-service-guide.md` | 6 | Update for all new features |
| `README.md` | 6 | Update endpoint list, TG commands |

**Files NOT changed:**

| File | Why |
| --- | --- |
| `api_keys.txt` | User data — never modified by code changes |
| `com.antigravity.proxy.plist` | No new env vars required; existing plist works as-is |
| `com.antigravity.proxy.conf` | Newsyslog config unchanged |
| `antigravity_up.sh` | Legacy launcher — not part of the launchd service |

---

## Appendix D — Implementation Order Cheatsheet

A single linear checklist for executing all phases in order:

```markdown
PHASE 1 — Bug Fixes                                          ✅ COMPLETE 2026-02-18
  [x] 1.1  Fix path normalisation (.removeprefix)
  [x] 1.2  Remove dead routes from guard list
  [x] 1.3  (Interim) Move httpx client inside stream_wrapper
            → Superseded by 2.2 (shared client); not needed as interim step

PHASE 2 — Infrastructure                                     ✅ COMPLETE 2026-02-18
  [x] 2.1  Add Metrics class + wire into catch_all
  [x] 2.2  Lifespan migration + shared httpx client (supersedes 1.3)
  [x] 2.3  Per-topic alert rate limiting in TelegramManager
  [x] 2.4  Graceful shutdown (CancelledError handling)
  [x] 2.5  Command router refactor + _send_message helper

PHASE 3 — Telegram Bot                                       ✅ COMPLETE 2026-02-18
  [x] 3.1  /help command
  [x] 3.2  /rotate command + inline button
  [x] 3.3  /ban <key> [duration] command
  [x] 3.4  /unban command + inline button
  [x] 3.5  /uptime command
  [x] 3.6  /config command
  [x] 3.7  /digest command
  [x] 3.8  Per-key failure notifications
  [x] 3.9  Pool health threshold alerts

PHASE 4 — HTTP Endpoints & Security                          ✅ COMPLETE 2026-02-18
  [x] 4.1  /status JSON endpoint
  [x] 4.2  /reload-keys POST endpoint
  [x] 4.3  /health endpoint (200/503)
  [x] 4.4  Protect internal endpoints (localhost OR token)
  [x] 4.5  .env file support (python-dotenv)

PHASE 5 — Dashboard                                          ✅ COMPLETE 2026-02-18
  [x] 5.1  Key pool status table
  [x] 5.2  /pool-status JSON endpoint
  [x] 5.3  Request metrics summary bar

PHASE 6 — Validation & Rollout                               ✅ COMPLETE 2026-02-18
  [x] 6.1  Smoke test every feature
            → All HTTP endpoints verified (200/405/503), auth guard, path normalisation
  [x] 6.2  Streaming verification (concurrent streams)
            → Single + 3 concurrent streams; no "Client is closed" errors in logs
  [x] 6.3  Concurrent load test (20 parallel requests)
            → 20/20 × 200 OK, 100% success rate, key load-balancing confirmed
            → /health 503 path verified: all-fake-key pool → 503 degraded
  [x] 6.4  launchd reload lifecycle test
            → Unload/load cycle clean; "Antigravity-Proxy shutdown complete" in logs
  [x] 6.5  Documentation update
            → docs/launchd-service-guide.md: env vars, all endpoints, TG commands,
              notifications, dashboard, config tuning table updated
            → README.md: full rewrite with features, endpoints, TG commands, structure

```

Each phase is independently deployable. After completing a phase, reload the
service and verify before moving to the next:

```sh
launchctl unload ~/Library/LaunchAgents/com.antigravity.proxy.plist
sleep 1
launchctl load   ~/Library/LaunchAgents/com.antigravity.proxy.plist
sleep 3
curl -s -o /dev/null -w "Health: %{http_code}\n" http://127.0.0.1:8888/health

```

---

*Plan created: 2026-02-18 — Covers 25 improvements across 6 phases.*
*Phases 1–5 implemented: 2026-02-18 in a single atomic jj commit to `develop` (313 → 1,038 lines).*
*Phase 6 completed: 2026-02-18 — all validation tests passed; documentation updated.*
*All 25 improvements across 6 phases are now complete and live on the `develop` branch.*
