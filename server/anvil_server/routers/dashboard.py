"""Dashboard router — main landing page with role-aware views."""
from __future__ import annotations
import ipaddress
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models.agent import Agent, AgentHealth
from ..models.customer import Customer
from ..models.job import Job, JobStatus
from ..models.user import User, UserRole
from ..services.auth_service import get_current_user
from ..services.ws_manager import ws_manager
from ..config import settings
from .. import templates

router = APIRouter()
logger = logging.getLogger("anvil.dashboard")


async def _latest_health(db: AsyncSession, agent_ids: list[int]) -> dict:
    """Return {agent_id: AgentHealth} for the most recent snapshot per agent."""
    if not agent_ids:
        return {}
    from sqlalchemy import func as sqlfunc
    latest_subq = (
        select(AgentHealth.agent_id, sqlfunc.max(AgentHealth.id).label("max_id"))
        .where(AgentHealth.agent_id.in_(agent_ids))
        .group_by(AgentHealth.agent_id)
        .subquery()
    )
    rows = (await db.execute(
        select(AgentHealth).join(latest_subq, AgentHealth.id == latest_subq.c.max_id)
    )).scalars().all()
    return {h.agent_id: h for h in rows}


def _agent_live_entry(agent, health) -> dict:
    """Build the per-agent dict for the /api/dashboard/live response."""
    # Pull GPU model name and total VRAM from static capabilities
    gpu_name = None
    vram_total_gb = None
    if agent.capabilities:
        caps_gpus = agent.capabilities.get("gpus", [])
        if caps_gpus:
            gpu_name = caps_gpus[0].get("name")
            vram_mb = caps_gpus[0].get("vram_mb")
            if vram_mb:
                vram_total_gb = round(vram_mb / 1024, 1)

    entry = {"id": agent.id, "name": agent.name, "is_online": agent.is_online,
             "gpu_util": None, "gpu_temp": None, "cpu_util": None, "ram_pct": None,
             "disk_free_gb": None, "disk_total_gb": None,
             "gpu_name": gpu_name, "vram_total_gb": vram_total_gb, "vram_used_gb": None}
    if health:
        if health.gpu_util_pct is not None:
            entry["gpu_util"] = round(health.gpu_util_pct, 1)
        if health.gpu_temp_c is not None:
            entry["gpu_temp"] = round(health.gpu_temp_c, 1)
        if health.cpu_util_pct is not None:
            entry["cpu_util"] = round(health.cpu_util_pct, 1)
        if health.ram_used_mb and health.ram_total_mb:
            entry["ram_pct"] = round(health.ram_used_mb / health.ram_total_mb * 100, 1)
        if health.disk_free_gb is not None and health.disk_total_gb:
            entry["disk_free_gb"] = round(health.disk_free_gb, 1)
            entry["disk_total_gb"] = round(health.disk_total_gb, 1)
        if health.gpu_mem_util_pct is not None and vram_total_gb:
            entry["vram_used_gb"] = round(health.gpu_mem_util_pct / 100 * vram_total_gb, 1)
    return entry


class _KioskUser:
    """
    Synthetic presentation-mode user for kiosk IPs.
    Does NOT require a presentation account in the database.
    Provides the same interface as the User ORM model used in templates.
    """
    id = None
    username = "kiosk"
    role = UserRole.PRESENTATION
    is_active = True

    def can_view_credentials(self) -> bool: return False
    def can_create_jobs(self) -> bool:      return False
    def can_manage_users(self) -> bool:     return False
    def can_manage_agents(self) -> bool:    return False


def _normalize_ip(ip: str) -> str:
    """
    Normalize an IP string to plain IPv4 where possible.

    When Uvicorn listens on '::' (dual-stack), IPv4 clients arrive as
    IPv4-mapped IPv6 addresses (e.g. '::ffff:192.168.1.55').  The allowlist
    stores plain IPv4 ('192.168.1.55'), so a direct string/object comparison
    fails intermittently — causing the kiosk to land on the login screen.
    Unwrapping the mapped address before comparison fixes the mismatch.
    """
    try:
        addr = ipaddress.ip_address(ip)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
    except ValueError:
        pass
    return ip


def _is_kiosk_ip(client_ip: str) -> bool:
    """Return True if client_ip is in the configured kiosk_allowlist."""
    allowlist = settings.server.kiosk_allowlist
    if not allowlist:
        return False
    client_ip = _normalize_ip(client_ip)
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        logger.debug("kiosk check: cannot parse client IP %r", client_ip)
        return False
    for entry in allowlist:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            else:
                if addr == ipaddress.ip_address(entry):
                    return True
        except ValueError:
            continue
    return False


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    client_ip = request.client.host if request.client else ""
    if _is_kiosk_ip(client_ip):
        # Kiosk bypass — no login required, force presentation mode.
        # Uses a synthetic user object; no DB account needed.
        logger.debug("kiosk bypass for %s", client_ip)
        user: User | _KioskUser = _KioskUser()
    else:
        user = await get_current_user(request=request, db=db)
    # Stats visible to all roles
    total_jobs = (await db.execute(select(func.count()).select_from(Job))).scalar()
    running_jobs = (await db.execute(
        select(func.count()).select_from(Job).where(
            Job.status.in_([JobStatus.RUNNING, JobStatus.QUEUED])
        )
    )).scalar()
    completed_jobs = (await db.execute(
        select(func.count()).select_from(Job).where(Job.status == JobStatus.COMPLETED)
    )).scalar()

    # Recent jobs (last 10)
    recent_q = select(Job).order_by(Job.created_at.desc()).limit(10)
    recent_jobs = (await db.execute(recent_q)).scalars().all()

    # Agent status + latest health
    agents = (await db.execute(select(Agent).where(Agent.is_active == True))).scalars().all()
    health_by_agent = await _latest_health(db, [a.id for a in agents])

    # Customer count
    total_customers = (await db.execute(select(func.count()).select_from(Customer))).scalar() or 0

    # Aggregate crack stats
    total_hashes = (await db.execute(select(func.sum(Job.total_hashes)))).scalar() or 0
    total_cracked = (await db.execute(select(func.sum(Job.cracked_count)))).scalar() or 0

    ctx = {
        "user": user,
        "total_jobs": total_jobs,
        "total_customers": total_customers,
        "running_jobs": running_jobs,
        "completed_jobs": completed_jobs,
        "recent_jobs": recent_jobs,
        "agents": agents,
        "health_by_agent": health_by_agent,
        "total_hashes": total_hashes,
        "total_cracked": total_cracked,
        "overall_crack_pct": round(total_cracked / total_hashes * 100, 1) if total_hashes else 0,
        "show_credentials": user.can_view_credentials(),
        "is_presentation": user.role == UserRole.PRESENTATION,
    }
    return templates.TemplateResponse(request, "dashboard/index.html", ctx)


@router.get("/api/dashboard/live")
async def dashboard_live(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Lightweight JSON endpoint polled by the dashboard every ~10s to update
    live running-job stats without a full page reload.
    Accessible to kiosk IPs without a session cookie.
    """
    client_ip = request.client.host if request.client else ""
    if not _is_kiosk_ip(client_ip):
        user = await get_current_user(request=request, db=db)
    else:
        user = None  # presentation context — no credentials shown

    running = (await db.execute(
        select(Job).where(Job.status.in_([JobStatus.RUNNING, JobStatus.QUEUED]))
    )).scalars().all()

    # Bulk-fetch wordlist names for running jobs
    from ..models.wordlist import Wordlist
    wl_ids = [j.wordlist_id for j in running if j.wordlist_id]
    wordlist_names: dict[int, str] = {}
    if wl_ids:
        wl_rows = (await db.execute(
            select(Wordlist.id, Wordlist.name).where(Wordlist.id.in_(wl_ids))
        )).all()
        wordlist_names = {row.id: row.name for row in wl_rows}

    # Bulk-fetch rule names for running jobs
    from ..models.wordlist import Rule
    all_rule_ids = list({rid for j in running if j.rules_json for rid in j.rules_json})
    rule_names: dict[int, str] = {}
    if all_rule_ids:
        r_rows = (await db.execute(
            select(Rule.id, Rule.name).where(Rule.id.in_(all_rule_ids))
        )).all()
        rule_names = {row.id: row.name for row in r_rows}

    agents = (await db.execute(select(Agent).where(Agent.is_active == True))).scalars().all()
    health_by_agent = await _latest_health(db, [a.id for a in agents])

    total_hashes = (await db.execute(select(func.sum(Job.total_hashes)))).scalar() or 0
    total_cracked = (await db.execute(select(func.sum(Job.cracked_count)))).scalar() or 0
    running_count = (await db.execute(
        select(func.count()).select_from(Job).where(
            Job.status.in_([JobStatus.RUNNING, JobStatus.QUEUED])
        )
    )).scalar()

    total_jobs = (await db.execute(select(func.count()).select_from(Job))).scalar() or 0

    two_min_ago = datetime.utcnow() - timedelta(minutes=2)
    recently_done = (await db.execute(
        select(Job)
        .where(
            Job.status.in_([JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]),
            Job.completed_at >= two_min_ago,
        )
        .order_by(Job.completed_at.desc())
        .limit(3)
    )).scalars().all()

    return JSONResponse({
        "total_jobs": total_jobs,
        "running_jobs": running_count,
        "total_cracked": total_cracked,
        "total_hashes": total_hashes,
        "overall_crack_pct": round(total_cracked / total_hashes * 100, 1) if total_hashes else 0,
        "agents": [
            _agent_live_entry(a, health_by_agent.get(a.id))
            for a in agents
        ],
        "active_jobs": [
            {
                "id": j.id,
                "name": j.name,
                "status": j.status.value,
                "phase": j.phase,
                "progress_pct": round(j.progress_pct, 1),
                "hashrate": j.hashrate or 0,
                "cracked_count": j.cracked_count,
                "total_hashes": j.total_hashes,
                "eta_seconds": j.eta_seconds,
                "current_candidate": ws_manager.get_candidate(j.id),
                "keys_tried": j.keys_tried or ws_manager.get_keys_tried(j.id),
                "hash_type_name": j.hash_type_name or (f"#{j.hash_type}" if j.hash_type else "Unknown"),
                "started_at_ts": j.started_at.replace(tzinfo=timezone.utc).timestamp() if j.started_at else None,
                "wordlist_name": wordlist_names.get(j.wordlist_id) if j.wordlist_id else None,
                "rule_names": [rule_names[rid] for rid in (j.rules_json or []) if rid in rule_names],
                "download_progress": (
                    ws_manager.get_download_progress(j.id)
                    or ({"filename": wordlist_names.get(j.wordlist_id, ""), "pct": 0,
                         "bytes_done": 0, "bytes_total": 0}
                        if j.phase == "downloading" else None)
                ),
            }
            for j in running
        ],
        "recently_completed": [
            {
                "id": j.id,
                "name": j.name,
                "status": j.status.value,
                "cracked_count": j.cracked_count or 0,
                "total_hashes": j.total_hashes or 0,
            }
            for j in recently_done
        ],
    })
