"""Agent API — REST endpoints for the Anvil cracking agent."""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...models.agent import Agent, AgentHealth
from ...models.job import Job, JobStatus
from ...models.hash_list import HashList, Hash
from ...models.wordlist import Wordlist, Rule
from ...services.auth_service import get_agent_from_token
from ...services.ws_manager import ws_manager
from ...services.notification_service import notify_job_complete

logger = logging.getLogger("anvil.server.agent_api")

router = APIRouter(prefix="/api/v1/agent")

# Grace period before a RUNNING job the agent isn't reporting as current is
# treated as orphaned. Protects the brief window between /jobs/next marking a job
# RUNNING and the agent's first heartbeat reporting it as current_job_id.
_ORPHAN_GRACE_SECONDS = 120

# Max values per IN(...) clause when checking recovery, to stay well under the
# SQLite bound-parameter limit for very large hash files.
_IN_CHUNK = 500


# ---------------------------------------------------------------------------
# "Fully recovered hash file" skip helpers
# ---------------------------------------------------------------------------

async def _job_hash_values(db: AsyncSession, job_id: int) -> set[str]:
    """The set of distinct hash values making up a job's hash file."""
    hl_sub = select(HashList.id).where(HashList.job_id == job_id).scalar_subquery()
    rows = (await db.execute(
        select(distinct(Hash.hash_value)).where(Hash.hash_list_id.in_(hl_sub))
    )).scalars().all()
    return set(rows)


async def _recovered_values(db: AsyncSession, customer_id, values: set[str]) -> set[str]:
    """Which of `values` are cracked (plaintext set) anywhere for this customer.

    Cracks live only on the rows of the job that found them (each job keeps its
    own copy of the hash file), so recovery is inherently cross-job: we ask which
    of these hash values have a cracked row in any of the customer's jobs.
    """
    if not values:
        return set()
    found: set[str] = set()
    vals = list(values)
    for i in range(0, len(vals), _IN_CHUNK):
        chunk = vals[i:i + _IN_CHUNK]
        rows = (await db.execute(
            select(distinct(Hash.hash_value))
            .join(HashList, Hash.hash_list_id == HashList.id)
            .join(Job, HashList.job_id == Job.id)
            .where(
                Job.customer_id == customer_id,
                Hash.hash_value.in_(chunk),
                Hash.plaintext.is_not(None),
            )
        )).scalars().all()
        found.update(rows)
    return found


async def _hashfile_fully_recovered(db: AsyncSession, job: Job) -> bool:
    """True when every distinct hash in this job's hash file is already cracked."""
    values = await _job_hash_values(db, job.id)
    if not values:
        return False
    recovered = await _recovered_values(db, job.customer_id, values)
    return values.issubset(recovered)


async def _skip_recovered_duplicate_jobs(db: AsyncSession, completed_job: Job) -> list[int]:
    """After a job finishes, skip any other active job on the *same* hash file
    once that hash file is fully recovered.

    "Same hash file" means an identical set of distinct hash values (so it spans
    batch siblings, reused hash lists, and separately-uploaded copies, across
    agents). Returns the ids that were skipped.
    """
    # Flush this job's freshly-recorded cracks so the recovery check below (a
    # fresh SELECT) sees them — the session has autoflush disabled.
    await db.flush()

    values = await _job_hash_values(db, completed_job.id)
    if not values:
        return []
    recovered = await _recovered_values(db, completed_job.customer_id, values)
    if not values.issubset(recovered):
        return []

    # Only active jobs on the same hash file are candidates. Pre-filter cheaply on
    # customer + unique-hash count, then confirm by comparing the actual value set.
    candidates = (await db.execute(
        select(Job).where(
            Job.id != completed_job.id,
            Job.customer_id == completed_job.customer_id,
            Job.total_hashes == completed_job.total_hashes,
            Job.status.in_([JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RUNNING]),
        )
    )).scalars().all()

    skipped: list[int] = []
    now = datetime.utcnow()
    for cand in candidates:
        if await _job_hash_values(db, cand.id) != values:
            continue
        was_running = cand.status == JobStatus.RUNNING
        cand.status = JobStatus.SKIPPED
        cand.completed_at = now
        cand.phase = None
        cand.error_message = (
            "Skipped — all hashes in this hash file were already recovered "
            f"by job #{completed_job.id}."
        )
        # A running sibling may be on another agent; tell it to stop. The race
        # where that agent then posts its own result is handled in
        # submit_job_result, which won't overwrite a SKIPPED job.
        if was_running and cand.agent_id:
            await ws_manager.send_to_agent(
                cand.agent_id, {"type": "cancel_job", "job_id": cand.id}
            )
        ws_manager.clear_candidate(cand.id)
        ws_manager.clear_recovered(cand.id)
        await ws_manager.broadcast_job_update(cand.id, {
            "type": "completed",
            "job_id": cand.id,
            "status": cand.status.value,
            "cracked": cand.cracked_count,
            "total": cand.total_hashes,
        })
        skipped.append(cand.id)

    if skipped:
        logger.info(
            "Job %d fully recovered its hash file; skipped sibling jobs %s",
            completed_job.id, skipped,
        )
    return skipped


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
            # Mark the feed fresh only when this heartbeat carries real crack
            # progress. The periodic hardware heartbeat (main._heartbeat_loop)
            # always sends a *stale* hashrate_hs — the last value the job runner
            # pushed — but never a progress_pct, so a hung hashcat would keep
            # refreshing this timestamp off that stale hashrate and defeat the
            # dashboard's stall detection. progress_pct only arrives on the
            # runner's per-status-line path, so gate on it.
            if payload.progress_pct is not None:
                job.progress_updated_at = datetime.utcnow()
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

    # Reconcile orphaned RUNNING jobs. The agent runs exactly one job at a time,
    # so any job still marked RUNNING for this agent that it is NOT currently
    # working on was left stranded (agent restart or crash between picking the job
    # up and submitting its result). Without this it would show "running/stalled"
    # on the dashboard forever. Fail those so their state reflects reality; a short
    # grace window protects a job just handed out by /jobs/next whose first
    # heartbeat hasn't yet reported it as current.
    orphan_q = select(Job).where(
        Job.agent_id == agent.id,
        Job.status == JobStatus.RUNNING,
    )
    if payload.current_job_id:
        orphan_q = orphan_q.where(Job.id != payload.current_job_id)
    now = datetime.utcnow()
    for stranded in (await db.execute(orphan_q)).scalars().all():
        if stranded.started_at and (now - stranded.started_at) < timedelta(seconds=_ORPHAN_GRACE_SECONDS):
            continue
        stranded.status = JobStatus.FAILED
        stranded.completed_at = now
        stranded.error_message = (
            "Interrupted — the assigned agent stopped running this job "
            "(agent restart or crash)."
        )
        logger.warning(
            "Agent %d heartbeat (current_job_id=%s): failed orphaned running job %d",
            agent.id, payload.current_job_id, stranded.id,
        )

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
    # Skip any queued job whose hash file is already fully recovered (e.g. by a
    # sibling that finished after this one was queued). Mark it SKIPPED and move
    # on to the next candidate. flush() so the status change is visible to the
    # next query (autoflush is off).
    while True:
        job = (await db.execute(q)).scalar_one_or_none()
        if not job:
            return {"job": None}
        if await _hashfile_fully_recovered(db, job):
            job.status = JobStatus.SKIPPED
            job.completed_at = datetime.utcnow()
            job.phase = None
            job.error_message = (
                "Skipped — all hashes in this hash file were already recovered."
            )
            await db.flush()
            await ws_manager.broadcast_job_update(job.id, {
                "type": "completed",
                "job_id": job.id,
                "status": job.status.value,
                "cracked": job.cracked_count,
                "total": job.total_hashes,
            })
            continue
        break

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

    # The job may have been SKIPPED while the agent was finishing it (a running
    # sibling cracked the last hash and we told this agent to cancel). Keep the
    # SKIPPED status rather than overwriting it with the agent's cancelled/failed
    # result, but still record any hashes it managed to crack.
    was_skipped = job.status == JobStatus.SKIPPED

    if not was_skipped:
        try:
            job.status = JobStatus(payload.status)
        except ValueError:
            job.status = JobStatus.FAILED
        job.completed_at = datetime.utcnow()
        job.progress_pct = payload.final_progress_pct
        job.error_message = payload.error_message
        job.phase = None  # clear activity phase on completion
    ws_manager.clear_candidate(job.id)
    ws_manager.clear_recovered(job.id)

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

    if not was_skipped:
        await notify_job_complete(
            job_id=job.id, job_name=job.name,
            cracked=job.cracked_count, total=job.total_hashes,
        )
        # If this job finished off the hash file, skip any other active job on
        # the same hashes so we don't waste time cracking what's already solved.
        await _skip_recovered_duplicate_jobs(db, job)

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
