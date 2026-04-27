"""Job templates (attack profiles) router."""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models.template import JobTemplate
from ..models.wordlist import Wordlist
from ..models.user import User
from ..services.auth_service import require_analyst
from ..services import audit_service
from ..hashcat_modes import HASHCAT_MODES, FAVOURITE_MODES
from .. import templates

router = APIRouter(prefix="/templates")


@router.get("", response_class=HTMLResponse)
async def list_templates(
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
):
    tmpl_list = (await db.execute(select(JobTemplate).order_by(JobTemplate.name))).scalars().all()
    return templates.TemplateResponse(request, "templates_mgr/list.html", {
        "user": user, "templates": tmpl_list,
    })


@router.get("/new", response_class=HTMLResponse)
async def new_template_form(
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
):
    wordlists = (await db.execute(select(Wordlist).order_by(Wordlist.name))).scalars().all()
    return templates.TemplateResponse(request, "templates_mgr/form.html", {
        "user": user, "template": None,
        "wordlists": wordlists,
        "hashcat_modes": HASHCAT_MODES, "favourite_modes": FAVOURITE_MODES,
    })


@router.post("/new")
async def create_template(
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    description: str = Form(""),
    attack_mode: int = Form(0),
    hash_type: Optional[int] = Form(None),
    wordlist_id: Optional[int] = Form(None),
    mask: Optional[str] = Form(None),
    extra_flags: Optional[str] = Form(None),
):
    t = JobTemplate(
        name=name, description=description or None,
        attack_mode=attack_mode, hash_type=hash_type,
        wordlist_id=wordlist_id, mask=mask or None,
        extra_flags=extra_flags or None, created_by_id=user.id,
    )
    db.add(t)
    await db.flush()
    await audit_service.log_action(db, "template_created", user_id=user.id,
                                    resource_type="template", resource_id=t.id)
    return RedirectResponse("/templates", status_code=302)


@router.post("/{template_id}/delete")
async def delete_template(
    template_id: int,
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
):
    t = await db.get(JobTemplate, template_id)
    if not t:
        raise HTTPException(status_code=404)
    await db.delete(t)
    await audit_service.log_action(db, "template_deleted", user_id=user.id,
                                    resource_type="template", resource_id=template_id)
    return RedirectResponse("/templates", status_code=302)
