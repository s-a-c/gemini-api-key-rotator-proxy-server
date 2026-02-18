# main.py
# FastAPI Gemini Proxy: Proactive Rotation, Log Tracking, Web Dashboard, and Telegram Bot
# pip install fastapi uvicorn httpx python-dotenv

import os
import time
import uuid
import asyncio
import json
import logging
import contextvars
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any
from logging.handlers import RotatingFileHandler

# ── .env support (soft dependency) ──────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; rely on system env vars

from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse, StreamingResponse, HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware
import httpx

# ─────────────────────────────────────────────────────────────────────────────
# 1. Configuration & Global State
# ─────────────────────────────────────────────────────────────────────────────
PROJECT_NAME = "Antigravity-Proxy"
KEYS_FILE = "api_keys.txt"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme_local_only")
UPSTREAM_BASE_GEMINI = "https://generativelanguage.googleapis.com/v1beta"

# Logging
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "proxy.log")

# Rotation & Health Config
BACKOFF_MIN = 5
BACKOFF_MAX = 600
COOLDOWN_PERIOD = 60   # strict cooldown for 429 errors (seconds)
MAX_REQ_PER_KEY = 15   # proactively rotate after N requests

# Telegram (read from environment; leave unset to disable)
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TG_ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)

# Async-safe request-ID storage
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="init"
)

# ─────────────────────────────────────────────────────────────────────────────
# 2. Logging
# ─────────────────────────────────────────────────────────────────────────────
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)


class RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()  # type: ignore[attr-defined]
        return True


_fmt = logging.Formatter("%(asctime)s [%(request_id)s] %(levelname)s - %(message)s")

log = logging.getLogger("antigravity")
log.setLevel(logging.INFO)
log.propagate = False

_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_file_handler.addFilter(RequestIDFilter())
_file_handler.setFormatter(_fmt)
log.addHandler(_file_handler)

_stderr_handler = logging.StreamHandler()
_stderr_handler.addFilter(RequestIDFilter())
_stderr_handler.setFormatter(_fmt)
log.addHandler(_stderr_handler)

# ─────────────────────────────────────────────────────────────────────────────
# 3. Key State & Pool
# ─────────────────────────────────────────────────────────────────────────────
class KeyState:
    def __init__(self, key: str) -> None:
        self.key = key
        self.banned_until: float = 0.0
        self.usage_count: int = 0
        self.success: int = 0
        self.fail: int = 0

    def is_available(self) -> bool:
        return time.monotonic() >= self.banned_until and self.usage_count < MAX_REQ_PER_KEY

    def mark_success(self) -> None:
        self.banned_until = 0.0
        self.success += 1
        self.usage_count += 1

    def mark_failure(self, status_code: int) -> None:
        wait = COOLDOWN_PERIOD if status_code == 429 else BACKOFF_MIN
        self.banned_until = time.monotonic() + wait
        self.usage_count = 0
        self.fail += 1


class KeyPool:
    def __init__(self, keys: List[str]) -> None:
        self.states = [KeyState(k) for k in keys]
        self.lock = asyncio.Lock()

    async def next_available(self) -> Optional[KeyState]:
        async with self.lock:
            available = [s for s in self.states if s.is_available()]
            if not available:
                # Fallback: pick the one waking up soonest
                self.states.sort(key=lambda x: x.banned_until)
                best = self.states[0]
                if time.monotonic() >= best.banned_until:
                    best.usage_count = 0
                    return best
                return None
            available.sort(key=lambda x: x.usage_count)
            return available[0]

    def status(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        return [
            {
                "key_preview": s.key[:12] + "...",
                "available_in": max(0, round(s.banned_until - now, 2)),
                "usage": s.usage_count,
                "success": s.success,
                "fail": s.fail,
            }
            for s in self.states
        ]


def load_keys_from_file(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


POOL = KeyPool(load_keys_from_file(KEYS_FILE))

# ─────────────────────────────────────────────────────────────────────────────
# 4. Metrics
# ─────────────────────────────────────────────────────────────────────────────
class Metrics:
    def __init__(self) -> None:
        self.start_time = time.time()
        self.total_requests: int = 0
        self.total_success: int = 0
        self.total_fail: int = 0
        self.total_429: int = 0
        self.rotations: int = 0
        self.benchings: List[Dict[str, Any]] = []

    def record_success(self) -> None:
        self.total_requests += 1
        self.total_success += 1

    def record_failure(self, status_code: int) -> None:
        self.total_requests += 1
        self.total_fail += 1
        if status_code == 429:
            self.total_429 += 1

    def record_benching(self, key_preview: str, status_code: int, duration: float) -> None:
        self.benchings.append({
            "time": time.time(),
            "key": key_preview,
            "status": status_code,
            "duration": duration,
        })
        if len(self.benchings) > 100:
            self.benchings = self.benchings[-100:]

    def record_rotation(self) -> None:
        self.rotations += 1

    def uptime(self) -> float:
        return time.time() - self.start_time

    def uptime_str(self) -> str:
        s = int(self.uptime())
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        parts: List[str] = []
        if d:
            parts.append(f"{d}d")
        if h:
            parts.append(f"{h}h")
        if m:
            parts.append(f"{m}m")
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

# ─────────────────────────────────────────────────────────────────────────────
# 5. Telegram Manager
# ─────────────────────────────────────────────────────────────────────────────
class TelegramManager:
    def __init__(self, token: str, chat_id: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        # Per-topic cooldowns: topic -> last send timestamp
        self.alert_cooldowns: Dict[str, float] = {}
        # Global rate limit: sliding window of send timestamps
        self.global_sends: List[float] = []
        self.global_max_per_minute = 5

    # ── Rate limiting ────────────────────────────────────────────────────────

    def _can_send(self, topic: str, cooldown: int = 600) -> bool:
        """Check per-topic cooldown AND global rate limit."""
        now = time.time()
        if now - self.alert_cooldowns.get(topic, 0.0) < cooldown:
            return False
        # Prune old entries and check global cap
        self.global_sends = [t for t in self.global_sends if now - t < 60]
        return len(self.global_sends) < self.global_max_per_minute

    def _mark_sent(self, topic: str) -> None:
        now = time.time()
        self.alert_cooldowns[topic] = now
        self.global_sends.append(now)

    # ── Outbound helpers ─────────────────────────────────────────────────────

    async def _send_message(
        self,
        client: httpx.AsyncClient,
        text: str,
        keyboard: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        try:
            await client.post(
                f"{self.base_url}/sendMessage", json=payload, timeout=10
            )
        except Exception as e:
            log.warning(f"TG _send_message failed: {repr(e)}")

    async def send_alert(
        self,
        text: str,
        topic: str = "general",
        cooldown: int = 600,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not TG_ENABLED:
            return
        if not self._can_send(topic, cooldown):
            return
        payload = {
            "chat_id": self.chat_id,
            "text": f"🚨 *{PROJECT_NAME}*\n{text}",
            "parse_mode": "Markdown",
        }
        _client = client or httpx.AsyncClient()
        try:
            await _client.post(
                f"{self.base_url}/sendMessage", json=payload, timeout=10
            )
            self._mark_sent(topic)
        except Exception as e:
            log.warning(f"TG send_alert failed: {repr(e)}")
        finally:
            if not client:
                await _client.aclose()

    async def notify_key_benched(
        self,
        key_preview: str,
        status_code: int,
        duration: float,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """Notify when a key is benched due to upstream failure."""
        topic = f"bench_{key_preview}"
        if status_code == 429:
            emoji, reason, cd = "🔴", "rate limit", 300
        elif status_code in (401, 403):
            emoji, reason, cd = "❌", "auth error", 600
        else:
            emoji, reason, cd = "⚠️", f"HTTP {status_code}", 300
        text = f"{emoji} Key `{key_preview}` benched for {int(duration)}s — {reason}"
        await self.send_alert(text, topic=topic, cooldown=cd, client=client)

    # ── Command handlers ─────────────────────────────────────────────────────

    async def _cmd_help(self, client: httpx.AsyncClient, msg: Dict) -> None:
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

    async def _cmd_status(self, client: httpx.AsyncClient, msg: Dict) -> None:
        pool = POOL.status()
        available = sum(1 for k in pool if k["available_in"] == 0)
        lines = [f"📊 *{PROJECT_NAME} Status* ({available}/{len(pool)} available)\n"]
        for k in pool:
            if k["available_in"] > 0:
                state_str = f"🔴 cooldown {k['available_in']}s"
            else:
                state_str = "🟢 active"
            lines.append(
                f"• `{k['key_preview']}`: {k['usage']} reqs, "
                f"{k['success']}✓ {k['fail']}✗ — {state_str}"
            )
        kb: Dict[str, Any] = {
            "inline_keyboard": [
                [
                    {"text": "🔄 Reload Keys", "callback_data": "reload"},
                    {"text": "🔃 Rotate All",  "callback_data": "rotate"},
                ],
                [{"text": "✅ Unban All", "callback_data": "unban"}],
            ]
        }
        await self._send_message(client, "\n".join(lines), keyboard=kb)

    async def _cmd_rotate(self, client: httpx.AsyncClient, msg: Dict) -> None:
        async with POOL.lock:
            for s in POOL.states:
                s.usage_count = 0
                s.banned_until = 0.0
        METRICS.record_rotation()
        await self._send_message(
            client,
            "🔃 All keys rotated — usage counters and cooldowns cleared.",
        )
        await self._cmd_status(client, msg)

    async def _cmd_ban(self, client: httpx.AsyncClient, msg: Dict) -> None:
        parts = msg.get("text", "").split()
        if len(parts) < 2:
            await self._send_message(
                client,
                "Usage: `/ban <key_preview> [seconds]`\n"
                "Example: `/ban AIzaSyCDpw 3600` (ban for 1h)\n"
                "Default duration: 86400s (24h)",
            )
            return
        preview = parts[1].rstrip(".")
        try:
            duration = int(parts[2]) if len(parts) > 2 else 86400
        except ValueError:
            await self._send_message(client, "❌ Duration must be an integer (seconds).")
            return
        for s in POOL.states:
            if s.key.startswith(preview):
                s.banned_until = time.monotonic() + duration
                if duration >= 3600:
                    dur_str = f"{duration // 3600}h {(duration % 3600) // 60}m"
                else:
                    dur_str = f"{duration}s"
                METRICS.record_benching(s.key[:12] + "...", 0, duration)
                await self._send_message(
                    client, f"🚫 Banned `{s.key[:12]}...` for {dur_str}"
                )
                return
        await self._send_message(
            client,
            f"❌ No key matching `{preview}` found.\n"
            f"Hint: use the first few chars shown in /status, e.g. `/ban AIzaSy`",
        )

    async def _cmd_unban(self, client: httpx.AsyncClient, msg: Dict) -> None:
        count = 0
        async with POOL.lock:
            now = time.monotonic()
            for s in POOL.states:
                if s.banned_until > now:
                    s.banned_until = 0.0
                    count += 1
        if count:
            await self._send_message(client, f"✅ Cleared cooldowns on {count} key(s)")
        else:
            await self._send_message(client, "✅ No keys were in cooldown")
        await self._cmd_status(client, msg)

    async def _cmd_uptime(self, client: httpx.AsyncClient, msg: Dict) -> None:
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

    async def _cmd_config(self, client: httpx.AsyncClient, msg: Dict) -> None:
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

    async def _cmd_digest(self, client: httpx.AsyncClient, msg: Dict) -> None:
        m = METRICS.summary()
        available = sum(1 for s in POOL.states if s.is_available())
        total_keys = len(POOL.states)
        lines = [
            f"📊 *{PROJECT_NAME} Digest*\n",
            f"Uptime: `{m['uptime_human']}`",
            f"Requests: `{m['total_requests']:,}` ({m['success_rate_pct']}% success)",
            f"429 events: `{m['total_429']}`",
            f"Keys: `{available}/{total_keys}` available\n",
        ]
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
                    f"• {ago_str}: `{ev['key']}` benched {int(ev['duration'])}s "
                    f"(HTTP {ev['status']})"
                )
        else:
            lines.append("_No recent events._")
        await self._send_message(client, "\n".join(lines))

    # ── Callback handlers ────────────────────────────────────────────────────

    async def _cb_reload(self, client: httpx.AsyncClient, cb: Dict) -> None:
        global POOL
        new_keys = load_keys_from_file(KEYS_FILE)
        POOL = KeyPool(new_keys)
        log.info(f"Keys reloaded via TG: {len(new_keys)} keys")
        try:
            await client.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": cb["id"], "text": "Keys Reloaded!"},
                timeout=10,
            )
        except Exception:
            pass
        await self._cmd_status(client, cb)

    async def _cb_rotate(self, client: httpx.AsyncClient, cb: Dict) -> None:
        try:
            await client.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": cb["id"], "text": "Keys Rotated!"},
                timeout=10,
            )
        except Exception:
            pass
        await self._cmd_rotate(client, cb)

    async def _cb_unban(self, client: httpx.AsyncClient, cb: Dict) -> None:
        try:
            await client.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": cb["id"], "text": "Cooldowns Cleared!"},
                timeout=10,
            )
        except Exception:
            pass
        await self._cmd_unban(client, cb)

    # ── Main polling loop ────────────────────────────────────────────────────

    async def handle_commands(self, client: httpx.AsyncClient) -> None:
        if not TG_ENABLED:
            log.info("Telegram integration disabled (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set)")
            return

        cmd_handlers: Dict[str, Any] = {
            "/help":   self._cmd_help,
            "/status": self._cmd_status,
            "/rotate": self._cmd_rotate,
            "/ban":    self._cmd_ban,
            "/unban":  self._cmd_unban,
            "/uptime": self._cmd_uptime,
            "/config": self._cmd_config,
            "/digest": self._cmd_digest,
        }
        cb_handlers: Dict[str, Any] = {
            "reload": self._cb_reload,
            "rotate": self._cb_rotate,
            "unban":  self._cb_unban,
        }

        offset = 0
        backoff = 5

        while True:
            try:
                resp = await client.get(
                    f"{self.base_url}/getUpdates",
                    params={"offset": offset, "timeout": 20},
                    timeout=30,
                )
                data = resp.json()
                if not data.get("ok"):
                    raise RuntimeError(
                        f"Telegram API error: {data.get('description', 'unknown')}"
                    )
                updates = data.get("result", [])
                backoff = 5  # reset on success

                for u in updates:
                    offset = u["update_id"] + 1
                    msg = u.get("message", {})
                    cb  = u.get("callback_query", {})
                    cid = str(
                        msg.get("chat", {}).get("id",
                        cb.get("from", {}).get("id", ""))
                    )

                    if cid != self.chat_id:
                        continue

                    # Inline button callback
                    cb_data = cb.get("data", "")
                    if cb_data and cb_data in cb_handlers:
                        try:
                            await cb_handlers[cb_data](client, cb)
                        except Exception as e:
                            log.error(f"TG callback error [{cb_data}]: {repr(e)}")
                        continue

                    # Text command
                    text = msg.get("text", "").strip()
                    cmd = text.split()[0].lower() if text else ""
                    if cmd in cmd_handlers:
                        try:
                            await cmd_handlers[cmd](client, msg)
                        except Exception as e:
                            log.error(f"TG command error [{cmd}]: {repr(e)}")

            except asyncio.CancelledError:
                log.info("TG Listener shutting down")
                return
            except Exception as e:
                log.error(f"TG Listener error: {repr(e)}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue

            await asyncio.sleep(1)


TG = TelegramManager(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)

# ─────────────────────────────────────────────────────────────────────────────
# 6. Application Lifespan (replaces deprecated @on_event("startup"))
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    app.state.tg_task = None

    if TG_ENABLED:
        app.state.tg_task = asyncio.create_task(
            TG.handle_commands(app.state.http_client)
        )
        log.info("Telegram listener started")
    else:
        log.info("Telegram integration disabled (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set)")

    log.info(f"{PROJECT_NAME} started — {len(POOL.states)} keys loaded")

    yield  # ── App is running ────────────────────────────────────────────────

    # ── Shutdown ─────────────────────────────────────────────────────────────
    if app.state.tg_task:
        app.state.tg_task.cancel()
        try:
            await app.state.tg_task
        except asyncio.CancelledError:
            pass

    await app.state.http_client.aclose()
    log.info(f"{PROJECT_NAME} shutdown complete")


# ─────────────────────────────────────────────────────────────────────────────
# 7. FastAPI App & Middleware
# ─────────────────────────────────────────────────────────────────────────────
APP = FastAPI(title="Native Gemini Proxy (Fully Loaded)", lifespan=lifespan)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = str(uuid.uuid4())[:8]
        token = request_id_ctx.set(rid)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            request_id_ctx.reset(token)


APP.add_middleware(RequestIDMiddleware)

# ─────────────────────────────────────────────────────────────────────────────
# 8. Routing Helpers
# ─────────────────────────────────────────────────────────────────────────────
INTERNAL_ROUTES = {"dashboard", "logs", "status", "reload-keys", "health", "pool-status"}


def prepare_auth(
    key_state: KeyState,
    headers: Dict[str, str],
    params: Dict[str, str],
) -> tuple:
    h = {k: v for k, v in headers.items()
         if k.lower() not in ("host", "content-length", "authorization")}
    p = dict(params)
    if key_state.key.startswith("AIza"):
        p["key"] = key_state.key
    else:
        h["Authorization"] = f"Bearer {key_state.key}"
    return h, p


def _pool_health_alert(client: httpx.AsyncClient) -> None:
    """Fire-and-forget pool degradation alert."""
    benched = sum(1 for s in POOL.states if not s.is_available())
    total   = len(POOL.states)
    if benched > total // 2:
        asyncio.create_task(
            TG.send_alert(
                f"⚠️ Pool degraded: {benched}/{total} keys currently benched",
                topic="pool_health",
                cooldown=300,
                client=client,
            )
        )


# ─────────────────────────────────────────────────────────────────────────────
# 9. Main Proxy Route
# ─────────────────────────────────────────────────────────────────────────────
@APP.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
)
async def catch_all(request: Request, path: str):
    # 1. HEAD -> quick health probe
    if request.method == "HEAD":
        return Response(status_code=200)

    # 2. Normalise path: strip leading version prefix only, not mid-path
    clean_path = path.lstrip("/")
    clean_path = clean_path.removeprefix("v1beta/").removeprefix("v1/")
    clean_path = clean_path.strip("/")

    # 3. Internal routes
    if clean_path in INTERNAL_ROUTES:
        return await handle_internal(request, clean_path)

    # 4. Proxy to Google
    upstream_url = f"{UPSTREAM_BASE_GEMINI}/{clean_path}"
    body   = await request.body()
    params = dict(request.query_params)
    is_stream = (
        "alt=sse" in str(request.query_params)
        or ":streamGenerateContent" in upstream_url
    )

    client = request.app.state.http_client

    for _ in range(len(POOL.states)):
        key_state = await POOL.next_available()
        if not key_state:
            asyncio.create_task(
                TG.send_alert(
                    "All keys exhausted — pool fully rate-limited!",
                    topic="exhausted",
                    cooldown=300,
                    client=client,
                )
            )
            return JSONResponse({"error": "all keys rate-limited"}, status_code=429)

        h, p = prepare_auth(key_state, dict(request.headers), params)

        try:
            if is_stream:
                # Capture loop-locals in default args so each iteration gets
                # its own copies; shared client is never closed mid-stream.
                async def stream_wrapper(
                    _ks=key_state, _h=h, _p=p, _cl=client
                ):
                    async with _cl.stream(
                        request.method, upstream_url,
                        headers=_h, params=_p, content=body,
                    ) as r:
                        if r.status_code >= 400:
                            benching_dur = (
                                COOLDOWN_PERIOD if r.status_code == 429
                                else BACKOFF_MIN
                            )
                            _ks.mark_failure(r.status_code)
                            METRICS.record_failure(r.status_code)
                            METRICS.record_benching(
                                _ks.key[:12] + "...", r.status_code, benching_dur
                            )
                            asyncio.create_task(
                                TG.notify_key_benched(
                                    _ks.key[:12] + "...",
                                    r.status_code,
                                    benching_dur,
                                    client=_cl,
                                )
                            )
                            _pool_health_alert(_cl)
                            yield (
                                f"data: {json.dumps({'error': 'stream error'})}\n\n"
                                .encode()
                            )
                            return
                        _ks.mark_success()
                        METRICS.record_success()
                        async for chunk in r.aiter_bytes():
                            yield chunk

                return StreamingResponse(
                    stream_wrapper(), media_type="text/event-stream"
                )

            else:
                r = await client.request(
                    request.method, upstream_url,
                    headers=h, params=p, content=body,
                )
                if r.status_code < 400:
                    key_state.mark_success()
                    METRICS.record_success()
                    return Response(
                        content=r.content,
                        status_code=r.status_code,
                        headers=dict(r.headers),
                    )

                benching_dur = COOLDOWN_PERIOD if r.status_code == 429 else BACKOFF_MIN
                key_state.mark_failure(r.status_code)
                METRICS.record_failure(r.status_code)
                METRICS.record_benching(
                    key_state.key[:12] + "...", r.status_code, benching_dur
                )
                asyncio.create_task(
                    TG.notify_key_benched(
                        key_state.key[:12] + "...",
                        r.status_code,
                        benching_dur,
                        client=client,
                    )
                )
                _pool_health_alert(client)
                log.warning(
                    f"Upstream {r.status_code} for {clean_path} — "
                    f"benching {key_state.key[:12]}... for {benching_dur}s"
                )
                continue

        except asyncio.CancelledError:
            raise
        except Exception as e:
            benching_dur = BACKOFF_MIN
            key_state.mark_failure(500)
            METRICS.record_failure(500)
            METRICS.record_benching(key_state.key[:12] + "...", 500, benching_dur)
            asyncio.create_task(
                TG.notify_key_benched(
                    key_state.key[:12] + "...", 500, benching_dur, client=client
                )
            )
            _pool_health_alert(client)
            log.error(f"Request error for {clean_path}: {repr(e)}")

    return JSONResponse({"error": "no upstream key succeeded"}, status_code=502)


# ─────────────────────────────────────────────────────────────────────────────
# 10. Internal Endpoints & Dashboard
# ─────────────────────────────────────────────────────────────────────────────
_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Antigravity Proxy Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #1e1e1e; color: #d4d4d4; font-family: monospace; padding: 20px; }
  h2 { color: #9cdcfe; margin-bottom: 16px; }
  h3 { color: #9cdcfe; margin: 16px 0 8px; }
  #metrics {
    display: flex; flex-wrap: wrap; gap: 16px;
    margin-bottom: 20px; padding: 12px 16px;
    background: #2d2d2d; border-radius: 6px; font-size: 13px;
  }
  #metrics span { white-space: nowrap; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 4px; }
  th { text-align: left; padding: 6px 8px; background: #2d2d2d; color: #9cdcfe; border-bottom: 1px solid #444; }
  td { padding: 5px 8px; border-bottom: 1px solid #333; }
  tr:hover td { background: #252525; }
  #log-box {
    background: #000; padding: 12px; height: 45vh;
    overflow-y: scroll; border-radius: 4px; font-size: 12px;
    border: 1px solid #333;
  }
  .ok   { color: #4ec9b0; }
  .fail { color: #f44747; }
  .pill-green { color: #4ec9b0; }
  .pill-red   { color: #f44747; }
</style>
</head>
<body>
<h2>&#x1F6F8; Antigravity Proxy Dashboard</h2>

<div id="metrics">
  <span>&#x23F1; <span id="m-uptime">-</span></span>
  <span>&#x1F4E8; <span id="m-reqs">0</span> reqs</span>
  <span class="ok">&#x2713; <span id="m-ok">0</span></span>
  <span class="fail">&#x2717; <span id="m-fail">0</span></span>
  <span>&#x1F534; <span id="m-429">0</span> rate-limited</span>
  <span>&#x1F4CA; <span id="m-rate">-</span>% success</span>
</div>

<h3>Key Pool</h3>
<table>
  <thead>
    <tr>
      <th>Key</th><th>Usage</th><th>Success</th>
      <th>Fail</th><th>Cooldown</th><th>Status</th>
    </tr>
  </thead>
  <tbody id="pool-body"></tbody>
</table>

<h3>Live Logs</h3>
<div id="log-box"></div>

<script>
(function () {
  async function refreshPool() {
    try {
      const resp = await fetch('/pool-status');
      if (!resp.ok) return;
      const data = await resp.json();
      const m = data.metrics;
      document.getElementById('m-uptime').textContent = m.uptime_human;
      document.getElementById('m-reqs').textContent   = m.total_requests.toLocaleString();
      document.getElementById('m-ok').textContent     = m.total_success.toLocaleString();
      document.getElementById('m-fail').textContent   = m.total_fail.toLocaleString();
      document.getElementById('m-429').textContent    = m.total_429;
      document.getElementById('m-rate').textContent   = m.success_rate_pct;
      const tbody = document.getElementById('pool-body');
      tbody.innerHTML = data.keys.map(k => {
        const avail = k.available_in > 0;
        return '<tr>' +
          '<td><code>' + k.key_preview + '</code></td>' +
          '<td>' + k.usage + '</td>' +
          '<td class="ok">' + k.success + '</td>' +
          '<td class="fail">' + k.fail + '</td>' +
          '<td>' + (avail ? k.available_in + 's' : '-') + '</td>' +
          '<td>' + (avail
            ? '<span class="pill-red">&#x1F534; cooling</span>'
            : '<span class="pill-green">&#x1F7E2; active</span>') + '</td>' +
          '</tr>';
      }).join('');
    } catch (e) { /* network error — will retry */ }
  }
  refreshPool();
  setInterval(refreshPool, 3000);

  const logBox = document.getElementById('log-box');
  const MAX_LINES = 500;
  function appendLog(text) {
    const line = document.createElement('div');
    line.textContent = text;
    logBox.appendChild(line);
    while (logBox.childElementCount > MAX_LINES) {
      logBox.removeChild(logBox.firstChild);
    }
    logBox.scrollTop = logBox.scrollHeight;
  }
  const es = new EventSource('/logs');
  es.onmessage = e => appendLog(e.data);
  es.onerror   = ()  => appendLog('[connection lost — retrying...]');
})();
</script>
</body>
</html>
"""


async def handle_internal(request: Request, path: str):
    global POOL

    # Auth guard: localhost always allowed; remote requires Bearer token
    client_host = request.client.host if request.client else "unknown"
    is_local = client_host in ("127.0.0.1", "::1", "localhost")

    if not is_local:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {ADMIN_TOKEN}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    # ── Dashboard ──────────────────────────────────────────────────────────
    if path == "dashboard":
        return HTMLResponse(_DASHBOARD_HTML)

    # ── Log tail (SSE) ─────────────────────────────────────────────────────
    if path == "logs":
        async def tail():
            with open(LOG_FILE, "r") as f:
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        await asyncio.sleep(0.5)
        return StreamingResponse(tail(), media_type="text/event-stream")

    # ── /status ────────────────────────────────────────────────────────────
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

    # ── /pool-status (lightweight JSON for dashboard polling) ──────────────
    if path == "pool-status":
        available = sum(1 for s in POOL.states if s.is_available())
        return JSONResponse({
            "keys": POOL.status(),
            "available": available,
            "total": len(POOL.states),
            "metrics": METRICS.summary(),
        })

    # ── /health ────────────────────────────────────────────────────────────
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

    # ── /reload-keys ───────────────────────────────────────────────────────
    if path == "reload-keys":
        if request.method != "POST":
            return JSONResponse({"error": "POST required"}, status_code=405)
        try:
            new_keys = load_keys_from_file(KEYS_FILE)
            if not new_keys:
                return JSONResponse({"error": "keys file is empty"}, status_code=400)
            POOL = KeyPool(new_keys)
            log.info(f"Keys reloaded via HTTP: {len(new_keys)} keys")
            return JSONResponse({"status": "ok", "keys_loaded": len(new_keys)})
        except FileNotFoundError:
            return JSONResponse({"error": f"{KEYS_FILE} not found"}, status_code=404)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"error": "not found"}, status_code=404)
