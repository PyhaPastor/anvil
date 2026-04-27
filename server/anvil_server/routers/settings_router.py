"""Server settings management — admin only."""
from __future__ import annotations
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models.user import User
from ..services.auth_service import require_admin
from ..services import audit_service
from ..config import settings, CONFIG_PATH
from .. import templates

router = APIRouter(prefix="/settings")


def _get_cert_sans() -> list[str]:
    """Read the current cert's SAN list for display."""
    from cryptography import x509
    from cryptography.x509 import DNSName, IPAddress as CertIP
    try:
        cert = x509.load_pem_x509_certificate(Path(settings.tls.cert_file).read_bytes())
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns = [n.value for n in san_ext.value if isinstance(n, DNSName)]
        ips = [str(n.value) for n in san_ext.value if isinstance(n, CertIP)]
        return dns + ips
    except Exception:
        return []


@router.get("", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from ..models.agent import Agent
    from sqlalchemy import select as _select
    key = settings.agent.provisioning_key
    masked = (key[:8] + "•" * 16) if key else None
    msg = request.query_params.get("msg")
    agents = (await db.execute(
        _select(Agent).where(Agent.is_active == True).order_by(Agent.id.asc())
    )).scalars().all()
    return templates.TemplateResponse(request, "settings/index.html", {
        "user": user,
        "prov_key_masked": masked,
        "prov_key_set": bool(key),
        "msg": msg,
        "extra_sans": settings.tls.extra_sans,
        "cert_sans": _get_cert_sans(),
        "tls_mode": settings.tls.mode,
        "install_allowlist": settings.agent.install_allowlist,
        "kiosk_allowlist": settings.server.kiosk_allowlist,
        "agents": agents,
        "default_agent_id": settings.agent.default_agent_id,
    })


@router.post("/kiosk-allowlist")
async def save_kiosk_allowlist(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    allowlist: str = Form(""),
):
    import re as _re
    import ipaddress as _ip

    new_list = [e.strip() for e in _re.split(r"[\n,]+", allowlist) if e.strip()]

    bad = []
    for entry in new_list:
        try:
            if "/" in entry:
                _ip.ip_network(entry, strict=False)
            else:
                _ip.ip_address(entry)
        except ValueError:
            bad.append(entry)
    if bad:
        return RedirectResponse(f"/settings?msg=kiosk_bad&bad={','.join(bad)}", status_code=302)

    # Mutate via model_copy to guarantee Pydantic v2 reflects the change
    settings.server = settings.server.model_copy(update={"kiosk_allowlist": new_list})

    config_path = Path(CONFIG_PATH)
    if config_path.exists():
        content = config_path.read_text()
        toml_list = "[" + ", ".join(f'"{e}"' for e in new_list) + "]"
        if _re.search(r"^kiosk_allowlist\s*=", content, _re.MULTILINE):
            content = _re.sub(
                r"^kiosk_allowlist\s*=.*$", f"kiosk_allowlist = {toml_list}",
                content, flags=_re.MULTILINE,
            )
        elif _re.search(r"^\[server\]", content, _re.MULTILINE):
            content = _re.sub(
                r"(\[server\])", rf"\1\nkiosk_allowlist = {toml_list}",
                content, flags=_re.MULTILINE,
            )
        else:
            content += f"\n[server]\nkiosk_allowlist = {toml_list}\n"
        config_path.write_text(content)

    await audit_service.log_action(
        db, "kiosk_allowlist_updated", user_id=user.id,
        details={"entries": new_list},
    )
    return RedirectResponse("/settings?msg=kiosk_saved", status_code=302)


@router.post("/install-allowlist")
async def save_install_allowlist(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    allowlist: str = Form(""),
):
    import re as _re

    new_list = [e.strip() for e in _re.split(r"[\n,]+", allowlist) if e.strip()]

    # Validate each entry is a valid IP or CIDR
    import ipaddress as _ip
    bad = []
    for entry in new_list:
        try:
            if "/" in entry:
                _ip.ip_network(entry, strict=False)
            else:
                _ip.ip_address(entry)
        except ValueError:
            bad.append(entry)
    if bad:
        return RedirectResponse(f"/settings?msg=allowlist_bad&bad={','.join(bad)}", status_code=302)

    settings.agent = settings.agent.model_copy(update={"install_allowlist": new_list})

    config_path = Path(CONFIG_PATH)
    if config_path.exists():
        content = config_path.read_text()
        toml_list = "[" + ", ".join(f'"{e}"' for e in new_list) + "]"
        if _re.search(r"^install_allowlist\s*=", content, _re.MULTILINE):
            content = _re.sub(
                r"^install_allowlist\s*=.*$", f"install_allowlist = {toml_list}",
                content, flags=_re.MULTILINE,
            )
        elif _re.search(r"^\[agent\]", content, _re.MULTILINE):
            content = _re.sub(
                r"(\[agent\])", rf"\1\ninstall_allowlist = {toml_list}",
                content, flags=_re.MULTILINE,
            )
        else:
            content += f"\n[agent]\ninstall_allowlist = {toml_list}\n"
        config_path.write_text(content)

    await audit_service.log_action(
        db, "install_allowlist_updated", user_id=user.id,
        details={"entries": new_list},
    )
    return RedirectResponse("/settings?msg=allowlist_saved", status_code=302)


@router.post("/default-agent")
async def save_default_agent(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    default_agent_id: int = Form(0),
):
    import re as _re

    settings.agent = settings.agent.model_copy(update={"default_agent_id": default_agent_id})

    config_path = Path(CONFIG_PATH)
    if config_path.exists():
        content = config_path.read_text()
        if _re.search(r"^default_agent_id\s*=", content, _re.MULTILINE):
            content = _re.sub(
                r"^default_agent_id\s*=.*$", f"default_agent_id = {default_agent_id}",
                content, flags=_re.MULTILINE,
            )
        elif _re.search(r"^\[agent\]", content, _re.MULTILINE):
            content = _re.sub(
                r"(\[agent\])", rf"\1\ndefault_agent_id = {default_agent_id}",
                content, flags=_re.MULTILINE,
            )
        else:
            content += f"\n[agent]\ndefault_agent_id = {default_agent_id}\n"
        config_path.write_text(content)

    await audit_service.log_action(
        db, "default_agent_updated", user_id=user.id,
        details={"default_agent_id": default_agent_id},
    )
    return RedirectResponse("/settings?msg=default_agent_saved", status_code=302)


@router.post("/regenerate-provisioning-key")
async def regenerate_provisioning_key(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    import re as _re

    new_key = secrets.token_urlsafe(48)
    settings.agent = settings.agent.model_copy(update={"provisioning_key": new_key})

    config_path = Path(CONFIG_PATH)
    if config_path.exists():
        content = config_path.read_text()
        if _re.search(r"^provisioning_key\s*=", content, _re.MULTILINE):
            content = _re.sub(
                r'^provisioning_key\s*=.*$',
                f'provisioning_key = "{new_key}"',
                content, flags=_re.MULTILINE,
            )
        elif _re.search(r"^\[agent\]", content, _re.MULTILINE):
            content = _re.sub(
                r"(\[agent\])", rf'\1\nprovisioning_key = "{new_key}"',
                content, flags=_re.MULTILINE,
            )
        else:
            content += f'\n[agent]\nprovisioning_key = "{new_key}"\n'
        config_path.write_text(content)

    await audit_service.log_action(db, "provisioning_key_rotated", user_id=user.id)
    return RedirectResponse("/settings?msg=prov_rotated", status_code=302)


@router.post("/regenerate-tls-cert")
async def regenerate_tls_cert(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    extra_sans: str = Form(""),
):
    """Delete the existing self-signed cert and regenerate with updated SANs."""
    import toml
    from ..services.tls_service import generate_self_signed_cert

    if settings.tls.mode != "self_signed":
        return RedirectResponse("/settings?msg=not_self_signed", status_code=302)

    # Parse the submitted SANs (one per line or comma-separated)
    import re
    new_sans = [s.strip() for s in re.split(r"[\n,]+", extra_sans) if s.strip()]

    # Update in-memory config
    settings.tls = settings.tls.model_copy(update={"extra_sans": new_sans})

    # Persist to config.toml — targeted replacement only, never rewrite the whole file
    # (rewriting via toml.dumps() strips comments and can alter string escaping)
    config_path = Path(CONFIG_PATH)
    if config_path.exists():
        toml_list = "[" + ", ".join(f'"{s}"' for s in new_sans) + "]"
        content = config_path.read_text()
        if re.search(r"^extra_sans\s*=", content, re.MULTILINE):
            content = re.sub(r"^extra_sans\s*=.*$", f"extra_sans = {toml_list}", content, flags=re.MULTILINE)
        elif re.search(r"^\[tls\]", content, re.MULTILINE):
            content = re.sub(r"(\[tls\])", rf"\1\nextra_sans = {toml_list}", content, flags=re.MULTILINE)
        else:
            content += f"\n[tls]\nextra_sans = {toml_list}\n"
        config_path.write_text(content)

    # Regenerate the cert (overwrites existing files)
    try:
        generate_self_signed_cert(settings.tls.cert_file, settings.tls.key_file, new_sans)
    except Exception as exc:
        import logging
        logging.getLogger("anvil.settings").exception("TLS cert regeneration failed: %s", exc)
        return RedirectResponse("/settings?msg=cert_error", status_code=302)

    await audit_service.log_action(
        db, "tls_cert_regenerated", user_id=user.id,
        details={"extra_sans": new_sans},
    )
    return RedirectResponse("/settings?msg=cert_regenerated", status_code=302)
