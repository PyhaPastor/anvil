"""Jobs router — create, view, manage cracking jobs."""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models.customer import Customer
from ..models.job import Job, JobStatus, AttackMode
from ..models.hash_list import HashList, Hash
from ..models.wordlist import Wordlist, Rule
from ..models.template import JobTemplate
from ..models.agent import Agent
from ..models.user import User, UserRole
from ..services.auth_service import get_current_user, require_analyst
from ..services import audit_service, export_service
from ..services.upload_service import save_upload
from ..services.ws_manager import ws_manager
from ..config import settings
from ..hashcat_modes import HASHCAT_MODES, FAVOURITE_MODES
from .. import templates

# Upload size limits
_ADMIN_MAX_HASHLIST_BYTES = 100 * 1024 ** 3   # 100 GB (effectively unlimited)
_NONADMIN_MAX_HASHLIST_BYTES = 30 * 1024 ** 3  # 30 GB

router = APIRouter(prefix="/jobs")


@router.get("", response_class=HTMLResponse)
async def list_jobs(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = None,
    customer_id: Optional[int] = None,
):
    q = select(Job).order_by(Job.created_at.desc())
    if status_filter:
        try:
            q = q.where(Job.status == JobStatus(status_filter))
        except ValueError:
            pass
    if customer_id:
        q = q.where(Job.customer_id == customer_id)
    jobs = (await db.execute(q)).scalars().all()
    customers = (await db.execute(select(Customer).order_by(Customer.name))).scalars().all()
    running_count = (await db.execute(
        select(func.count()).select_from(Job).where(Job.status == JobStatus.RUNNING)
    )).scalar() or 0
    total_jobs = (await db.execute(select(func.count()).select_from(Job))).scalar() or 0
    return templates.TemplateResponse(request, "jobs/list.html", {
        "user": user, "jobs": jobs,
        "customers": customers, "status_filter": status_filter,
        "customer_id": customer_id,
        "running_count": running_count,
        "total_jobs": total_jobs,
    })


@router.get("/live")
async def jobs_live(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lightweight endpoint polled by the jobs list page for live updates."""
    total_jobs = (await db.execute(select(func.count()).select_from(Job))).scalar() or 0
    running_count = (await db.execute(
        select(func.count()).select_from(Job).where(Job.status == JobStatus.RUNNING)
    )).scalar() or 0
    active = (await db.execute(
        select(Job).where(Job.status.in_([JobStatus.RUNNING, JobStatus.QUEUED]))
    )).scalars().all()
    return JSONResponse({
        "total_jobs": total_jobs,
        "running_count": running_count,
        "jobs": [
            {
                "id": j.id,
                "status": j.status.value,
                "phase": j.phase,
                "progress_pct": round(j.progress_pct, 1),
                "hashrate": j.hashrate or 0,
                "cracked_count": j.cracked_count,
                "total_hashes": j.total_hashes,
            }
            for j in active
        ],
    })


@router.get("/new", response_class=HTMLResponse)
async def new_job_form(
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
    template_id: Optional[int] = None,
):
    customers  = (await db.execute(select(Customer).order_by(Customer.name))).scalars().all()
    wordlists  = (await db.execute(select(Wordlist).order_by(Wordlist.name))).scalars().all()
    rules      = (await db.execute(select(Rule).order_by(Rule.name))).scalars().all()
    agents     = (await db.execute(select(Agent).where(Agent.is_active == True).order_by(Agent.id.asc()))).scalars().all()
    online_agents = [a for a in agents if a.is_online]
    # Default selection priority:
    # 1. Admin-configured default agent (Settings → Default Agent)
    # 2. Only online agent (auto-select when exactly 1)
    # 3. First registered online agent
    configured_default = settings.agent.default_agent_id  # 0 = not set
    default_agent_id = None
    if configured_default and any(a.id == configured_default for a in agents):
        default_agent_id = configured_default
    elif len(online_agents) == 1:
        default_agent_id = online_agents[0].id
    elif len(online_agents) >= 2:
        default_agent_id = online_agents[0].id  # first registered (lowest id)
    job_templates = (await db.execute(select(JobTemplate).order_by(JobTemplate.name))).scalars().all()

    prefill = None
    if template_id:
        prefill = await db.get(JobTemplate, template_id)

    # Existing hash lists from previous jobs — for the "reuse" option
    existing_hashlists_q = (
        select(HashList, Job)
        .join(Job, HashList.job_id == Job.id)
        .order_by(Job.created_at.desc())
    )
    existing_hashlists_raw = (await db.execute(existing_hashlists_q)).all()
    existing_hashlists = [
        {"id": hl.id, "name": hl.name, "job_name": job.name,
         "total_hashes": hl.total_hashes, "cracked_count": hl.cracked_count}
        for hl, job in existing_hashlists_raw
        if Path(hl.file_path).exists()
    ]

    return templates.TemplateResponse(request, "jobs/new.html", {
        "user": user,
        "customers": customers, "wordlists": wordlists,
        "rules": rules, "agents": agents,
        "online_agents": online_agents,
        "default_agent_id": default_agent_id,
        "job_templates": job_templates, "prefill": prefill,
        "hashcat_modes": HASHCAT_MODES, "favourite_modes": FAVOURITE_MODES,
        "attack_modes": [m for m in AttackMode],
        "existing_hashlists": existing_hashlists,
    })


@router.post("/new")
async def create_job(
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    notes: str = Form(""),
    customer_id: Optional[int] = Form(None),
    agent_id: Optional[int] = Form(None),
    attack_mode: int = Form(0),
    hash_type: Optional[int] = Form(None),
    wordlist_id: Optional[int] = Form(None),
    batch_wordlist_ids: str = Form(""),   # JSON array; if set, one job per wordlist
    rule_ids: str = Form(""),             # JSON array of rule IDs
    mask: Optional[str] = Form(None),
    extra_flags: Optional[str] = Form(None),
    priority: int = Form(5),
    hash_files: List[UploadFile] = File(default=[]),
    existing_hashlist_ids: str = Form(""),   # JSON array of existing HashList IDs
):
    # A customer is required so per-customer hash deletion has a clear scope
    if not customer_id:
        raise HTTPException(status_code=400, detail="A customer must be selected")
    customer = await db.get(Customer, customer_id)
    if not customer:
        raise HTTPException(status_code=400, detail="Selected customer does not exist")

    # Validate agent exists
    if agent_id:
        agent = await db.get(Agent, agent_id)
        if not agent or not agent.is_active:
            raise HTTPException(status_code=400, detail="Selected agent is not available")

    # Filter out empty file slots (browser submits empty input as filename="")
    valid_files = [f for f in hash_files if f.filename]
    existing_ids = json.loads(existing_hashlist_ids) if existing_hashlist_ids.strip() else []

    if not valid_files and not existing_ids:
        raise HTTPException(status_code=400, detail="At least one hash file is required")

    # Per-role upload limit
    max_bytes = (
        _ADMIN_MAX_HASHLIST_BYTES
        if user.role == UserRole.ADMIN
        else _NONADMIN_MAX_HASHLIST_BYTES
    )

    # Validate extra_flags for dangerous options
    if extra_flags:
        _validate_extra_flags(extra_flags)

    rule_list = json.loads(rule_ids) if rule_ids.strip() else []
    batch_ids = json.loads(batch_wordlist_ids) if batch_wordlist_ids.strip() else []

    # Build the list of (wordlist_id, job_name_suffix) pairs to create
    # If batch mode: one job per selected wordlist; otherwise one job with the single wordlist
    if batch_ids:
        wl_records = {wl.id: wl for wl in (
            await db.execute(select(Wordlist).where(Wordlist.id.in_(batch_ids)))
        ).scalars().all()}
        wordlist_slots = [(wid, wl_records[wid].name) for wid in batch_ids if wid in wl_records]
    else:
        wordlist_slots = [(wordlist_id, None)]

    def _parse_and_store(file_path: str) -> tuple[set[str], list[tuple]]:
        with open(file_path, encoding="utf-8", errors="replace") as fh:
            lines = [l.strip() for l in fh if l.strip()]
        file_unique: set[str] = set()
        parsed: list[tuple[Optional[str], str]] = []
        for line in lines:
            parts = line.split(":", 1)
            if len(parts) == 2 and not parts[0].startswith("$"):
                username, hash_val = parts[0], parts[1]
            else:
                username, hash_val = None, line
            parsed.append((username, hash_val))
            file_unique.add(hash_val)
        return file_unique, parsed

    # Pre-save uploaded files once — reused across all batch jobs
    saved_uploads: list[tuple[str, set[str], list[tuple]]] = []
    for upload in valid_files:
        saved_path, _ = await save_upload(
            upload,
            settings.storage.hashlists_dir,
            max_bytes,
            settings.storage.allowed_hashlist_extensions,
        )
        file_unique, parsed = _parse_and_store(saved_path)
        saved_uploads.append((saved_path, file_unique, parsed))

    existing_parsed: list[tuple[str, str, set[str], list[tuple]]] = []
    for hl_id in existing_ids:
        existing_hl = await db.get(HashList, int(hl_id))
        if not existing_hl or not Path(existing_hl.file_path).exists():
            continue
        file_unique, parsed = _parse_and_store(existing_hl.file_path)
        existing_parsed.append((existing_hl.file_path, existing_hl.name, file_unique, parsed))

    first_job_id = None
    for slot_idx, (wl_id, wl_label) in enumerate(wordlist_slots):
        job_name = f"{name} [{wl_label}]" if wl_label else name

        job = Job(
            name=job_name,
            notes=notes or None,
            customer_id=customer_id,
            created_by_id=user.id,
            agent_id=agent_id,
            attack_mode=attack_mode,
            hash_type=hash_type,
            wordlist_id=wl_id,
            rules_json=rule_list,
            mask=mask or None,
            extra_flags=extra_flags or None,
            priority=priority,
            total_hashes=0,
            status=JobStatus.QUEUED if agent_id else JobStatus.PENDING,
        )
        db.add(job)
        await db.flush()
        if first_job_id is None:
            first_job_id = job.id

        all_unique_hashes: set[str] = set()

        for saved_path, file_unique, parsed in saved_uploads:
            all_unique_hashes |= file_unique
            hash_list = HashList(
                job_id=job.id,
                name=Path(saved_path).name,
                file_path=saved_path,
                total_hashes=len(file_unique),
            )
            db.add(hash_list)
            await db.flush()
            for username, hash_val in parsed:
                db.add(Hash(hash_list_id=hash_list.id, username=username, hash_value=hash_val))

        for file_path, hl_name, file_unique, parsed in existing_parsed:
            all_unique_hashes |= file_unique
            hash_list = HashList(
                job_id=job.id,
                name=hl_name,
                file_path=file_path,
                total_hashes=len(file_unique),
            )
            db.add(hash_list)
            await db.flush()
            for username, hash_val in parsed:
                db.add(Hash(hash_list_id=hash_list.id, username=username, hash_value=hash_val))

        job.total_hashes = len(all_unique_hashes)

        await audit_service.log_action(
            db, "job_created", user_id=user.id, resource_type="job",
            resource_id=job.id, details={"name": job_name, "file_count": len(valid_files) + len(existing_ids), "batch": bool(wl_label)},
            ip_address=request.client.host if request.client else None,
        )

    # Batch: go to job list so all queued jobs are visible; single: go to the job detail
    if batch_ids:
        return RedirectResponse("/jobs", status_code=302)
    return RedirectResponse(f"/jobs/{first_job_id}", status_code=302)


@router.get("/{job_id}/live")
async def job_detail_live(
    job_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lightweight polling endpoint for the job detail page."""
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404)
    from ..services.ws_manager import ws_manager
    dl = ws_manager.get_download_progress(job.id)
    if dl is None and job.phase == "downloading":
        dl = {"filename": "", "pct": 0, "bytes_done": 0, "bytes_total": 0}
    return JSONResponse({
        "status": job.status.value,
        "phase": job.phase,
        "progress_pct": round(job.progress_pct or 0, 1),
        "hashrate": job.hashrate,
        "eta_seconds": job.eta_seconds,
        "cracked_count": job.cracked_count,
        "total_hashes": job.total_hashes,
        "download_progress": dl,
    })


@router.get("/{job_id}", response_class=HTMLResponse)
async def job_detail(
    job_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    hash_lists = (await db.execute(
        select(HashList).where(HashList.job_id == job_id)
    )).scalars().all()

    # Only load hash detail for credentialed roles
    hashes = []
    if user.can_view_credentials():
        from sqlalchemy import and_
        hashes = (await db.execute(
            select(Hash)
            .where(Hash.hash_list_id.in_([hl.id for hl in hash_lists]))
            .order_by(Hash.cracked_at.desc().nullslast())
            .limit(500)
        )).scalars().all()

    customer_name = None
    if job.customer:
        customer_name = (
            job.customer.name if user.can_view_credentials()
            else job.customer.presentation_name
        )

    # Wordlist name
    wordlist_name = None
    if job.wordlist_id:
        wl = await db.get(Wordlist, job.wordlist_id)
        if wl:
            wordlist_name = wl.name

    # Rule names
    rule_names = []
    if job.rules_json:
        for rule_id in job.rules_json:
            r = await db.get(Rule, rule_id)
            if r:
                rule_names.append(r.name)

    return templates.TemplateResponse(request, "jobs/detail.html", {
        "user": user, "job": job,
        "hash_lists": hash_lists, "hashes": hashes,
        "customer_name": customer_name,
        "show_credentials": user.can_view_credentials(),
        "wordlist_name": wordlist_name,
        "rule_names": rule_names,
    })


@router.post("/{job_id}/rerun")
async def rerun_job(
    job_id: int,
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
):
    original = await db.get(Job, job_id)
    if not original:
        raise HTTPException(status_code=404)
    if original.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
        raise HTTPException(status_code=400, detail="Only failed or cancelled jobs can be re-run")

    hash_lists = (await db.execute(
        select(HashList).where(HashList.job_id == job_id)
    )).scalars().all()
    if not hash_lists:
        raise HTTPException(status_code=400, detail="Original job has no hash list")
    original_hl = hash_lists[0]
    if not Path(original_hl.file_path).exists():
        raise HTTPException(
            status_code=400,
            detail="Original hash file no longer exists on disk — cannot re-run",
        )

    new_job = Job(
        name=original.name,
        notes=original.notes,
        customer_id=original.customer_id,
        created_by_id=user.id,
        agent_id=original.agent_id,
        attack_mode=original.attack_mode,
        hash_type=original.hash_type,
        hash_type_name=original.hash_type_name,
        wordlist_id=original.wordlist_id,
        rules_json=original.rules_json,
        mask=original.mask,
        extra_flags=original.extra_flags,
        priority=original.priority,
        total_hashes=original.total_hashes,
        status=JobStatus.QUEUED if original.agent_id else JobStatus.PENDING,
    )
    db.add(new_job)
    await db.flush()

    new_hl = HashList(
        job_id=new_job.id,
        name=original_hl.name,
        file_path=original_hl.file_path,
        total_hashes=original_hl.total_hashes,
    )
    db.add(new_hl)
    await db.flush()

    # Re-parse the hash file to create fresh (uncracked) hash records
    with open(original_hl.file_path, encoding="utf-8", errors="replace") as f:
        lines = [l.strip() for l in f if l.strip()]
    for line in lines:
        parts = line.split(":", 1)
        if len(parts) == 2 and not parts[0].startswith("$"):
            username, hash_val = parts[0], parts[1]
        else:
            username, hash_val = None, line
        db.add(Hash(hash_list_id=new_hl.id, username=username, hash_value=hash_val))

    await audit_service.log_action(
        db, "job_rerun", user_id=user.id, resource_type="job",
        resource_id=new_job.id,
        details={"original_job_id": job_id, "name": new_job.name},
        ip_address=request.client.host if request.client else None,
    )
    return RedirectResponse(f"/jobs/{new_job.id}", status_code=302)


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: int,
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404)
    if job.status not in (JobStatus.RUNNING, JobStatus.QUEUED, JobStatus.PENDING):
        raise HTTPException(status_code=400, detail="Job cannot be cancelled in its current state")
    job.status = JobStatus.CANCELLED
    if job.agent_id:
        await ws_manager.send_to_agent(job.agent_id, {"type": "cancel_job", "job_id": job_id})
    await audit_service.log_action(db, "job_cancelled", user_id=user.id, resource_type="job", resource_id=job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=302)


@router.get("/{job_id}/export/csv")
async def export_csv(
    job_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404)
    hash_lists = (await db.execute(select(HashList).where(HashList.job_id == job_id))).scalars().all()
    hashes = (await db.execute(
        select(Hash).where(Hash.hash_list_id.in_([hl.id for hl in hash_lists]))
    )).scalars().all()
    await audit_service.log_action(db, "export_csv", user_id=user.id, resource_type="job", resource_id=job_id)
    return export_service.export_job_csv(job, list(hashes), user)


@router.get("/{job_id}/export/pdf")
async def export_pdf(
    job_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404)
    hash_lists = (await db.execute(select(HashList).where(HashList.job_id == job_id))).scalars().all()
    hashes = (await db.execute(
        select(Hash).where(Hash.hash_list_id.in_([hl.id for hl in hash_lists]))
    )).scalars().all()
    customer_name = "N/A"
    if job.customer:
        customer_name = (
            job.customer.name if user.can_view_credentials()
            else job.customer.presentation_name
        )
    await audit_service.log_action(db, "export_pdf", user_id=user.id, resource_type="job", resource_id=job_id)
    return export_service.export_job_pdf(job, list(hashes), user, customer_name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DANGEROUS_FLAGS = {
    "--stdout", "--outfile", "--outfile-format", "--potfile-path",
    "--session", "--restore-file-path", "--debug-file",
    "--induction-dir", "--outfile-check-dir",
}

def _validate_extra_flags(flags: str) -> None:
    tokens = flags.split()
    for tok in tokens:
        flag = tok.split("=")[0].lower()
        if flag in _DANGEROUS_FLAGS:
            raise HTTPException(
                status_code=400,
                detail=f"Flag '{flag}' is not permitted in extra_flags for security reasons.",
            )
