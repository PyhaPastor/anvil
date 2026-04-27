"""Anvil Agent — server API client (REST + WebSocket)."""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import ssl
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import websockets
from websockets.exceptions import WebSocketException

from .config import settings

logger = logging.getLogger("anvil.agent.client")

BASE = settings.agent.server_url
HEADERS = lambda: {"Authorization": f"Bearer {settings.agent.api_token}"}


def _ssl_context() -> ssl.SSLContext | bool:
    """Build SSL context respecting verify_tls and ca_bundle settings."""
    if not settings.agent.verify_tls:
        logger.warning("TLS verification disabled — use only on trusted local networks.")
        return False
    ctx = ssl.create_default_context()
    if settings.agent.ca_bundle:
        ca = Path(settings.agent.ca_bundle)
        if ca.exists():
            ctx.load_verify_locations(str(ca))
        else:
            logger.warning("ca_bundle path does not exist: %s", ca)
    return ctx


def _http_client() -> httpx.AsyncClient:
    ssl_ctx = _ssl_context()
    return httpx.AsyncClient(
        base_url=BASE,
        headers=HEADERS(),
        verify=ssl_ctx,
        timeout=30,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )


# ---------------------------------------------------------------------------
# REST helpers
# ---------------------------------------------------------------------------

async def post_capabilities(caps: dict) -> bool:
    async with _http_client() as client:
        try:
            r = await client.post("/api/v1/agent/capabilities", json=caps)
            return r.status_code == 200
        except httpx.HTTPError as exc:
            logger.error("capabilities POST failed: %s", exc)
            return False


async def post_heartbeat(payload: dict) -> bool:
    async with _http_client() as client:
        try:
            r = await client.post("/api/v1/agent/heartbeat", json=payload)
            return r.status_code == 200
        except httpx.HTTPError as exc:
            logger.debug("heartbeat failed: %s", exc)
            return False


async def get_next_job() -> Optional[dict]:
    async with _http_client() as client:
        try:
            r = await client.get("/api/v1/agent/jobs/next")
            r.raise_for_status()
            data = r.json()
            return data.get("job")
        except httpx.HTTPError as exc:
            logger.error("get_next_job failed: %s", exc)
            return None


async def submit_result(payload: dict) -> bool:
    async with _http_client() as client:
        try:
            r = await client.post("/api/v1/agent/jobs/result", json=payload)
            return r.status_code == 200
        except httpx.HTTPError as exc:
            logger.error("submit_result failed: %s", exc)
            return False


async def identify_hash(sample: str) -> List[dict]:
    async with _http_client() as client:
        try:
            r = await client.post("/api/v1/agent/identify-hash", json={"sample": sample})
            r.raise_for_status()
            return r.json().get("modes", [])
        except httpx.HTTPError as exc:
            logger.debug("identify_hash failed: %s", exc)
            return []


async def _delete_cached_wordlist(filename: str) -> None:
    """Delete a wordlist from the local cache directory."""
    cache_dir = Path(settings.hashcat.workdir) / "cache" / "wordlists"
    # filename may be bare name or include the id_ prefix
    target = cache_dir / filename
    if target.exists():
        target.unlink()
        logger.info("Deleted cached wordlist: %s", target)
    else:
        # Try matching by suffix in case name doesn't include the id prefix
        for f in cache_dir.glob(f"*_{filename}"):
            f.unlink()
            logger.info("Deleted cached wordlist: %s", f)
            break
    # Notify server the file is gone
    async with _http_client() as client:
        try:
            await client.delete("/api/v1/agent/cache/wordlist", params={"name": filename})
        except httpx.HTTPError:
            pass


def get_wordlist_cache() -> list[dict]:
    """Return inventory of cached wordlists for heartbeat reporting."""
    cache_dir = Path(settings.hashcat.workdir) / "cache" / "wordlists"
    if not cache_dir.exists():
        return []
    entries = []
    for f in sorted(cache_dir.iterdir()):
        if f.is_file():
            # Extract wordlist_id from filename prefix (format: {id}_{name})
            parts = f.name.split("_", 1)
            wl_id = int(parts[0]) if len(parts) == 2 and parts[0].isdigit() else None
            entries.append({
                "name": f.name,
                "size_bytes": f.stat().st_size,
                "wordlist_id": wl_id,
            })
    return entries


async def download_file(url_path: str, dest: Path, on_progress=None) -> None:
    """
    Stream-download a file from the server to *dest*.
    Uses the agent's auth token.  Raises httpx.HTTPError on failure.

    on_progress: optional async callable(bytes_done: int, bytes_total: int)
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with _http_client() as client:
        async with client.stream("GET", url_path) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes(65536):
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        await on_progress(done, total)
    logger.debug("Downloaded %s → %s", url_path, dest)


# ---------------------------------------------------------------------------
# WebSocket client for live progress streaming
# ---------------------------------------------------------------------------

WS_BASE = BASE.replace("https://", "wss://").replace("http://", "ws://")


class AgentWSClient:
    """
    Maintains a persistent WebSocket connection to the server.
    Sends live progress events and receives control commands (e.g. cancel).
    """
    def __init__(self) -> None:
        self._ws = None
        self._connected = False
        self._cancel_callbacks: Dict[int, asyncio.Event] = {}
        self._task: Optional[asyncio.Task] = None

    def register_cancel(self, job_id: int, event: asyncio.Event) -> None:
        self._cancel_callbacks[job_id] = event

    def deregister_cancel(self, job_id: int) -> None:
        self._cancel_callbacks.pop(job_id, None)

    async def connect(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ws_client")

    async def disconnect(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def send_progress(self, payload: dict) -> None:
        if self._ws and self._connected:
            try:
                await self._ws.send(json.dumps(payload))
            except WebSocketException:
                self._connected = False

    async def _run(self) -> None:
        """Reconnect loop."""
        backoff = 2
        while True:
            try:
                await self._connect_once()
                backoff = 2
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("WS connection lost: %s — retrying in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_preload_wordlist(self, data: dict) -> None:
        """Download a wordlist to the local cache on demand (server-initiated pre-caching)."""
        import time
        wordlist_id = data.get("wordlist_id")
        name = data.get("name") or f"wordlist_{wordlist_id}.txt"
        size = data.get("size") or 0

        cache_dir = Path(settings.hashcat.workdir) / "cache" / "wordlists"
        dest = cache_dir / f"{wordlist_id}_{name}"

        if dest.exists() and size and dest.stat().st_size == size:
            logger.info("Preload: wordlist %d already cached at %s", wordlist_id, dest)
            await self.send_progress({
                "type": "preload_status",
                "wordlist_id": wordlist_id,
                "status": "already_cached",
            })
            return

        logger.info("Preloading wordlist %d (%s)…", wordlist_id, name)
        last_report = [0.0]
        last_pct = [-1.0]

        async def progress_cb(done: int, total: int) -> None:
            now = time.monotonic()
            bt = total or size
            pct = (done / bt * 100) if bt else 0.0
            if now - last_report[0] < 1.0 and pct - last_pct[0] < 1.0 and pct < 99.5:
                return
            last_report[0] = now
            last_pct[0] = pct
            await self.send_progress({
                "type": "preload_progress",
                "wordlist_id": wordlist_id,
                "bytes_done": done,
                "bytes_total": bt,
                "pct": round(pct, 1),
            })

        try:
            await download_file(
                f"/api/v1/agent/files/wordlist/{wordlist_id}",
                dest,
                on_progress=progress_cb,
            )
            await self.send_progress({
                "type": "preload_status",
                "wordlist_id": wordlist_id,
                "status": "complete",
            })
            logger.info("Preload complete: wordlist %d → %s", wordlist_id, dest)
        except Exception as exc:
            logger.error("Preload failed: wordlist %d: %s", wordlist_id, exc)
            dest.unlink(missing_ok=True)
            await self.send_progress({
                "type": "preload_status",
                "wordlist_id": wordlist_id,
                "status": "error",
                "error": str(exc),
            })

    async def _connect_once(self) -> None:
        # Extract agent_id from token
        try:
            # Decode JWT claims without signature verification (we just need the sub claim)
            parts = settings.agent.api_token.split(".")
            if len(parts) != 3:
                raise ValueError("malformed token")
            padding = 4 - len(parts[1]) % 4
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * padding))
            agent_id = int(payload["sub"])
        except Exception as exc:
            logger.error("Cannot parse agent_id from token — WS connect aborted: %s", exc)
            await asyncio.sleep(30)
            return

        uri = f"{WS_BASE}/ws/agent/{agent_id}"
        ssl_ctx = _ssl_context() if WS_BASE.startswith("wss://") else None
        kwargs: dict = {}
        if ssl_ctx is not False and ssl_ctx is not None:
            kwargs["ssl"] = ssl_ctx
        elif ssl_ctx is False:
            no_verify = ssl.create_default_context()
            no_verify.check_hostname = False
            no_verify.verify_mode = ssl.CERT_NONE
            kwargs["ssl"] = no_verify

        async with websockets.connect(uri, **kwargs) as ws:
            self._ws = ws
            # Authenticate
            await ws.send(json.dumps({"type": "auth", "token": settings.agent.api_token}))
            auth_resp = json.loads(await ws.recv())
            if auth_resp.get("type") != "auth_ok":
                logger.error("WS auth rejected: %s", auth_resp)
                return
            self._connected = True
            logger.info("WebSocket connected to server (agent_id=%d)", agent_id)

            async for raw in ws:
                data = json.loads(raw)
                msg_type = data.get("type")
                if msg_type == "cancel_job":
                    job_id = data.get("job_id")
                    ev = self._cancel_callbacks.get(job_id)
                    if ev:
                        ev.set()
                        logger.info("Received cancel for job %d", job_id)
                elif msg_type == "delete_cached_wordlist":
                    filename = data.get("name")
                    if filename:
                        await _delete_cached_wordlist(filename)
                elif msg_type == "preload_wordlist":
                    asyncio.create_task(
                        self._handle_preload_wordlist(data),
                        name=f"preload_wl_{data.get('wordlist_id')}",
                    )
                elif msg_type == "ping":
                    await ws.send(json.dumps({"type": "pong"}))

        self._connected = False
