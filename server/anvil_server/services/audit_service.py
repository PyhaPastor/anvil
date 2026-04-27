"""Audit logging service."""
from __future__ import annotations
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditLog


async def log_action(
    db: AsyncSession,
    action: str,
    user_id: Optional[int] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> None:
    """Append an audit entry. Silently ignores write errors to avoid breaking main flows."""
    try:
        entry = AuditLog(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            ip_address=ip_address,
        )
        db.add(entry)
        await db.flush()
    except Exception:
        pass  # Audit failures must never block the primary operation
