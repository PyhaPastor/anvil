"""Auth router — login / logout / password change (browser UI)."""
from __future__ import annotations
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models.user import User
from ..services import auth_service, audit_service
from ..config import settings
from .. import templates

router = APIRouter()


def _set_session_cookie(response, token: str):
    response.set_cookie(
        key="anvil_session",
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=settings.server.session_max_age,
    )
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    error: str = "",
    db: AsyncSession = Depends(get_db),
):
    user = await auth_service.get_current_user_optional(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(request, "auth/login.html", {"error": error})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if user is None or not auth_service.verify_password(password, user.password_hash):
        await audit_service.log_action(db, "login_failed", details={"username": username}, ip_address=ip)
        return RedirectResponse("/login?error=Invalid+username+or+password", status_code=302)

    if not user.is_active:
        return RedirectResponse("/login?error=Account+disabled", status_code=302)

    user.last_login = datetime.utcnow()
    await audit_service.log_action(db, "login_success", user_id=user.id, ip_address=ip)

    token = auth_service.create_access_token(user.id, user.role.value)
    redirect_to = "/change-password" if user.force_password_change else "/dashboard"
    response = RedirectResponse(redirect_to, status_code=302)
    _set_session_cookie(response, token)
    return response


@router.get("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    user = await auth_service.get_current_user_optional(request, db)
    if user:
        await audit_service.log_action(db, "logout", user_id=user.id)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("anvil_session")
    return response


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await auth_service.get_current_user(request, db)
    return templates.TemplateResponse(request, "auth/change_password.html", {"user": user})


@router.post("/change-password")
async def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await auth_service.get_current_user(request, db)
    errors = []

    if not auth_service.verify_password(current_password, user.password_hash):
        errors.append("Current password is incorrect.")
    if new_password != confirm_password:
        errors.append("New passwords do not match.")
    if len(new_password) < 12:
        errors.append("Password must be at least 12 characters.")
    if new_password == current_password:
        errors.append("New password must differ from current password.")

    if errors:
        return templates.TemplateResponse(
            request, "auth/change_password.html", {"user": user, "errors": errors},
        )

    user.password_hash = auth_service.hash_password(new_password)
    user.force_password_change = False
    await audit_service.log_action(db, "password_changed", user_id=user.id)
    return RedirectResponse("/dashboard", status_code=302)
