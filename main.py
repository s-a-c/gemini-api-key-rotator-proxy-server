# main.py
# FastAPI Gemini Proxy: Proactive Rotation, Log Tracking, Web Dashboard, and Telegram Bot
# pip install fastapi uvicorn httpx

import os
import time
import uuid
import asyncio
import json
import logging
import contextvars
from typing import List, Optional, Dict, Any
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import Response, JSONResponse, StreamingResponse, HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware
import httpx

# -------------------------
# 1. Configuration & Global State
# -------------------------
PROJECT_NAME = "Antigravity-Proxy"
KEYS_FILE = "api_keys.txt"
ADMIN_TOKEN = "changeme_local_only"
UPSTREAM_BASE_GEMINI = "https://generativelanguage.googleapis.com/v1beta"

# Logging Config
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "proxy.log")

# Rotation & Health Config
BACKOFF_MIN = 5
BACKOFF_MAX = 600
COOLDOWN_PERIOD = 60  # Strict cooldown for 429 errors
MAX_REQ_PER_KEY = 15  # Proactively rotate after 15 requests
DEBUG = False

# Telegram Config (read from environment; leave unset to disable)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TG_ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)

# Async-safe storage for Request IDs
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="init")

# -------------------------
# 2. Advanced Logging & Log Rotation
# -------------------------
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

class RequestIDFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_ctx.get()
        return True

log = logging.getLogger("antigravity")
log.setLevel(logging.INFO)
log.propagate = False

# Rotate logs at 5MB, keep 3 backups
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
file_handler.addFilter(RequestIDFilter())
file_handler.setFormatter(logging.Formatter('%(asctime)s [%(request_id)s] %(levelname)s - %(message)s'))
log.addHandler(file_handler)

# Single stderr handler with matching format (no duplicate with uvicorn)
stderr_handler = logging.StreamHandler()
stderr_handler.addFilter(RequestIDFilter())
stderr_handler.setFormatter(logging.Formatter('%(asctime)s [%(request_id)s] %(levelname)s - %(message)s'))
log.addHandler(stderr_handler)

# -------------------------
# 3. Key State & Pool
# -------------------------
class KeyState:
    def __init__(self, key: str):
        self.key = key
        self.banned_until = 0.0
        self.usage_count = 0
        self.success = 0
        self.fail = 0

    def is_available(self) -> bool:
        now = time.monotonic()
        return now >= self.banned_until and self.usage_count < MAX_REQ_PER_KEY

    def mark_success(self):
        self.banned_until = 0.0
        self.success += 1
        self.usage_count += 1

    def mark_failure(self, status_code: int):
        wait = COOLDOWN_PERIOD if status_code == 429 else BACKOFF_MIN
        self.banned_until = time.monotonic() + wait
        self.usage_count = 0
        self.fail += 1

class KeyPool:
    def __init__(self, keys: List[str]):
        self.states = [KeyState(k) for k in keys]
        self.lock = asyncio.Lock()

    async def next_available(self) -> Optional[KeyState]:
        async with self.lock:
            # Load balance: pick the available key with least usage
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
        return [{
            "key_preview": s.key[:12] + "...",
            "available_in": max(0, round(s.banned_until - now, 2)),
            "usage": s.usage_count,
            "success": s.success,
            "fail": s.fail
        } for s in self.states]

def load_keys_from_file(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]

POOL = KeyPool(load_keys_from_file(KEYS_FILE))

# -------------------------
# 4. Telegram Management
# -------------------------
class TelegramManager:
    def __init__(self, token, chat_id):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.last_notify_time = 0

    async def send_alert(self, text):
        if not TG_ENABLED:
            return
        now = time.time()
        if (now - self.last_notify_time) < 600:
            return
        try:
            payload = {"chat_id": self.chat_id, "text": f"🚨 *{PROJECT_NAME} Alert*\n{text}", "parse_mode": "Markdown"}
            async with httpx.AsyncClient() as client:
                await client.post(f"{self.base_url}/sendMessage", json=payload)
            self.last_notify_time = now
        except Exception as e:
            log.warning(f"TG send_alert failed: {repr(e)}")

    async def handle_commands(self):
        if not TG_ENABLED:
            log.info("Telegram integration disabled (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set)")
            return

        offset = 0
        backoff = 5
        max_backoff = 300  # 5 minutes ceiling

        while True:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(f"{self.base_url}/getUpdates", params={"offset": offset, "timeout": 20})
                    data = resp.json()
                    if not data.get("ok"):
                        raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown')}")
                    updates = data.get("result", [])
                    backoff = 5  # reset on success

                    for u in updates:
                        offset = u["update_id"] + 1
                        msg = u.get("message", {})
                        cb = u.get("callback_query", {})
                        cid = str(msg.get("chat", {}).get("id", cb.get("from", {}).get("id")))

                        if cid == self.chat_id:
                            # Handle /status or "Reload" button click
                            if cb.get("data") == "reload" or msg.get("text") == "/status":
                                if cb.get("data") == "reload":
                                    global POOL; POOL = KeyPool(load_keys_from_file(KEYS_FILE))
                                    await client.post(f"{self.base_url}/answerCallbackQuery", json={"callback_query_id": cb["id"], "text": "Keys Reloaded!"})

                                report = f"📊 *{PROJECT_NAME} Status*\n" + "\n".join([f"• {k['key_preview']}: {k['usage']} reqs" for k in POOL.status()])
                                kb = {"inline_keyboard": [[{"text": "🔄 Reload Keys", "callback_data": "reload"}]]}
                                await client.post(f"{self.base_url}/sendMessage", json={"chat_id": self.chat_id, "text": report, "parse_mode": "Markdown", "reply_markup": kb})

            except Exception as e:
                log.error(f"TG Listener Error: {repr(e)}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue

            await asyncio.sleep(5)

TG = TelegramManager(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)

# -------------------------
# 5. FastAPI App & Middleware
# -------------------------
APP = FastAPI(title="Native Gemini Proxy (Fully Loaded)")

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

@APP.on_event("startup")
async def startup_event():
    if TG_ENABLED:
        asyncio.create_task(TG.handle_commands())
    else:
        log.info("Telegram integration disabled (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set)")

# -------------------------
# 6. Proxy & Routing
# -------------------------
def prepare_auth(key_state: KeyState, headers: Dict, params: Dict):
    h = {k: v for k, v in headers.items() if k.lower() not in ("host", "content-length", "authorization")}
    p = dict(params)
    if key_state.key.startswith("AIza"):
        p['key'] = key_state.key
    else:
        h['Authorization'] = f"Bearer {key_state.key}"
    return h, p

@APP.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
async def catch_all(request: Request, path: str):
    # 1. Handle health checks from tools like curl -I
    if request.method == "HEAD":
        return Response(status_code=200)

    # 2. Normalize path
    clean_path = path.replace("v1/", "").replace("v1beta/", "").lstrip("/").rstrip("/")

    # 3. Route internal tools
    if clean_path in ("dashboard", "logs", "status", "reload-keys"):
        return await handle_internal(request, clean_path)

    # 4. Proxy everything else to Google...
    upstream_url = f"{UPSTREAM_BASE_GEMINI}/{clean_path}"
    body = await request.body()
    params = dict(request.query_params)
    is_stream = "alt=sse" in str(request.query_params) or ":generateContent" in upstream_url

    for _ in range(len(POOL.states)):
        key_state = await POOL.next_available()
        if not key_state:
            asyncio.create_task(TG.send_alert("All keys exhausted!"))
            return JSONResponse({"error": "all keys rate-limited"}, status_code=429)

        h, p = prepare_auth(key_state, dict(request.headers), params)
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                if is_stream:
                    async def stream_wrapper():
                        async with client.stream(request.method, upstream_url, headers=h, params=p, content=body) as r:
                            if r.status_code >= 400:
                                key_state.mark_failure(r.status_code)
                                yield f"data: {json.dumps({'error': 'stream error'})}\n\n".encode()
                                return
                            key_state.mark_success()
                            async for chunk in r.aiter_bytes(): yield chunk
                    return StreamingResponse(stream_wrapper(), media_type="text/event-stream")
                else:
                    r = await client.request(request.method, upstream_url, headers=h, params=p, content=body)
                    if r.status_code < 400:
                        key_state.mark_success()
                        return Response(content=r.content, status_code=r.status_code)
                    key_state.mark_failure(r.status_code)
                    continue
        except Exception as e:
            key_state.mark_failure(500)
            log.error(f"Request error: {e}")

    return JSONResponse({"error": "no upstream key succeeded"}, status_code=502)

# -------------------------
# 7. Internal & Dashboard
# -------------------------
async def handle_internal(request, path):
    if path == "dashboard":
        return HTMLResponse("""<html><body style="background:#1e1e1e;color:#d4d4d4;font-family:monospace;padding:20px;">
            <h2>Live Logs</h2><div id="l" style="background:#000;padding:10px;height:80vh;overflow-y:scroll;"></div>
            <script>new EventSource('/logs').onmessage=e=>{const n=document.createElement('div');n.textContent=e.data;
            const c=document.getElementById('l');c.appendChild(n);c.scrollTop=c.scrollHeight;}</script></body></html>""")

    if path == "logs":
        async def tail():
            with open(LOG_FILE, "r") as f:
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line: yield f"data: {line}\n\n"
                    else: await asyncio.sleep(0.5)
        return StreamingResponse(tail(), media_type="text/event-stream")

    return JSONResponse({"error": "not found"}, status_code=404)
