"""WebSocket router — browser clients subscribe to live job progress."""
from __future__ import annotations
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services.ws_manager import ws_manager

router = APIRouter()


@router.websocket("/ws/jobs/{job_id}")
async def job_progress_ws(job_id: int, websocket: WebSocket):
    """
    Browser connects here to receive live progress updates for a job.
    No auth cookie check on WS — the job detail page is already auth-gated.
    A shared secret or signed URL token could be added in a future hardening pass.
    """
    await ws_manager.subscribe(job_id, websocket)
    try:
        while True:
            # Keep connection alive; ignore messages from browser
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.unsubscribe(job_id, websocket)


@router.websocket("/ws/agent/{agent_id}")
async def agent_ws(agent_id: int, websocket: WebSocket):
    """
    Agent connects here for bidirectional real-time communication.
    Token validation is performed on the first message.
    """
    import hashlib
    from fastapi import HTTPException
    from sqlalchemy.ext.asyncio import AsyncSession
    from ..database import AsyncSessionLocal
    from ..services.auth_service import decode_access_token, hash_agent_token
    from ..models.agent import Agent
    from sqlalchemy import select

    await websocket.accept()

    # First message must be {"type": "auth", "token": "<bearer>"}
    try:
        raw = await websocket.receive_text()
        msg = json.loads(raw)
    except Exception:
        await websocket.close(code=1008, reason="Auth message required")
        return

    if msg.get("type") != "auth" or not msg.get("token"):
        await websocket.close(code=1008, reason="Invalid auth message")
        return

    raw_token = msg["token"]
    try:
        payload = decode_access_token(raw_token)
    except HTTPException:
        await websocket.close(code=1008, reason="Invalid token")
        return

    if payload.get("type") != "agent" or int(payload["sub"]) != agent_id:
        await websocket.close(code=1008, reason="Token/agent mismatch")
        return

    token_hash = hash_agent_token(raw_token)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.api_token_hash == token_hash)
        )
        agent = result.scalar_one_or_none()
        if not agent or not agent.is_active:
            await websocket.close(code=1008, reason="Agent not found or inactive")
            return

    # Authenticated — hand off to manager
    # Unregister old connection if any
    await ws_manager.unregister_agent(agent_id)
    ws_manager._agent_conns[agent_id] = websocket

    await websocket.send_text(json.dumps({"type": "auth_ok"}))

    try:
        while True:
            raw = await websocket.receive_text()
            # Agent may push ad-hoc status updates via WS (same schema as heartbeat REST)
            data = json.loads(raw)
            if data.get("type") == "progress" and data.get("job_id"):
                # Persist progress to DB so dashboard/list polling sees updated values
                if data.get("progress_pct") is not None:
                    try:
                        from ..database import AsyncSessionLocal
                        from ..models.job import Job
                        async with AsyncSessionLocal() as db:
                            job = await db.get(Job, data["job_id"])
                            if job:
                                job.progress_pct = data["progress_pct"]
                                if data.get("hashrate_hs") is not None:
                                    job.hashrate = data["hashrate_hs"]
                                if data.get("eta_seconds") is not None:
                                    job.eta_seconds = data["eta_seconds"]
                                if data.get("keys_tried"):
                                    job.keys_tried = int(data["keys_tried"])
                                if data.get("phase"):
                                    job.phase = data["phase"]
                                await db.commit()
                    except Exception:
                        logger.exception("Failed to persist progress for job %s", data.get("job_id"))
                if data.get("current_candidate"):
                    ws_manager.set_candidate(data["job_id"], data["current_candidate"])
                if data.get("keys_tried"):
                    ws_manager.set_keys_tried(data["job_id"], int(data["keys_tried"]))
                # Clear download progress once cracking begins
                if data.get("phase") == "cracking":
                    ws_manager.clear_download_progress(data["job_id"])
                await ws_manager.broadcast_job_update(data["job_id"], data)
            elif data.get("type") == "download_progress" and data.get("job_id"):
                ws_manager.set_download_progress(data["job_id"], {
                    "filename": data.get("filename", ""),
                    "pct": data.get("pct", 0),
                    "bytes_done": data.get("bytes_done", 0),
                    "bytes_total": data.get("bytes_total", 0),
                })
                await ws_manager.broadcast_job_update(data["job_id"], data)
            elif data.get("type") == "preload_progress" and data.get("wordlist_id"):
                ws_manager.set_preload_progress(agent_id, data["wordlist_id"], {
                    "status": "downloading",
                    "pct": data.get("pct", 0),
                    "bytes_done": data.get("bytes_done", 0),
                    "bytes_total": data.get("bytes_total", 0),
                })
            elif data.get("type") == "preload_status" and data.get("wordlist_id"):
                ws_manager.set_preload_progress(agent_id, data["wordlist_id"], {
                    "status": data.get("status"),  # complete / error / already_cached
                    "pct": 100 if data.get("status") in ("complete", "already_cached") else 0,
                    "error": data.get("error"),
                })
    except WebSocketDisconnect:
        await ws_manager.unregister_agent(agent_id)
