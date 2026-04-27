#!/usr/bin/env bash
# Anvil Agent — installation script
set -euo pipefail

INSTALL_DIR="/opt/anvil/agent"
DATA_DIR="/var/lib/anvil-agent"       # persistent data — survives reinstalls
SERVICE_USER="anvil-agent"
VENV_DIR="${INSTALL_DIR}/venv"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${CYAN}── $* ──${NC}"; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo bash setup.sh"

# ── Python version check ──────────────────────────────────────────────────────
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
[[ -z "$PYTHON_BIN" ]] && error "Python 3.11+ required. Install with: apt-get install python3.12"
info "Using $PYTHON_BIN ($VER_STR)"

# ── Hashcat check ─────────────────────────────────────────────────────────────
section "Checking hashcat"
if command -v hashcat &>/dev/null; then
    info "hashcat found: $(hashcat --version 2>/dev/null || echo 'unknown version')"
else
    warn "hashcat not found in PATH."
    warn "Install it before running jobs:"
    warn "  apt-get install hashcat          (may be outdated)"
    warn "  or download from https://hashcat.net/hashcat/"
fi

# ── System packages ───────────────────────────────────────────────────────────
section "Installing system packages"
apt-get update -q
PY_VER=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
apt-get install -y -q gcc build-essential python3-dev
apt-get install -y -q "python${PY_VER}-venv" 2>/dev/null || \
    apt-get install -y -q python3-venv 2>/dev/null || true

# ── OpenCL runtime ────────────────────────────────────────────────────────────
# pocl provides a CPU OpenCL device so hashcat always has something to run on.
# GPU drivers (NVIDIA/AMD) will add their own ICD loaders alongside this one.
section "Installing OpenCL runtime (pocl CPU fallback)"
apt-get install -y -q ocl-icd-libopencl1 pocl-opencl-icd && \
    info "pocl OpenCL CPU runtime installed." || \
    warn "pocl install failed — hashcat may not find any devices without a GPU driver."

# ── Service user ──────────────────────────────────────────────────────────────
section "Creating service user"
id "${SERVICE_USER}" &>/dev/null || \
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"

# Add to GPU access groups so hashcat can enumerate and use devices.
# video  — required for GPU access on most Linux distros
# render — required for DRM render nodes (/dev/dri/renderD*)
# nvidia — NVIDIA proprietary driver: /dev/nvidia* device access (OpenCL/CUDA)
for grp in video render nvidia; do
    if getent group "$grp" &>/dev/null; then
        usermod -aG "$grp" "${SERVICE_USER}" && info "Added ${SERVICE_USER} to group: $grp" || true
    fi
done

# Install OpenCL runtime if no ICD loaders are present.
# pocl runs entirely on CPU — ensures hashcat always has at least one device.
section "Checking OpenCL / compute runtime"
if ! ldconfig -p 2>/dev/null | grep -q libOpenCL; then
    info "No OpenCL runtime found — installing pocl (CPU fallback) ..."
    if command -v apt-get &>/dev/null; then
        apt-get install -y -q pocl-opencl-icd ocl-icd-libopencl1 2>/dev/null || \
            warn "pocl install failed — GPU jobs may not work until an OpenCL runtime is installed."
    elif command -v dnf &>/dev/null; then
        dnf install -y pocl 2>/dev/null || warn "pocl install failed."
    fi
else
    info "OpenCL runtime detected."
fi

# ── Directory layout ──────────────────────────────────────────────────────────
section "Setting up directories"
mkdir -p "${INSTALL_DIR}"/{certs,logs}
rsync -a --exclude='venv' "$(dirname "$0")/" "${INSTALL_DIR}/"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
chmod 750 "${INSTALL_DIR}"

# Persistent data directory — outside INSTALL_DIR so it survives reinstalls.
# Contains: workdir/cache/wordlists/  (pushed + job-cached wordlists)
#           workdir/cache/rules/      (cached rule files)
#           workdir/jobs/             (per-job temp files, cleaned after each job)
#           workdir/xdg/              (hashcat XDG runtime dirs)
# DO NOT wipe this directory during upgrades.
section "Setting up persistent data directory"
mkdir -p "${DATA_DIR}/workdir/cache/wordlists"
mkdir -p "${DATA_DIR}/workdir/cache/rules"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}"
chmod 750 "${DATA_DIR}"
info "Persistent data directory: ${DATA_DIR}"

# Migrate wordlists cached at the old location (inside INSTALL_DIR) if any exist
OLD_WL_CACHE="${INSTALL_DIR}/workdir/cache/wordlists"
if [[ -d "$OLD_WL_CACHE" ]] && [[ -n "$(ls -A "$OLD_WL_CACHE" 2>/dev/null)" ]]; then
    warn "Migrating cached wordlists from old location to ${DATA_DIR}/workdir/cache/wordlists/ ..."
    cp -n "${OLD_WL_CACHE}"/* "${DATA_DIR}/workdir/cache/wordlists/" 2>/dev/null || true
    chown "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}/workdir/cache/wordlists/"* 2>/dev/null || true
    info "Migration complete. Old cache at ${OLD_WL_CACHE} can be removed manually."
fi

# ── Auto-configure OpenCL for NVIDIA driver-only installs ──────────────────────
# When nvidia-smi is present but cuda toolkit is absent, hashcat needs
# --backend-prefer-opencl to skip broken CUDA init and use OpenCL instead.
# We also set opencl_device_types=2 (GPU only) so the GPU probe works as the
# service user (which may not have full CUDA device access).
if command -v nvidia-smi &>/dev/null; then
    if ! ldconfig -p 2>/dev/null | grep -qi libcuda.so.1 2>/dev/null; then
        info "NVIDIA GPU detected without CUDA SDK — enabling GPU OpenCL mode in config.toml"
        sed -i 's/^# opencl_device_types   = "2"/opencl_device_types   = "2"/' "${INSTALL_DIR}/config.toml"
    else
        info "NVIDIA GPU with CUDA SDK detected."
    fi
fi

# ── Python venv ───────────────────────────────────────────────────────────────
section "Creating Python virtual environment"
[[ -d "${VENV_DIR}" ]] && { warn "Existing venv found — removing"; rm -rf "${VENV_DIR}"; }

"${PYTHON_BIN}" -m venv "${VENV_DIR}"

section "Installing Python dependencies"
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
info "Installing packages (--prefer-binary)..."
"${VENV_DIR}/bin/pip" install \
    --prefer-binary \
    --no-cache-dir \
    -r "${INSTALL_DIR}/requirements.txt"
info "Dependencies installed"

# ── systemd service ───────────────────────────────────────────────────────────
section "Installing systemd service"
cat > /etc/systemd/system/anvil-agent.service << EOF
[Unit]
Description=Anvil Hash Cracking Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=ANVIL_AGENT_CONFIG=${INSTALL_DIR}/config.toml
ExecStart=${VENV_DIR}/bin/python -m anvil_agent.main
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes
# Persistent data lives in ${DATA_DIR} — this must NOT be a RuntimeDirectory
# (which would be cleaned on service stop) and must NOT use PrivateTmp=yes.
ReadWritePaths=${DATA_DIR}
# DevicePolicy=auto lets the service access all device nodes that the OS
# permits (NVIDIA devices are crw-rw-rw- so no extra allow-listing needed).
# DeviceAllow entries are intentionally omitted — they implicitly switch
# systemd into a closed/restrictive device policy that blocks GPU access.
DevicePolicy=auto

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable anvil-agent

echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Anvil agent installed                            ║${NC}"
echo -e "${GREEN}╠═══════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}  Config:   ${CYAN}${INSTALL_DIR}/config.toml${NC}"
echo -e "${GREEN}║${NC}  Wordlist cache (persistent):"
echo -e "${GREEN}║${NC}    ${CYAN}${DATA_DIR}/workdir/cache/wordlists/${NC}"
echo -e "${GREEN}║${NC}  Do NOT delete ${DATA_DIR} when upgrading."
echo -e "${GREEN}╚═══════════════════════════════════════════════════╝${NC}"
echo ""
echo "  Next steps:"
echo "  1. Edit ${INSTALL_DIR}/config.toml"
echo "     Set server_url and api_token (from the dashboard)"
echo ""
echo "  2. If using a self-signed server cert, copy it:"
echo "     scp root@<server>:/opt/anvil/server/certs/anvil.crt \\"
echo "         ${INSTALL_DIR}/certs/anvil.crt"
echo "     Then set ca_bundle = \"${INSTALL_DIR}/certs/anvil.crt\" in config.toml"
echo ""
echo "  3. Start the agent:"
echo "     sudo systemctl start anvil-agent"
echo "     sudo journalctl -u anvil-agent -f"
echo ""
