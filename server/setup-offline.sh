#!/usr/bin/env bash
# Anvil Server — offline installation script
# Target: Ubuntu 24.04 LTS (Noble), x86_64
# Requires: ./offline/ directory produced by package-offline.sh on a matching host.
# No network access is used. SCP the whole server/ dir over before running.
set -euo pipefail

INSTALL_DIR="/opt/anvil/server"
SERVICE_USER="anvil"
VENV_DIR="${INSTALL_DIR}/venv"
CONFIG_FILE="${INSTALL_DIR}/config.toml"
CERT_DIR="${INSTALL_DIR}/certs"

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
OFFLINE_DIR="${SRC_DIR}/offline"
DEBS_DIR="${OFFLINE_DIR}/debs"
WHEELS_DIR="${OFFLINE_DIR}/wheels"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${CYAN}── $* ──${NC}"; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo bash setup-offline.sh"

# ── Verify offline bundle is present ──────────────────────────────────────────
section "Checking offline bundle"
[[ -d "${OFFLINE_DIR}" ]] || error "offline/ directory not found at ${OFFLINE_DIR}. Run package-offline.sh on a connected host first."
[[ -d "${DEBS_DIR}"    ]] || error "offline/debs/ missing"
[[ -d "${WHEELS_DIR}"  ]] || error "offline/wheels/ missing"
DEB_COUNT=$(find "${DEBS_DIR}" -name '*.deb' | wc -l)
WHEEL_COUNT=$(find "${WHEELS_DIR}" \( -name '*.whl' -o -name '*.tar.gz' \) | wc -l)
[[ ${DEB_COUNT}   -gt 0 ]] || error "no .deb files in offline/debs/"
[[ ${WHEEL_COUNT} -gt 0 ]] || error "no wheel files in offline/wheels/"
info "Bundle OK: ${DEB_COUNT} debs, ${WHEEL_COUNT} wheels"
[[ -f "${OFFLINE_DIR}/MANIFEST.txt" ]] && cat "${OFFLINE_DIR}/MANIFEST.txt"

# ── Verify target OS ──────────────────────────────────────────────────────────
section "Verifying target OS"
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
    warn "Expected Ubuntu 24.04, found ${ID:-?} ${VERSION_ID:-?}"
    warn "Continuing anyway — bundle may be incompatible"
fi

# ── Install .deb packages from local bundle ───────────────────────────────────
section "Installing system packages from offline bundle"
# apt-get install with file paths handles dependency ordering and skips
# already-installed packages cleanly. No network is touched.
DEB_FILES=("${DEBS_DIR}"/*.deb)
apt-get install -y --no-download --allow-downgrades --allow-change-held-packages \
    -o Debug::NoLocking=true \
    "${DEB_FILES[@]}" 2>&1 | grep -vE '^(Reading|Building|Selecting)' || true
info "System packages installed (${#DEB_FILES[@]} debs processed)"

# ── Pick Python interpreter ───────────────────────────────────────────────────
section "Checking Python"
PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.13 python3; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(sys.version_info.minor + sys.version_info.major*100)")
        if [[ $ver -ge 311 ]]; then
            PYTHON_BIN=$(command -v "$candidate")
            VER_STR=$("$candidate" --version)
            break
        fi
    fi
done
[[ -z "$PYTHON_BIN" ]] && error "Python 3.11+ not available even after .deb install"
info "Using $PYTHON_BIN ($VER_STR)"

# ── Service user ──────────────────────────────────────────────────────────────
section "Creating service user"
id "${SERVICE_USER}" &>/dev/null || \
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
info "Service user '${SERVICE_USER}' ready"

# ── Directory layout ──────────────────────────────────────────────────────────
section "Setting up directories"
mkdir -p "${INSTALL_DIR}"/{certs,data/{wordlists,rules,hashlists,exports},logs}
rsync -a --exclude='venv' --exclude='*.db' --exclude='*.db-*' --exclude='config.toml' \
    --exclude='offline' \
    "${SRC_DIR}/" "${INSTALL_DIR}/"
if [[ ! -f "${CONFIG_FILE}" ]]; then
    if [[ -f "${SRC_DIR}/config.toml" ]]; then
        cp "${SRC_DIR}/config.toml" "${CONFIG_FILE}"
    elif [[ -f "${SRC_DIR}/config.toml.example" ]]; then
        cp "${SRC_DIR}/config.toml.example" "${CONFIG_FILE}"
    else
        error "No config.toml or config.toml.example found in source directory"
    fi
    info "config.toml installed"
else
    info "config.toml already exists — skipping (live config preserved)"
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
chmod 750 "${INSTALL_DIR}"
chmod 700 "${INSTALL_DIR}/certs"
chmod 700 "${INSTALL_DIR}/data"

# ── Package agent files for in-dashboard download ─────────────────────────────
section "Packaging agent distribution"
AGENT_SRC="${SRC_DIR}/../agent"
AGENT_DIST="${INSTALL_DIR}/agent-dist"
if [[ -d "${AGENT_SRC}" ]]; then
    rm -rf "${AGENT_DIST}"
    mkdir -p "${AGENT_DIST}"
    find "${AGENT_SRC}" \
        \( -name "venv" -o -name "__pycache__" -o -name "*.pyc" \
           -o -name "*.db" -o -name "*.db-shm" -o -name "*.db-wal" \
           -o -name ".git" \) -prune \
        -o -type f -print \
        | while IFS= read -r src_file; do
            rel="${src_file#${AGENT_SRC}/}"
            dest="${AGENT_DIST}/${rel}"
            mkdir -p "$(dirname "$dest")"
            cp -p "$src_file" "$dest"
          done
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${AGENT_DIST}"
    info "Agent package staged at ${AGENT_DIST}"
else
    warn "Agent source not found at ${AGENT_SRC} — skipping."
fi

# ── Python venv ───────────────────────────────────────────────────────────────
section "Creating Python virtual environment"
if [[ -d "${VENV_DIR}" ]]; then
    warn "Existing venv found — removing and recreating"
    rm -rf "${VENV_DIR}"
fi
"${PYTHON_BIN}" -m venv "${VENV_DIR}"
info "venv created at ${VENV_DIR}"

# ── Install Python deps from local wheels (no network) ────────────────────────
section "Installing Python dependencies from local wheels"
"${VENV_DIR}/bin/pip" install --quiet \
    --no-index --find-links="${WHEELS_DIR}" \
    --upgrade pip setuptools wheel
info "pip upgraded from local wheels"

"${VENV_DIR}/bin/pip" install \
    --no-index --find-links="${WHEELS_DIR}" \
    --no-cache-dir \
    -r "${INSTALL_DIR}/requirements.txt"
info "Dependencies installed from local wheels"

if command -v setcap &>/dev/null; then
    PY_BIN=$(readlink -f "${VENV_DIR}/bin/python3")
    setcap cap_net_bind_service=+ep "${PY_BIN}" 2>/dev/null && \
        info "cap_net_bind_service granted to ${PY_BIN}" || \
        warn "setcap failed — will rely on AmbientCapabilities in service file"
else
    warn "setcap not found — install libcap2-bin if port 443 binding fails"
fi

# ── TLS certificate ───────────────────────────────────────────────────────────
section "TLS certificate"
DETECTED_IP=$(hostname -I | awk '{print $1}')
DETECTED_HOST=$(hostname -f 2>/dev/null || hostname)

echo ""
echo -e "${CYAN}  The self-signed TLS certificate must include your server's hostname${NC}"
echo -e "${CYAN}  so that agents can verify it. Enter the hostname or IP address${NC}"
echo -e "${CYAN}  that agents will use to reach this server.${NC}"
echo ""
read -rp "  Server hostname/IP (e.g. anvil.example.com or ${DETECTED_IP}) [${DETECTED_HOST}]: " SERVER_HOSTNAME
SERVER_HOSTNAME="${SERVER_HOSTNAME:-${DETECTED_HOST}}"

SAN="IP:127.0.0.1,DNS:localhost,DNS:${DETECTED_HOST}"
if [[ "${SERVER_HOSTNAME}" != "${DETECTED_HOST}" ]]; then
    if [[ "${SERVER_HOSTNAME}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        SAN="${SAN},IP:${SERVER_HOSTNAME}"
    else
        SAN="${SAN},DNS:${SERVER_HOSTNAME}"
    fi
fi
if [[ -n "${DETECTED_IP}" && "${DETECTED_IP}" != "127.0.0.1" ]]; then
    SAN="${SAN},IP:${DETECTED_IP}"
fi

"${PYTHON_BIN}" - "${CONFIG_FILE}" "${SERVER_HOSTNAME}" << 'PYEOF'
import sys, re
cfg_path, hostname = sys.argv[1], sys.argv[2]
with open(cfg_path, 'r', encoding='utf-8') as f:
    text = f.read()
replacement = f'extra_sans = ["{hostname}"]'
if re.search(r'^extra_sans\s*=', text, re.MULTILINE):
    text = re.sub(r'^extra_sans\s*=.*$', replacement, text, flags=re.MULTILINE)
else:
    text = re.sub(r'(\[tls\])', r'\1\n' + replacement, text)
with open(cfg_path, 'w', encoding='utf-8') as f:
    f.write(text)
PYEOF

if [[ ! -f "${CERT_DIR}/anvil.key" ]]; then
    openssl req -x509 -newkey rsa:4096 \
        -keyout "${CERT_DIR}/anvil.key" \
        -out    "${CERT_DIR}/anvil.crt" \
        -days 3650 -nodes \
        -subj "/CN=${SERVER_HOSTNAME}/O=Anvil" \
        -addext "subjectAltName=${SAN}" \
        2>/dev/null
    chown "${SERVICE_USER}:${SERVICE_USER}" "${CERT_DIR}/anvil.key" "${CERT_DIR}/anvil.crt"
    chmod 400 "${CERT_DIR}/anvil.key"
    chmod 444 "${CERT_DIR}/anvil.crt"
    info "Self-signed certificate generated for ${SERVER_HOSTNAME} (10-year validity)"
    info "SANs: ${SAN}"
else
    warn "Certificate already exists — skipping generation"
fi

# ── Secret key ────────────────────────────────────────────────────────────────
section "Generating secret key"
if grep -qE "CHANGE_ME_BEFORE_FIRST_RUN|dev-only-key-change-before-production" "${CONFIG_FILE}" 2>/dev/null; then
    SECRET=$("${VENV_DIR}/bin/python" -c "import secrets; print(secrets.token_hex(64))")
    sed -i -E "s/secret_key = \"[^\"]+\"/secret_key = \"${SECRET}\"/" "${CONFIG_FILE}"
    info "Secret key written to config"
else
    warn "Secret key already set — skipping"
fi

# ── Agent provisioning key ────────────────────────────────────────────────────
section "Generating agent provisioning key"
if grep -qE '^provisioning_key = ".+"' "${CONFIG_FILE}" 2>/dev/null; then
    info "Agent provisioning key already set — skipping"
else
    PROV_KEY=$("${VENV_DIR}/bin/python" -c "import secrets; print(secrets.token_urlsafe(48))")
    if grep -qE '^provisioning_key' "${CONFIG_FILE}" 2>/dev/null; then
        sed -i "s|^provisioning_key = \".*\"|provisioning_key = \"${PROV_KEY}\"|" "${CONFIG_FILE}"
    else
        sed -i "/^\[agent\]/a provisioning_key = \"${PROV_KEY}\"" "${CONFIG_FILE}"
    fi
    info "Agent provisioning key written to config.toml"
fi

# ── Database init ─────────────────────────────────────────────────────────────
section "Initialising database"
cd "${INSTALL_DIR}"
sudo -u "${SERVICE_USER}" "${VENV_DIR}/bin/python" -c "
import asyncio
import sys
sys.path.insert(0, '${INSTALL_DIR}')
from anvil_server.database import init_db
asyncio.run(init_db())
print('  Database tables created and admin user seeded.')
"

# ── Seed built-in rules ───────────────────────────────────────────────────────
section "Seeding built-in rule files"
cd "${INSTALL_DIR}"
sudo -u "${SERVICE_USER}" "${VENV_DIR}/bin/python" - << 'PYEOF'
import asyncio, sys, os
from pathlib import Path
sys.path.insert(0, os.getcwd())
os.chdir(os.getcwd())

async def seed():
    from anvil_server.database import AsyncSessionLocal
    import anvil_server.models  # noqa: F401
    from anvil_server.models.wordlist import Rule
    from sqlalchemy import select

    rules = [
        ("Quick Wins",      "quick-wins.rule",    "High-yield first-pass rules covering ~60% of crackable AD hashes"),
        ("Best 64",         "best64.rule",         "Industry-standard 64-rule set (KoreLogic/hashcat). Best balance of speed vs coverage"),
        ("Common Leet",     "common-leet.rule",    "Leet speak substitutions (a→@, e→3, i→1, o→0, s→5) alone and combined"),
        ("Append Numbers",  "append-numbers.rule", "Append digits, years (1990–2026), and common symbols"),
        ("Case Toggle",     "case-toggle.rule",    "Case transformations: lower, upper, capitalise, toggle, title"),
        ("Corporate",       "corporate.rule",      "AD/corporate patterns: season+year, policy mutations, company name appends"),
    ]

    rules_dir = Path("data/rules").resolve()
    async with AsyncSessionLocal() as db:
        for name, filename, description in rules:
            file_path = str(rules_dir / filename)
            if not Path(file_path).exists():
                print(f"  [skip] {filename} — file not found")
                continue
            existing = (await db.execute(select(Rule).where(Rule.file_path == file_path))).scalar_one_or_none()
            if existing:
                print(f"  [skip] {name} — already registered")
            else:
                db.add(Rule(name=name, description=description, file_path=file_path))
                print(f"  [add]  {name}")
        await db.commit()

asyncio.run(seed())
PYEOF

# ── systemd service ───────────────────────────────────────────────────────────
section "Installing systemd service"
cat > /etc/systemd/system/anvil-server.service << EOF
[Unit]
Description=Anvil Hash Cracking Server
Documentation=https://github.com/your-org/anvil
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=ANVIL_CONFIG=${CONFIG_FILE}
ExecStart=${VENV_DIR}/bin/gunicorn anvil_server.main:app \\
    --worker-class uvicorn.workers.UvicornWorker \\
    --workers 2 \\
    --bind 0.0.0.0:443 \\
    --keyfile  ${CERT_DIR}/anvil.key \\
    --certfile ${CERT_DIR}/anvil.crt \\
    --access-logfile ${INSTALL_DIR}/logs/access.log \\
    --error-logfile  ${INSTALL_DIR}/logs/error.log \\
    --capture-output \\
    --timeout 120
Restart=on-failure
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable anvil-server
systemctl restart anvil-server

# ── Done ──────────────────────────────────────────────────────────────────────
SERVER_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Anvil server installed (offline)                 ║${NC}"
echo -e "${GREEN}╠═══════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}  URL:      ${CYAN}https://${SERVER_IP}${NC}"
echo -e "${GREEN}║${NC}  Login:    ${CYAN}admin${NC}"
echo -e "${GREEN}║${NC}  Password: ${CYAN}ChangeMe123!${NC}"
echo -e "${GREEN}║${NC}  Cert:     ${INSTALL_DIR}/certs/anvil.crt"
echo -e "${GREEN}╚═══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}  ► Change the admin password immediately after first login${NC}"
echo -e "${YELLOW}  ► Copy anvil.crt to each agent machine for TLS verification${NC}"
echo ""
