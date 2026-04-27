"""Agent API — REST endpoints for the Anvil cracking agent."""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...models.agent import Agent, AgentHealth
from ...models.job import Job, JobStatus
from ...models.hash_list import HashList, Hash
from ...models.wordlist import Wordlist, Rule
from ...services.auth_service import get_agent_from_token
from ...services.ws_manager import ws_manager
from ...services.notification_service import notify_job_complete

router = APIRouter(prefix="/api/v1/agent")


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class CapabilitiesPayload(BaseModel):
    hostname: str = ""
    gpus: List[dict] = []
    cpu_count: int = 0
    os: str = ""


class CachedWordlistEntry(BaseModel):
    name: str
    size_bytes: int
    wordlist_id: Optional[int] = None   # server-side ID if known


class HeartbeatPayload(BaseModel):
    gpu_temp_c: Optional[float] = None
    gpu_util_pct: Optional[float] = None
    gpu_mem_util_pct: Optional[float] = None
    cpu_util_pct: Optional[float] = None
    ram_used_mb: Optional[float] = None
    ram_total_mb: Optional[float] = None
    disk_free_gb: Optional[float] = None
    disk_total_gb: Optional[float] = None
    hashrate_hs: Optional[float] = None
    current_job_id: Optional[int] = None
    progress_pct: Optional[float] = None
    eta_seconds: Optional[int] = None
    phase: Optional[str] = None
    current_candidate: Optional[str] = None
    wordlist_cache: Optional[List[CachedWordlistEntry]] = None


class CrackResultEntry(BaseModel):
    hash_value: str
    plaintext: str
    time_to_crack_seconds: Optional[float] = None


class JobResultPayload(BaseModel):
    job_id: int
    status: str    # "completed" | "failed"
    cracked: List[CrackResultEntry] = []
    error_message: Optional[str] = None
    final_progress_pct: float = 100.0


class HashIdentifyPayload(BaseModel):
    sample: str     # single hash line to identify


class ProvisionPayload(BaseModel):
    provisioning_key: str
    name: str           # agent name (defaults to hostname on the install side)
    hostname: str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/provision")
async def provision_agent(
    payload: ProvisionPayload,
    db: AsyncSession = Depends(get_db),
):
    """
    Zero-touch agent registration.  Called by the install script using the
    provisioning_key embedded at serve-time.  Returns a fresh API token so the
    install script can write config.toml without any dashboard interaction.

    Re-running the install on the same hostname rotates the existing agent's
    token instead of creating a duplicate record.
    """
    import hmac
    from ...config import settings
    from ...services.auth_service import create_agent_token, hash_agent_token

    if not settings.agent.provisioning_key:
        raise HTTPException(
            status_code=503,
            detail="Auto-provisioning is not configured on this server. "
                   "Re-run server/setup.sh or register the agent manually.",
        )

    # Constant-time comparison — prevents timing attacks on the shared key
    if not hmac.compare_digest(payload.provisioning_key, settings.agent.provisioning_key):
        raise HTTPException(status_code=401, detail="Invalid provisioning key.")

    # Re-install: reuse the existing active agent record for this hostname
    existing = None
    if payload.hostname:
        result = await db.execute(
            select(Agent).where(
                Agent.hostname == payload.hostname,
                Agent.is_active == True,
            )
        )
        existing = result.scalar_one_or_none()

    if existing:
        agent = existing
        agent.name = payload.name  # allow rename on re-provision
    else:
        agent = Agent(name=payload.name, api_token_hash="pending")
        db.add(agent)
        await db.flush()

    # Always persist hostname so re-installs find the same record
    if payload.hostname:
        agent.hostname = payload.hostname

    raw_token = create_agent_token(agent.id)
    agent.api_token_hash = hash_agent_token(raw_token)
    await db.commit()

    return {"api_token": raw_token, "agent_id": agent.id, "agent_name": agent.name}


@router.post("/capabilities")
async def update_capabilities(
    request: Request,
    payload: CapabilitiesPayload,
    agent: Agent = Depends(get_agent_from_token),
    db: AsyncSession = Depends(get_db),
):
    agent.capabilities = payload.model_dump()
    agent.hostname = payload.hostname or agent.hostname
    # Record the agent's IP address from the connection
    if request.client:
        agent.ip_address = request.client.host
    return {"status": "ok"}


@router.post("/heartbeat")
async def heartbeat(
    payload: HeartbeatPayload,
    agent: Agent = Depends(get_agent_from_token),
    db: AsyncSession = Depends(get_db),
):
    snap = AgentHealth(
        agent_id=agent.id,
        gpu_temp_c=payload.gpu_temp_c,
        gpu_util_pct=payload.gpu_util_pct,
        gpu_mem_util_pct=payload.gpu_mem_util_pct,
        cpu_util_pct=payload.cpu_util_pct,
        ram_used_mb=payload.ram_used_mb,
        ram_total_mb=payload.ram_total_mb,
        disk_free_gb=payload.disk_free_gb,
        disk_total_gb=payload.disk_total_gb,
        hashrate_hs=payload.hashrate_hs,
        current_job_id=payload.current_job_id,
    )
    db.add(snap)

    # Update running job progress and phase
    if payload.current_job_id:
        job = await db.get(Job, payload.current_job_id)
        if job:
            if payload.progress_pct is not None:
                job.progress_pct = payload.progress_pct
            if payload.hashrate_hs is not None:
                job.hashrate = payload.hashrate_hs
            if payload.eta_seconds is not None:
                job.eta_seconds = payload.eta_seconds
            if payload.phase is not None:
                job.phase = payload.phase
            if payload.current_candidate:
                ws_manager.set_candidate(job.id, payload.current_candidate)
            # Broadcast live progress to browser clients

            await ws_manager.broadcast_job_update(job.id, {
                "type": "progress",
                "job_id": job.id,
                "progress_pct": payload.progress_pct,
                "hashrate_hs": payload.hashrate_hs,
                "eta_seconds": payload.eta_seconds,
                "gpu_temp_c": payload.gpu_temp_c,
                "gpu_util_pct": payload.gpu_util_pct,
                "phase": job.phase,
                "current_candidate": payload.current_candidate,
            })
    # Update cached wordlist inventory
    if payload.wordlist_cache is not None:
        agent.wordlist_cache = [e.model_dump() for e in payload.wordlist_cache]

    return {"status": "ok"}


@router.delete("/cache/wordlist")
async def delete_cached_wordlist(
    name: str,
    agent: Agent = Depends(get_agent_from_token),
    db: AsyncSession = Depends(get_db),
):
    """Agent calls this to confirm a deletion; server clears it from the cache inventory."""
    if agent.wordlist_cache:
        agent.wordlist_cache = [e for e in agent.wordlist_cache if e.get("name") != name]
    return {"status": "ok"}


@router.get("/jobs/next")
async def get_next_job(
    agent: Agent = Depends(get_agent_from_token),
    db: AsyncSession = Depends(get_db),
):
    """Return the highest-priority queued job assigned to this agent."""
    q = (
        select(Job)
        .where(Job.agent_id == agent.id, Job.status == JobStatus.QUEUED)
        .order_by(Job.priority.asc(), Job.created_at.asc())
        .limit(1)
    )
    job = (await db.execute(q)).scalar_one_or_none()
    if not job:
        return {"job": None}

    job.status = JobStatus.RUNNING
    job.started_at = datetime.utcnow()

    # Fetch hash lists (agent will download by ID)
    hash_lists = (await db.execute(
        select(HashList).where(HashList.job_id == job.id)
    )).scalars().all()

    # Fetch wordlist metadata (agent will download by ID if needed)
    wordlist_meta = None
    if job.wordlist_id:
        wl = await db.get(Wordlist, job.wordlist_id)
        if wl:
            wordlist_meta = {
                "id": wl.id,
                "name": Path(wl.file_path).name,
                "size": wl.file_size_bytes,
            }

    # Fetch rule metadata
    rule_metas = []
    if job.rules_json:
        for rule_id in job.rules_json:
            r = await db.get(Rule, rule_id)
            if r:
                rule_metas.append({
                    "id": r.id,
                    "name": Path(r.file_path).name,
                })

    return {
        "job": {
            "id": job.id,
            "attack_mode": job.attack_mode,
            "hash_type": job.hash_type,      # None = let agent auto-detect
            "hash_lists": [
                {"id": hl.id, "name": Path(hl.file_path).name, "size": hl.total_hashes}
                for hl in hash_lists
            ],
            "wordlist": wordlist_meta,
            "rules": rule_metas,
            "mask": job.mask if job.mask and job.mask.strip() else None,
            "extra_flags": job.extra_flags if job.extra_flags and job.extra_flags.strip() else None,
        }
    }


@router.get("/files/hashlist/{hash_list_id}")
async def download_hash_list(
    hash_list_id: int,
    agent: Agent = Depends(get_agent_from_token),
    db: AsyncSession = Depends(get_db),
):
    """Authenticated: stream the hash list file to the agent."""
    hl = await db.get(HashList, hash_list_id)
    if not hl:
        raise HTTPException(status_code=404, detail="Hash list not found")
    p = Path(hl.file_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Hash list file missing on server")
    return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")


@router.get("/files/wordlist/{wordlist_id}")
async def download_wordlist(
    wordlist_id: int,
    agent: Agent = Depends(get_agent_from_token),
    db: AsyncSession = Depends(get_db),
):
    """Authenticated: stream the wordlist file to the agent."""
    wl = await db.get(Wordlist, wordlist_id)
    if not wl:
        raise HTTPException(status_code=404, detail="Wordlist not found")
    p = Path(wl.file_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Wordlist file missing on server")
    return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")


@router.get("/files/rule/{rule_id}")
async def download_rule(
    rule_id: int,
    agent: Agent = Depends(get_agent_from_token),
    db: AsyncSession = Depends(get_db),
):
    """Authenticated: stream the rule file to the agent."""
    r = await db.get(Rule, rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="Rule not found")
    p = Path(r.file_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Rule file missing on server")
    return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")


@router.post("/jobs/result")
async def submit_job_result(
    payload: JobResultPayload,
    agent: Agent = Depends(get_agent_from_token),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(Job, payload.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Job not assigned to this agent")

    try:
        job.status = JobStatus(payload.status)
    except ValueError:
        job.status = JobStatus.FAILED

    job.completed_at = datetime.utcnow()
    job.progress_pct = payload.final_progress_pct
    job.error_message = payload.error_message
    job.phase = None  # clear activity phase on completion
    ws_manager.clear_candidate(job.id)

    if payload.cracked:
        hash_lists = (await db.execute(
            select(HashList).where(HashList.job_id == job.id)
        )).scalars().all()
        hash_list_ids = [hl.id for hl in hash_lists]

        # Track unique hash values cracked — this is the deduplicated crack count.
        # We also update ALL rows with the same hash_value (different usernames get their
        # plaintext filled in too, because they share the same password).
        unique_cracked: set[str] = set()
        now = datetime.utcnow()

        for entry in payload.cracked:
            # Find all rows with this hash value (may span usernames / duplicates)
            result = await db.execute(
                select(Hash)
                .where(
                    Hash.hash_list_id.in_(hash_list_ids),
                    Hash.hash_value == entry.hash_value,
                    Hash.plaintext.is_(None),
                )
            )
            rows = result.scalars().all()
            for h in rows:
                h.plaintext = entry.plaintext
                h.cracked_at = now
                h.time_to_crack_seconds = entry.time_to_crack_seconds
            if rows:
                unique_cracked.add(entry.hash_value)

        job.cracked_count = len(unique_cracked)
        # Update hash list counts proportionally (all belong to same job here)
        for hl in hash_lists:
            hl.cracked_count = job.cracked_count

    await ws_manager.broadcast_job_update(job.id, {
        "type": "completed",
        "job_id": job.id,
        "status": job.status.value,
        "cracked": job.cracked_count,
        "total": job.total_hashes,
    })

    await notify_job_complete(
        job_id=job.id, job_name=job.name,
        cracked=job.cracked_count, total=job.total_hashes,
    )

    return {"status": "accepted"}


@router.post("/identify-hash")
async def identify_hash(
    payload: HashIdentifyPayload,
    agent: Agent = Depends(get_agent_from_token),
):
    """Return hashcat mode candidates for a given hash sample (server-side lookup)."""
    from ...hashcat_modes import identify_hash_modes
    modes = identify_hash_modes(payload.sample)
    return {"modes": modes}


@router.get("/health")
async def agent_health_check(agent: Agent = Depends(get_agent_from_token)):
    return {"status": "ok", "agent_id": agent.id, "agent_name": agent.name}
