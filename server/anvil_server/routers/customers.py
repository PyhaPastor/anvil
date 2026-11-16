"""Customers / engagement management router."""
from __future__ import annotations
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models.customer import Customer
from ..models.hash_list import Hash, HashList
from ..models.job import Job
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

    job_count_rows = (await db.execute(
        select(Job.customer_id, func.count(Job.id))
        .where(Job.customer_id.is_not(None))
        .group_by(Job.customer_id)
    )).all()
    job_counts = {cid: cnt for cid, cnt in job_count_rows}

    hash_count_rows = (await db.execute(
        select(Job.customer_id, func.count(Hash.id))
        .join(HashList, HashList.job_id == Job.id)
        .join(Hash, Hash.hash_list_id == HashList.id)
        .where(Job.customer_id.is_not(None))
        .group_by(Job.customer_id)
    )).all()
    hash_counts = {cid: cnt for cid, cnt in hash_count_rows}

    return templates.TemplateResponse(request, "customers/list.html", {
        "user": user, "customers": customers,
        "job_counts": job_counts, "hash_counts": hash_counts,
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


@router.get("/{customer_id}/hashes", response_class=HTMLResponse)
async def customer_hashes(
    customer_id: int,
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
):
    """List the cracked credentials recovered across all of a customer's jobs."""
    c = await db.get(Customer, customer_id)
    if not c:
        raise HTTPException(status_code=404)

    # Distinct hash-value counts for the summary line. Hashes are copied per job
    # (and per batch sibling), so everything here is deduplicated by hash_value.
    base = (
        select(Hash.hash_value)
        .join(HashList, Hash.hash_list_id == HashList.id)
        .join(Job, HashList.job_id == Job.id)
        .where(Job.customer_id == customer_id)
    )
    total_distinct = (await db.execute(
        select(func.count()).select_from(base.distinct().subquery())
    )).scalar_one()
    cracked_distinct = (await db.execute(
        select(func.count()).select_from(
            base.where(Hash.plaintext.is_not(None)).distinct().subquery()
        )
    )).scalar_one()

    # Deduplicated cracked credentials — one row per (hash_value, username),
    # most-recently-cracked first, capped for the page.
    display_limit = 1000
    rows = (await db.execute(
        select(
            Hash.username,
            Hash.hash_value,
            func.max(Hash.plaintext).label("plaintext"),
            func.max(Hash.cracked_at).label("cracked_at"),
        )
        .join(HashList, Hash.hash_list_id == HashList.id)
        .join(Job, HashList.job_id == Job.id)
        .where(Job.customer_id == customer_id, Hash.plaintext.is_not(None))
        .group_by(Hash.hash_value, Hash.username)
        .order_by(func.max(Hash.cracked_at).desc())
        .limit(display_limit)
    )).all()

    hashes = [
        {"username": r.username, "hash_value": r.hash_value,
         "plaintext": r.plaintext, "cracked_at": r.cracked_at}
        for r in rows
    ]
    has_username = any(h["username"] is not None for h in hashes)

    await audit_service.log_action(
        db, "customer_hashes_viewed", user_id=user.id,
        resource_type="customer", resource_id=customer_id,
        details={"cracked": cracked_distinct, "total": total_distinct},
        ip_address=request.client.host if request.client else None,
    )

    return templates.TemplateResponse(request, "customers/hashes.html", {
        "user": user, "customer": c,
        "hashes": hashes, "has_username": has_username,
        "total_distinct": total_distinct, "cracked_distinct": cracked_distinct,
        "display_limit": display_limit,
    })


@router.post("/{customer_id}/hashes/delete")
async def delete_customer_hashes(
    customer_id: int,
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
):
    c = await db.get(Customer, customer_id)
    if not c:
        raise HTTPException(status_code=404)

    job_ids = (await db.execute(
        select(Job.id).where(Job.customer_id == customer_id)
    )).scalars().all()
    if not job_ids:
        return RedirectResponse("/customers", status_code=302)

    target_lists = (await db.execute(
        select(HashList.id, HashList.file_path).where(HashList.job_id.in_(job_ids))
    )).all()
    target_ids = [hl_id for hl_id, _ in target_lists]
    target_paths = {fp for _, fp in target_lists if fp}

    hash_count = 0
    if target_ids:
        hash_count = (await db.execute(
            select(func.count(Hash.id)).where(Hash.hash_list_id.in_(target_ids))
        )).scalar_one()

        # Cascades to `hashes` via ON DELETE CASCADE
        await db.execute(delete(HashList).where(HashList.id.in_(target_ids)))

        # Only unlink files no other (surviving) hash_list still references
        if target_paths:
            still_used = set((await db.execute(
                select(HashList.file_path).where(HashList.file_path.in_(target_paths))
            )).scalars().all())
            for fp in target_paths - still_used:
                try:
                    Path(fp).unlink(missing_ok=True)
                except OSError:
                    pass

    await audit_service.log_action(
        db, "customer_hashes_deleted", user_id=user.id,
        resource_type="customer", resource_id=customer_id,
        details={"hash_lists": len(target_ids), "hashes": hash_count},
        ip_address=request.client.host if request.client else None,
    )
    return RedirectResponse("/customers", status_code=302)
