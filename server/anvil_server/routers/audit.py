"""Audit log viewer — admin only."""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models.audit import AuditLog
from ..models.user import User
from ..services.auth_service import require_admin
from .. import templates

router = APIRouter(prefix="/audit")


@router.get("", response_class=HTMLResponse)
async def audit_log(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    page: int = 1,
    action_filter: Optional[str] = None,
    user_filter: Optional[int] = None,
):
    PAGE_SIZE = 100
    q = select(AuditLog).order_by(AuditLog.timestamp.desc())
    if action_filter:
        q = q.where(AuditLog.action == action_filter)
    if user_filter:
        q = q.where(AuditLog.user_id == user_filter)
    q = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    entries = (await db.execute(q)).scalars().all()

    # Distinct actions for filter dropdown
    actions_result = await db.execute(
        select(AuditLog.action).distinct().order_by(AuditLog.action)
    )
    distinct_actions = [r[0] for r in actions_result]

    return templates.TemplateResponse(request, "audit/list.html", {
        "user": user,
        "entries": entries, "page": page,
        "distinct_actions": distinct_actions,
        "action_filter": action_filter,
        "user_filter": user_filter,
    })
