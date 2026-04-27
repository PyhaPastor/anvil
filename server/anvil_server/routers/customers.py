"""Customers / engagement management router."""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models.customer import Customer
from ..models.user import User
from ..services.auth_service import get_current_user, require_analyst
from ..services import audit_service
from .. import templates

router = APIRouter(prefix="/customers")


@router.get("", response_class=HTMLResponse)
async def list_customers(
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
):
    customers = (await db.execute(select(Customer).order_by(Customer.name))).scalars().all()
    return templates.TemplateResponse(request, "customers/list.html", {
        "user": user, "customers": customers,
    })


@router.get("/new", response_class=HTMLResponse)
async def new_customer_form(
    request: Request,
    user: User = Depends(require_analyst),
):
    return templates.TemplateResponse(request, "customers/form.html", {
        "user": user, "customer": None,
    })


@router.post("/new")
async def create_customer(
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    presentation_name: str = Form(...),
    description: str = Form(""),
):
    c = Customer(
        name=name, presentation_name=presentation_name,
        description=description or None, created_by_id=user.id,
    )
    db.add(c)
    await db.flush()
    await audit_service.log_action(db, "customer_created", user_id=user.id,
                                    resource_type="customer", resource_id=c.id)
    return RedirectResponse("/customers", status_code=302)


@router.get("/{customer_id}/edit", response_class=HTMLResponse)
async def edit_customer_form(
    customer_id: int,
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
):
    c = await db.get(Customer, customer_id)
    if not c:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "customers/form.html", {
        "user": user, "customer": c,
    })


@router.post("/{customer_id}/edit")
async def update_customer(
    customer_id: int,
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    presentation_name: str = Form(...),
    description: str = Form(""),
):
    c = await db.get(Customer, customer_id)
    if not c:
        raise HTTPException(status_code=404)
    c.name = name
    c.presentation_name = presentation_name
    c.description = description or None
    await audit_service.log_action(db, "customer_updated", user_id=user.id,
                                    resource_type="customer", resource_id=customer_id)
    return RedirectResponse("/customers", status_code=302)
