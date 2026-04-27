"""Wordlists and rules management router."""
from __future__ import annotations
import asyncio
import ipaddress
import re
import shutil
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models.wordlist import Wordlist, Rule
from ..models.agent import Agent
from ..models.user import User, UserRole
from ..services.auth_service import require_analyst
from ..services import audit_service
from ..services.upload_service import save_upload
from ..services.ws_manager import ws_manager
from ..config import settings
from .. import templates

_ADMIN_MAX_UPLOAD_BYTES = 100 * 1024 ** 3    # 100 GB (effectively unlimited)
_NONADMIN_MAX_UPLOAD_BYTES = 30 * 1024 ** 3  # 30 GB

router = APIRouter(prefix="/wordlists")

_WORDLIST_EXTS = [".txt", ".lst", ".dict", ".wl"]
_RULE_EXTS = [".rule", ".rules", ".txt"]


def _count_lines_binary(path: str) -> int:
    """Count newlines by reading raw bytes — no encoding issues, fast."""
    count = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(131_072), b""):
            count += chunk.count(b"\n")
    return count


@router.get("", response_class=HTMLResponse)
async def list_wordlists(
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
):
    wordlists = (await db.execute(select(Wordlist).order_by(Wordlist.name))).scalars().all()
    rules = (await db.execute(select(Rule).order_by(Rule.name))).scalars().all()
    agents = (await db.execute(
        select(Agent).where(Agent.is_active == True).order_by(Agent.name)
    )).scalars().all()
    agents_info = [
        {
            "id": a.id,
            "name": a.name,
            "is_online": a.is_online,
            "wordlist_cache": a.wordlist_cache or [],
        }
        for a in agents
    ]
    try:
        du = shutil.disk_usage(settings.storage.wordlists_dir)
        disk_free_gb = du.free / 1024 ** 3
        disk_total_gb = du.total / 1024 ** 3
    except Exception:
        disk_free_gb = None
        disk_total_gb = None
    return templates.TemplateResponse(request, "wordlists/list.html", {
        "user": user,
        "wordlists": wordlists, "rules": rules,
        "agents_info": agents_info,
        "disk_free_gb": disk_free_gb,
        "disk_total_gb": disk_total_gb,
    })


def _is_xhr(request: Request) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


@router.post("/upload-wordlist")
async def upload_wordlist(
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    description: str = Form(""),
    category: str = Form(""),
    file: UploadFile = File(...),
):
    max_bytes = (
        _ADMIN_MAX_UPLOAD_BYTES if user.role == UserRole.ADMIN else _NONADMIN_MAX_UPLOAD_BYTES
    )
    try:
        saved_path, size = await save_upload(
            file, settings.storage.wordlists_dir, max_bytes, _WORDLIST_EXTS,
        )
    except HTTPException as exc:
        if _is_xhr(request):
            return JSONResponse({"ok": False, "detail": exc.detail}, status_code=exc.status_code)
        raise

    # Count lines in a thread to avoid blocking the event loop; binary mode handles any encoding
    line_count = await asyncio.to_thread(_count_lines_binary, saved_path)

    wl = Wordlist(
        name=name, description=description or None,
        file_path=saved_path, file_size_bytes=size,
        line_count=line_count, category=category or None,
        uploaded_by_id=user.id,
    )
    db.add(wl)
    await db.flush()
    await audit_service.log_action(db, "wordlist_uploaded", user_id=user.id,
                                    resource_type="wordlist", resource_id=wl.id)

    if _is_xhr(request):
        return JSONResponse({
            "ok": True,
            "name": name,
            "line_count": line_count,
            "size_bytes": size,
        })
    return RedirectResponse("/wordlists", status_code=302)


@router.post("/upload-rule")
async def upload_rule(
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    description: str = Form(""),
    file: UploadFile = File(...),
):
    max_bytes = (
        _ADMIN_MAX_UPLOAD_BYTES if user.role == UserRole.ADMIN else _NONADMIN_MAX_UPLOAD_BYTES
    )
    try:
        saved_path, size = await save_upload(
            file, settings.storage.rules_dir, max_bytes, _RULE_EXTS,
        )
    except HTTPException as exc:
        if _is_xhr(request):
            return JSONResponse({"ok": False, "detail": exc.detail}, status_code=exc.status_code)
        raise

    rule = Rule(
        name=name, description=description or None,
        file_path=saved_path, uploaded_by_id=user.id,
    )
    db.add(rule)
    await db.flush()
    await audit_service.log_action(db, "rule_uploaded", user_id=user.id,
                                    resource_type="rule", resource_id=rule.id)

    if _is_xhr(request):
        return JSONResponse({
            "ok": True,
            "name": name,
            "size_bytes": size,
        })
    return RedirectResponse("/wordlists", status_code=302)


@router.post("/fetch-url")
async def fetch_wordlist_from_url(
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    url: str = Form(...),
    description: str = Form(""),
    category: str = Form(""),
):
    """Download a wordlist from a remote URL and save it to the library."""
    # Validate URL scheme
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        detail = "Only http:// and https:// URLs are supported."
        if _is_xhr(request):
            return JSONResponse({"ok": False, "detail": detail}, status_code=400)
        raise HTTPException(400, detail)

    # SSRF protection — resolve hostname and block private/loopback addresses
    hostname = parsed.hostname or ""
    if not hostname:
        raise HTTPException(400, "Invalid URL: missing hostname.")
    try:
        addr_infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
        for info in addr_infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                detail = "URL resolves to a private/internal address — not allowed."
                if _is_xhr(request):
                    return JSONResponse({"ok": False, "detail": detail}, status_code=400)
                raise HTTPException(400, detail)
    except HTTPException:
        raise
    except Exception:
        detail = "Could not resolve URL hostname."
        if _is_xhr(request):
            return JSONResponse({"ok": False, "detail": detail}, status_code=400)
        raise HTTPException(400, detail)

    max_bytes = (
        _ADMIN_MAX_UPLOAD_BYTES if user.role == UserRole.ADMIN else _NONADMIN_MAX_UPLOAD_BYTES
    )

    # Derive a safe filename from the URL path
    raw_filename = Path(parsed.path).name or "wordlist"
    safe_filename = re.sub(r"[^a-zA-Z0-9._\-]", "_", raw_filename)[:128] or "wordlist"
    if not any(safe_filename.lower().endswith(ext) for ext in _WORDLIST_EXTS):
        safe_filename += ".txt"

    dest_dir = Path(settings.storage.wordlists_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / safe_filename
    # Avoid clobbering existing files
    stem, suffix = dest_path.stem, dest_path.suffix
    counter = 1
    while dest_path.exists():
        dest_path = dest_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    downloaded = 0
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(10.0, read=300.0)) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    detail = f"Remote server returned HTTP {response.status_code}."
                    if _is_xhr(request):
                        return JSONResponse({"ok": False, "detail": detail}, status_code=400)
                    raise HTTPException(400, detail)
                with open(dest_path, "wb") as fout:
                    async for chunk in response.aiter_bytes(65536):
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            fout.close()
                            dest_path.unlink(missing_ok=True)
                            detail = f"File exceeds the {max_bytes // (1024**3)} GB size limit."
                            if _is_xhr(request):
                                return JSONResponse({"ok": False, "detail": detail}, status_code=413)
                            raise HTTPException(413, detail)
                        fout.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        dest_path.unlink(missing_ok=True)
        detail = f"Download failed: {exc}"
        if _is_xhr(request):
            return JSONResponse({"ok": False, "detail": detail}, status_code=400)
        raise HTTPException(400, detail)

    line_count = await asyncio.to_thread(_count_lines_binary, str(dest_path))

    wl = Wordlist(
        name=name, description=description or None,
        file_path=str(dest_path), file_size_bytes=downloaded,
        line_count=line_count, category=category or None,
        uploaded_by_id=user.id,
    )
    db.add(wl)
    await db.flush()
    await audit_service.log_action(db, "wordlist_uploaded", user_id=user.id,
                                    resource_type="wordlist", resource_id=wl.id,
                                    details={"name": name, "source_url": url})

    if _is_xhr(request):
        return JSONResponse({
            "ok": True,
            "name": name,
            "line_count": line_count,
            "size_bytes": downloaded,
        })
    return RedirectResponse("/wordlists", status_code=302)


@router.post("/{wordlist_id}/push-to-agent")
async def push_wordlist_to_agent(
    wordlist_id: int,
    agent_id: int = Form(...),
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
):
    """Send a preload command to an agent over WebSocket so it downloads the wordlist to its cache."""
    wl = await db.get(Wordlist, wordlist_id)
    if not wl:
        raise HTTPException(status_code=404, detail="Wordlist not found")
    agent = await db.get(Agent, agent_id)
    if not agent or not agent.is_active:
        raise HTTPException(status_code=404, detail="Agent not found")

    delivered = await ws_manager.send_to_agent(agent_id, {
        "type": "preload_wordlist",
        "wordlist_id": wordlist_id,
        "name": Path(wl.file_path).name,
        "size": wl.file_size_bytes,
    })
    return JSONResponse({"ok": True, "ws_delivered": delivered})


@router.get("/{wordlist_id}/preload-status")
async def get_preload_status(
    wordlist_id: int,
    agent_id: int,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
):
    """Poll the current preload progress for a given agent + wordlist pair."""
    progress = ws_manager.get_preload_progress(agent_id, wordlist_id)
    if progress is None:
        return JSONResponse({"status": "idle"})
    return JSONResponse(progress)


@router.post("/{wordlist_id}/delete")
async def delete_wordlist(
    wordlist_id: int,
    request: Request,
    user: User = Depends(require_analyst),
    db: AsyncSession = Depends(get_db),
):
    wl = await db.get(Wordlist, wordlist_id)
    if not wl:
        raise HTTPException(status_code=404)
    Path(wl.file_path).unlink(missing_ok=True)
    await db.delete(wl)
    await audit_service.log_action(db, "wordlist_deleted", user_id=user.id,
                                    resource_type="wordlist", resource_id=wordlist_id)
    return RedirectResponse("/wordlists", status_code=302)
