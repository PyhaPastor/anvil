"""Users router — admin-only user management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models.user import User, UserRole
from ..services.auth_service import hash_password, require_admin
from ..services import audit_service
from .. import templates

router = APIRouter(prefix="/users")


@router.get("", response_class=HTMLResponse)
async def list_users(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    users = (await db.execute(select(User).order_by(User.username))).scalars().all()
    return templates.TemplateResponse(request, "users/list.html", {
        "user": user, "users": users, "roles": list(UserRole),
    })


@router.get("/new", response_class=HTMLResponse)
async def new_user_form(request: Request, user: User = Depends(require_admin)):
    return templates.TemplateResponse(request, "users/form.html", {
        "user": user, "edit_user": None, "roles": list(UserRole),
    })


@router.post("/new")
async def create_user(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
):
    if len(password) < 12:
        raise HTTPException(status_code=400, detail="Password must be at least 12 characters")
    try:
        role_enum = UserRole(role)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role")

    existing = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Username already taken")

    new_user = User(
        username=username, email=email,
        password_hash=hash_password(password),
        role=role_enum, is_active=True, force_password_change=True,
    )
    db.add(new_user)
    await db.flush()
    await audit_service.log_action(db, "user_created", user_id=user.id,
                                    resource_type="user", resource_id=new_user.id,
                                    details={"username": username, "role": role})
    return RedirectResponse("/users", status_code=302)


@router.post("/{target_id}/toggle-active")
async def toggle_user_active(
    target_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if target_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    target = await db.get(User, target_id)
    if not target:
        raise HTTPException(status_code=404)
    target.is_active = not target.is_active
    await audit_service.log_action(
        db, "user_toggled", user_id=user.id, resource_type="user", resource_id=target_id,
        details={"active": target.is_active},
    )
    return RedirectResponse("/users", status_code=302)


@router.post("/{target_id}/change-role")
async def change_user_role(
    target_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    role: str = Form(...),
):
    if target_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    target = await db.get(User, target_id)
    if not target:
        raise HTTPException(status_code=404)
    try:
        target.role = UserRole(role)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role")
    await audit_service.log_action(
        db, "user_role_changed", user_id=user.id, resource_type="user", resource_id=target_id,
        details={"new_role": role},
    )
    return RedirectResponse("/users", status_code=302)


@router.post("/{target_id}/reset-password")
async def reset_password(
    target_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    new_password: str = Form(...),
):
    if len(new_password) < 12:
        raise HTTPException(status_code=400, detail="Password must be at least 12 characters")
    target = await db.get(User, target_id)
    if not target:
        raise HTTPException(status_code=404)
    target.password_hash = hash_password(new_password)
    target.force_password_change = True
    await audit_service.log_action(
        db, "password_reset_by_admin", user_id=user.id, resource_type="user", resource_id=target_id,
    )
    return RedirectResponse("/users", status_code=302)
