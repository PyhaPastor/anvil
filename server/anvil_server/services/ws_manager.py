"""WebSocket connection manager — tracks live connections per job and broadcasts progress."""
from __future__ import annotations
import asyncio
import json
import logging
from collections import defaultdict
from typing import Dict, List

from fastapi import WebSocket

logger = logging.getLogger("anvil.ws")


class ConnectionManager:
    def __init__(self) -> None:
        # job_id -> list of browser WebSocket connections
        self._job_subs: Dict[int, List[WebSocket]] = defaultdict(list)
        # agent_id -> single WebSocket connection from the agent
        self._agent_conns: Dict[int, WebSocket] = {}
        # job_id -> most recent candidate word (in-memory, not persisted)
        self._job_candidates: Dict[int, str] = {}
        # job_id -> most recent absolute keys_tried count from hashcat
        self._keys_tried: Dict[int, int] = {}
        # job_id -> download progress snapshot
        self._download_progress: Dict[int, dict] = {}
        # (agent_id, wordlist_id) -> preload progress snapshot
        self._preload_progress: Dict[tuple, dict] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Browser subscriptions
    # ------------------------------------------------------------------

    async def subscribe(self, job_id: int, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._job_subs[job_id].append(ws)
        logger.debug("Browser subscribed to job %d (total: %d)", job_id, len(self._job_subs[job_id]))

    async def unsubscribe(self, job_id: int, ws: WebSocket) -> None:
        async with self._lock:
            self._job_subs[job_id] = [c for c in self._job_subs[job_id] if c is not ws]

    async def broadcast_job_update(self, job_id: int, payload: dict) -> None:
        """Push a JSON update to all browsers watching this job."""
        message = json.dumps(payload)
        dead: list[WebSocket] = []
        for ws in list(self._job_subs.get(job_id, [])):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.unsubscribe(job_id, ws)

    # ------------------------------------------------------------------
    # Candidate cache
    # ------------------------------------------------------------------

    def set_candidate(self, job_id: int, candidate: str) -> None:
        if candidate:
            self._job_candidates[job_id] = candidate

    def get_candidate(self, job_id: int) -> str | None:
        return self._job_candidates.get(job_id)

    def clear_candidate(self, job_id: int) -> None:
        self._job_candidates.pop(job_id, None)

    # ------------------------------------------------------------------
    # Keys tried cache
    # ------------------------------------------------------------------

    def set_keys_tried(self, job_id: int, count: int) -> None:
        self._keys_tried[job_id] = count

    def get_keys_tried(self, job_id: int) -> int:
        return self._keys_tried.get(job_id, 0)

    def clear_keys_tried(self, job_id: int) -> None:
        self._keys_tried.pop(job_id, None)

    # ------------------------------------------------------------------
    # Download progress cache
    # ------------------------------------------------------------------

    def set_download_progress(self, job_id: int, data: dict) -> None:
        self._download_progress[job_id] = data

    def get_download_progress(self, job_id: int) -> dict | None:
        return self._download_progress.get(job_id)

    def clear_download_progress(self, job_id: int) -> None:
        self._download_progress.pop(job_id, None)

    # ------------------------------------------------------------------
    # Agent wordlist preload progress
    # ------------------------------------------------------------------

    def set_preload_progress(self, agent_id: int, wordlist_id: int, data: dict) -> None:
        self._preload_progress[(agent_id, wordlist_id)] = data

    def get_preload_progress(self, agent_id: int, wordlist_id: int) -> dict | None:
        return self._preload_progress.get((agent_id, wordlist_id))

    # ------------------------------------------------------------------
    # Agent connections
    # ------------------------------------------------------------------

    async def register_agent(self, agent_id: int, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._agent_conns[agent_id] = ws
        logger.info("Agent %d connected via WebSocket", agent_id)

    async def unregister_agent(self, agent_id: int) -> None:
        async with self._lock:
            self._agent_conns.pop(agent_id, None)
        logger.info("Agent %d disconnected", agent_id)

    def agent_connected(self, agent_id: int) -> bool:
        return agent_id in self._agent_conns

    async def send_to_agent(self, agent_id: int, payload: dict) -> bool:
        ws = self._agent_conns.get(agent_id)
        if ws is None:
            return False
        try:
            await ws.send_text(json.dumps(payload))
            return True
        except Exception:
            await self.unregister_agent(agent_id)
            return False


# Module-level singleton
ws_manager = ConnectionManager()
