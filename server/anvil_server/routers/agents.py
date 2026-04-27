"""Agents management router."""
from __future__ import annotations
import io
import ipaddress
import tarfile
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import func as sqlfunc
from ..database import get_db
from ..models.agent import Agent, AgentHealth
from ..models.user import User
from ..services.auth_service import (
    require_admin,
    create_agent_token,
    create_bootstrap_token,
    verify_bootstrap_token,
    hash_agent_token,
)
from ..services import audit_service
from .. import templates

router = APIRouter(prefix="/agents")


def _check_install_allowlist(request: Request) -> None:
    """
    Raise 403 if the client IP is not in the configured install_allowlist.
    Empty allowlist = no restriction (open to all).
    Respects X-Forwarded-For when the server is behind a trusted proxy.
    """
    from ..config import settings
    allowlist = settings.agent.install_allowlist
    if not allowlist:
        return

    # Prefer X-Forwarded-For (set by reverse proxies), fall back to direct client IP
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        client_ip_str = forwarded_for.split(",")[0].strip()
    else:
        client_ip_str = request.client.host if request.client else "0.0.0.0"

    try:
        client_ip = ipaddress.ip_address(client_ip_str)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied.")

    for entry in allowlist:
        try:
            if "/" in entry:
                if client_ip in ipaddress.ip_network(entry, strict=False):
                    return
            else:
                if client_ip == ipaddress.ip_address(entry):
                    return
        except ValueError:
            continue

    raise HTTPException(
        status_code=403,
        detail=f"Install access denied for {client_ip_str}. Add your IP to the allowlist in Settings.",
    )


# ---------------------------------------------------------------------------
# Admin UI — list and register
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def list_agents(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    agents = (await db.execute(select(Agent).order_by(Agent.name))).scalars().all()

    # Latest health snapshot per agent (single query using max-id subquery)
    health_by_agent: dict = {}
    if agents:
        latest_subq = (
            select(AgentHealth.agent_id, sqlfunc.max(AgentHealth.id).label("max_id"))
            .where(AgentHealth.agent_id.in_([a.id for a in agents]))
            .group_by(AgentHealth.agent_id)
            .subquery()
        )
        health_rows = (await db.execute(
            select(AgentHealth).join(latest_subq, AgentHealth.id == latest_subq.c.max_id)
        )).scalars().all()
        health_by_agent = {h.agent_id: h for h in health_rows}

    return templates.TemplateResponse(request, "agents/list.html", {
        "user": user, "agents": agents, "health_by_agent": health_by_agent,
    })


@router.get("/register", response_class=HTMLResponse)
async def register_form(request: Request, user: User = Depends(require_admin)):
    return templates.TemplateResponse(request, "agents/register.html", {
        "user": user, "registered": False,
    })


@router.post("/register")
async def register_agent(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
):
    agent = Agent(name=name, api_token_hash="pending")
    db.add(agent)
    await db.flush()

    raw_api_token = create_agent_token(agent.id)
    agent.api_token_hash = hash_agent_token(raw_api_token)
    raw_bootstrap_token = create_bootstrap_token(agent.id)

    await audit_service.log_action(db, "agent_registered", user_id=user.id,
                                    resource_type="agent", resource_id=agent.id,
                                    details={"name": name})

    server_url = str(request.base_url).rstrip("/")
    bootstrap_cmd = f"curl -sfL {server_url}/agents/bootstrap/{raw_bootstrap_token} | sudo bash"

    return templates.TemplateResponse(request, "agents/register.html", {
        "user": user,
        "registered": True,
        "agent_name": name,
        "bootstrap_cmd": bootstrap_cmd,
        # Kept for the manual fallback section
        "raw_api_token": raw_api_token,
        "server_url": server_url,
    })


# ---------------------------------------------------------------------------
# Public — bootstrap script delivery
# ---------------------------------------------------------------------------

@router.get("/bootstrap/{raw_token}", response_class=PlainTextResponse)
async def bootstrap_agent(
    raw_token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Public endpoint — no auth required.
    Verifies the bootstrap JWT, rotates the agent's API token, and returns a
    shell script that writes config.toml, downloads the TLS cert, and starts
    the anvil-agent service.

    Calling this endpoint multiple times is safe — it simply issues a new
    API token each time (rotating the previous one).
    """
    agent_id = verify_bootstrap_token(raw_token)

    agent = await db.get(Agent, agent_id)
    if agent is None or not agent.is_active:
        raise HTTPException(status_code=404, detail="Agent not found or inactive.")

    # Rotate the API token so this script can embed a fresh one
    new_api_token = create_agent_token(agent.id)
    agent.api_token_hash = hash_agent_token(new_api_token)
    await db.commit()

    server_url = str(request.base_url).rstrip("/")
    agent_name = agent.name

    # Build the shell script with all values already substituted.
    # The single-quoted heredoc END_TOML prevents the shell from expanding
    # anything inside — safe because the token may contain special characters.
    script = f"""#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Anvil Agent Bootstrap — auto-generated, expires 1 hour after registration
#  Agent : {agent_name}
#  Server: {server_url}
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="/opt/anvil/agent"
CERTS_DIR="/opt/anvil/agent/certs"
SERVICE="anvil-agent"
SERVER="{server_url}"

echo ""
echo "  Anvil Agent Bootstrap"
echo "  Agent : {agent_name}"
echo "  Server: {server_url}"
echo ""

# ── Preflight ─────────────────────────────────────────────────────────────────
if [[ ! -f "$INSTALL_DIR/venv/bin/python" ]]; then
    echo "ERROR: Agent not installed at $INSTALL_DIR"
    echo ""
    echo "  Run the zero-touch install command from the Agents page first."
    echo ""
    exit 1
fi

# ── 1. Ensure hashcat is installed ────────────────────────────────────────────
echo "[1/5] Checking hashcat ..."
HASHCAT_BIN=$(command -v hashcat 2>/dev/null || true)
if [[ -z "$HASHCAT_BIN" ]]; then
    echo "      hashcat not found — attempting install ..."
    if command -v apt-get &>/dev/null; then
        apt-get install -y -q hashcat 2>&1 | tail -3
    elif command -v dnf &>/dev/null; then
        dnf install -y hashcat
    elif command -v yum &>/dev/null; then
        yum install -y hashcat
    else
        echo "WARN: No supported package manager found. Install hashcat manually:"
        echo "      https://hashcat.net/hashcat/"
    fi
    HASHCAT_BIN=$(command -v hashcat 2>/dev/null || true)
fi
if [[ -n "$HASHCAT_BIN" ]]; then
    echo "      hashcat : $HASHCAT_BIN ($($HASHCAT_BIN --version 2>/dev/null || echo 'version unknown'))"
else
    echo "WARN: hashcat still not found. Jobs will fail until it is installed manually."
    HASHCAT_BIN="/usr/bin/hashcat"
fi

# ── 2. Write config.toml ─────────────────────────────────────────────────────
echo "[2/5] Writing config.toml ..."
cat > "$INSTALL_DIR/config.toml" << 'END_TOML'
[agent]
name        = "{agent_name}"
server_url  = "{server_url}"
api_token   = "{new_api_token}"
poll_interval = 5
verify_tls  = true
ca_bundle   = "/opt/anvil/agent/certs/anvil.crt"

[hashcat]
binary       = "/usr/bin/hashcat"
extra_flags  = ""
workdir      = "/tmp/anvil-hashcat"
potfile      = ""
cpu_fallback = true

[hardware]
sample_interval = 2
gpu_backend     = "gputil"

[logging]
level = "INFO"
file  = "/opt/anvil/agent/anvil-agent.log"
END_TOML
# Patch hashcat binary path to the detected location
sed -i "s|^binary      = \\".*\\"|binary      = \\"$HASHCAT_BIN\\"|" "$INSTALL_DIR/config.toml"

# ── 3. Download TLS certificate ──────────────────────────────────────────────────
echo "[3/5] Downloading TLS certificate ..."
mkdir -p "$CERTS_DIR"
# -k (skip verify) is intentional for this one bootstrap request only —
# the downloaded cert is used for all future connections.
if curl -sfk "$SERVER/agents/cert" -o "$CERTS_DIR/anvil.crt"; then
    echo "      Certificate saved to $CERTS_DIR/anvil.crt"
else
    echo "WARN: Could not download TLS cert."
    echo "      Falling back to verify_tls = false (less secure)."
    sed -i 's/^verify_tls  = true/verify_tls  = false/' "$INSTALL_DIR/config.toml"
    sed -i '/^ca_bundle/d' "$INSTALL_DIR/config.toml"
fi

# ── 4. Fix ownership ─────────────────────────────────────────────────────────────
echo "[4/5] Setting file ownership ..."
chown -R "$SERVICE:$SERVICE" "$INSTALL_DIR" 2>/dev/null || true

# ── 5. Start service ─────────────────────────────────────────────────────────────
echo "[5/5] Starting $SERVICE ..."
systemctl daemon-reload
systemctl enable --now "$SERVICE" 2>/dev/null || systemctl restart "$SERVICE"

echo ""
echo "  Done. Agent '{agent_name}' is configured and starting."
echo "  Status : sudo systemctl status $SERVICE"
echo "  Logs   : sudo journalctl -u $SERVICE -f"
echo ""
"""
    return PlainTextResponse(content=script, media_type="text/x-shellscript")


@router.get("/download")
async def download_agent_package(request: Request):
    """
    Public endpoint — streams the agent source as a .tar.gz archive.
    Populated by setup.sh into agent-dist/; returns 503 if not available.
    The archive unpacks as anvil-agent/ so setup.sh is at anvil-agent/setup.sh.
    """
    _check_install_allowlist(request)
    from ..config import settings
    dist_dir = Path(settings.storage.agent_dist_dir)
    if not dist_dir.exists() or not any(dist_dir.iterdir()):
        raise HTTPException(
            status_code=503,
            detail="Agent package not staged on this server. Re-run server/setup.sh from the project root.",
        )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(dist_dir), arcname="anvil-agent")
    return Response(
        content=buf.getvalue(),
        media_type="application/gzip",
        headers={"Content-Disposition": "attachment; filename=anvil-agent.tar.gz"},
    )


@router.get("/install", response_class=PlainTextResponse)
async def agent_install_script(request: Request):
    """
    Public endpoint — returns a fully self-contained shell script that:
      1. Downloads and installs the agent package from this server
      2. Calls /api/v1/agent/provision (using the embedded provisioning key)
         to create the agent record and obtain an API token — no dashboard needed
      3. Writes config.toml with the token, downloads the TLS cert, starts the service

    Run on the agent machine with:
      curl -sfk <server>/agents/install | sudo bash
    """
    _check_install_allowlist(request)
    from ..config import settings
    server_url = str(request.base_url).rstrip("/")
    prov_key = settings.agent.provisioning_key
    dist_dir = Path(settings.storage.agent_dist_dir)
    pkg_available = dist_dir.exists() and any(dist_dir.iterdir())

    # If the package isn't staged yet, the download step will fail with a clear
    # server-side 503; we still emit the script so the rest of the flow is visible.
    pkg_unavailable_warn = (
        "" if pkg_available else
        'echo "WARN: Agent package not yet available on the server (re-run server/setup.sh)."\n'
    )
    prov_unavailable_guard = (
        "" if prov_key else
        'echo "ERROR: Provisioning key not set on server. Re-run server/setup.sh."\nexit 1\n'
    )

    script = f"""\
#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Anvil Agent — Install, Configure & Start
#  Server : {server_url}
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

[[ $EUID -ne 0 ]] && echo "ERROR: Run as root  (sudo bash)" && exit 1
{prov_unavailable_guard}
SERVER="{server_url}"
PROV_KEY="{prov_key}"
INSTALL_DIR="/opt/anvil/agent"
SERVICE="anvil-agent"

echo ""
echo "  Anvil Agent — Install, Configure & Start"
echo "  Server : $SERVER"
echo ""

# ── 1. Install ────────────────────────────────────────────────────────────────
echo "[1/6] Downloading and installing agent ..."
{pkg_unavailable_warn}TMPDIR=$(mktemp -d)
curl -sfk "$SERVER/agents/download" -o "$TMPDIR/anvil-agent.tar.gz"
tar -xf "$TMPDIR/anvil-agent.tar.gz" -C "$TMPDIR"
echo "      Running installer (this may take a minute) ..."
bash "$TMPDIR/anvil-agent/setup.sh"
rm -rf "$TMPDIR"

# ── 2. Ensure hashcat is installed ────────────────────────────────────────────
echo "[2/6] Checking hashcat ..."
HASHCAT_BIN=$(command -v hashcat 2>/dev/null || true)
if [[ -z "$HASHCAT_BIN" ]]; then
    echo "      hashcat not found — attempting install ..."
    if command -v apt-get &>/dev/null; then
        apt-get install -y -q hashcat 2>&1 | tail -3
    elif command -v dnf &>/dev/null; then
        dnf install -y hashcat
    elif command -v yum &>/dev/null; then
        yum install -y hashcat
    else
        echo "WARN: No supported package manager found. Install hashcat manually:"
        echo "      https://hashcat.net/hashcat/"
    fi
    HASHCAT_BIN=$(command -v hashcat 2>/dev/null || true)
fi
if [[ -n "$HASHCAT_BIN" ]]; then
    echo "      hashcat : $HASHCAT_BIN ($($HASHCAT_BIN --version 2>/dev/null || echo 'version unknown'))"
else
    echo "WARN: hashcat still not found after install attempt."
    echo "      Jobs will fail until hashcat is installed manually."
    HASHCAT_BIN="/usr/bin/hashcat"
fi

# ── 3. Provision API token ────────────────────────────────────────────────────
echo "[3/6] Provisioning API token ..."
AGENT_NAME=$(hostname -s 2>/dev/null || hostname)
AGENT_HOST=$(hostname -f 2>/dev/null || hostname)

RAW=$(curl -sfk "$SERVER/api/v1/agent/provision" \\
    -H 'Content-Type: application/json' \\
    -d "{{\\"provisioning_key\\":\\"$PROV_KEY\\",\\"name\\":\\"$AGENT_NAME\\",\\"hostname\\":\\"$AGENT_HOST\\"}}" \\
    2>/dev/null) || RAW=""

API_TOKEN=$(echo "$RAW" | python3 -c \\
    "import sys,json; print(json.load(sys.stdin)['api_token'])" 2>/dev/null || echo "")

if [[ -z "$API_TOKEN" ]]; then
    echo ""
    echo "ERROR: Provisioning failed."
    [[ -n "$RAW" ]] && echo "  Server response: $RAW"
    echo ""
    echo "  Possible causes:"
    echo "    - Server unreachable at $SERVER"
    echo "    - Provisioning key mismatch (server was re-installed?)"
    echo "    - See server logs: sudo journalctl -u anvil-server -n 50"
    echo ""
    echo "  Manual alternative: register from the dashboard at $SERVER/agents/register"
    exit 1
fi

echo "      Token issued for: $AGENT_NAME ($AGENT_HOST)"

# ── 4. Write config.toml ──────────────────────────────────────────────────────
echo "[4/6] Writing config.toml ..."
cat > "$INSTALL_DIR/config.toml" << ENDTOML
[agent]
name        = "$AGENT_NAME"
server_url  = "$SERVER"
api_token   = "$API_TOKEN"
poll_interval = 5
verify_tls  = true
ca_bundle   = "/opt/anvil/agent/certs/anvil.crt"

[hashcat]
binary               = "$HASHCAT_BIN"
extra_flags          = ""
workdir              = "/tmp/anvil-hashcat"
potfile              = ""
cpu_fallback         = true
# Uncomment on GPU machines without CUDA SDK (driver-only / OpenCL install):
# opencl_device_types   = "2"

[hardware]
sample_interval = 2
gpu_backend     = "gputil"

[logging]
level = "INFO"
file  = "/opt/anvil/agent/anvil-agent.log"
ENDTOML

# Patch hashcat binary path to the detected location
sed -i "s|^binary               = .*|binary               = \\"$HASHCAT_BIN\\"|" "$INSTALL_DIR/config.toml"

# Auto-enable GPU OpenCL mode when nvidia-smi is present (driver installed).
# This passes --opencl-device-types 2 to hashcat, bypassing CUDA SDK requirement
# and skipping the GPU probe (which fails for service users without a home dir).
if command -v nvidia-smi &>/dev/null; then
    sed -i 's/^# opencl_device_types   = "2"/opencl_device_types   = "2"/' "$INSTALL_DIR/config.toml"
    echo "      NVIDIA GPU detected — enabled opencl_device_types = \\"2\\" in config.toml"
fi

# ── 5. Download TLS certificate ───────────────────────────────────────────────
echo "[5/6] Downloading TLS certificate (trust-on-first-use) ..."
mkdir -p "$INSTALL_DIR/certs"
if curl -sfk "$SERVER/agents/cert" -o "$INSTALL_DIR/certs/anvil.crt"; then
    echo "      Certificate saved."
else
    echo "WARN: TLS cert download failed — falling back to verify_tls = false."
    sed -i 's/^verify_tls  = true/verify_tls  = false/' "$INSTALL_DIR/config.toml"
    sed -i '/^ca_bundle/d' "$INSTALL_DIR/config.toml"
fi

# ── 6. Fix ownership and start service ────────────────────────────────────────
echo "[6/6] Starting $SERVICE ..."
chown -R "$SERVICE:$SERVICE" "$INSTALL_DIR" 2>/dev/null || true
systemctl daemon-reload
systemctl enable "$SERVICE" 2>/dev/null || true
# Always restart so the freshly-written config.toml is loaded
systemctl restart "$SERVICE"

STATUS=$(systemctl is-active "$SERVICE" 2>/dev/null || echo "unknown")
echo ""
echo "  ✓ Agent '$AGENT_NAME' installed and $STATUS."
echo "  Logs : sudo journalctl -u $SERVICE -f"
echo ""
"""
    return PlainTextResponse(content=script, media_type="text/x-shellscript")


@router.get("/cert", response_class=PlainTextResponse)
async def download_server_cert():
    """
    Public endpoint — returns the server's TLS certificate in PEM format.
    The certificate is NOT secret (any TLS client sees it during the handshake).
    Used by the bootstrap script so agents can verify the server cert after setup.
    """
    from ..config import settings
    cert_path = Path(settings.tls.cert_file)
    if not cert_path.exists():
        raise HTTPException(status_code=404, detail="TLS certificate not found on server.")
    return PlainTextResponse(content=cert_path.read_text(), media_type="application/x-pem-file")


# ---------------------------------------------------------------------------
# Admin — deactivate
# ---------------------------------------------------------------------------

@router.post("/{agent_id}/deactivate")
async def deactivate_agent(
    agent_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404)
    agent.is_active = False
    await audit_service.log_action(db, "agent_deactivated", user_id=user.id,
                                    resource_type="agent", resource_id=agent_id)
    return RedirectResponse("/agents", status_code=302)


@router.get("/{agent_id}/wordlist-cache")
async def get_wordlist_cache(
    agent_id: int,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return the cached wordlist inventory for an agent."""
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404)
    return JSONResponse({"cache": agent.wordlist_cache or []})


@router.post("/{agent_id}/wordlist-cache/delete")
async def delete_wordlist_from_cache(
    agent_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Send a delete command to the agent via WebSocket and clear the server-side inventory entry."""
    from ..services.ws_manager import ws_manager
    body = await request.json()
    filename = body.get("name")
    if not filename:
        raise HTTPException(status_code=400, detail="name is required")

    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404)

    # Send delete command to agent over WebSocket
    sent = await ws_manager.send_to_agent(agent_id, {
        "type": "delete_cached_wordlist",
        "name": filename,
    })

    # Optimistically remove from inventory regardless of WS delivery
    # (agent will re-report on next heartbeat if it failed)
    if agent.wordlist_cache:
        agent.wordlist_cache = [e for e in agent.wordlist_cache if e.get("name") != filename]

    await audit_service.log_action(
        db, "wordlist_cache_deleted", user_id=user.id,
        resource_type="agent", resource_id=agent_id,
        details={"filename": filename, "ws_delivered": sent},
    )

    return JSONResponse({"status": "ok", "ws_delivered": sent})


@router.post("/{agent_id}/delete")
async def delete_agent(
    agent_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404)
    if agent.is_active:
        raise HTTPException(status_code=400, detail="Deactivate the agent before deleting it.")
    name = agent.name
    await db.delete(agent)
    await audit_service.log_action(db, "agent_deleted", user_id=user.id,
                                    resource_type="agent", resource_id=agent_id,
                                    details={"name": name})
    return RedirectResponse("/agents", status_code=302)
