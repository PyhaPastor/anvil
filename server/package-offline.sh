#!/usr/bin/env bash
# Anvil Server — offline bundle builder
# Run this on an internet-connected machine that matches the target server:
#   Ubuntu 24.04 LTS (Noble), x86_64
# Produces: ./offline/  (apt debs + python wheels) — SCP the whole server/
# directory to the airgapped server, then run setup-offline.sh as root.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OFFLINE_DIR="${SCRIPT_DIR}/offline"
DEBS_DIR="${OFFLINE_DIR}/debs"
WHEELS_DIR="${OFFLINE_DIR}/wheels"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${CYAN}── $* ──${NC}"; }

# Target platform — must match the airgapped server exactly for wheel ABI compat.
TARGET_OS_ID="ubuntu"
TARGET_OS_VERSION="24.04"
TARGET_PY="python3.12"   # Ubuntu 24.04 default
TARGET_PY_TAG="cp312"
TARGET_PLATFORM="manylinux_2_39_x86_64"

# ── Sanity check the build host ───────────────────────────────────────────────
section "Verifying build host matches target (${TARGET_OS_ID} ${TARGET_OS_VERSION})"

if [[ ! -r /etc/os-release ]]; then
    error "/etc/os-release missing — cannot verify host OS"
fi
. /etc/os-release
if [[ "${ID:-}" != "${TARGET_OS_ID}" || "${VERSION_ID:-}" != "${TARGET_OS_VERSION}" ]]; then
    warn "Build host is ${ID:-?} ${VERSION_ID:-?}, target is ${TARGET_OS_ID} ${TARGET_OS_VERSION}"
    warn "Wheels and .debs may not be compatible. Continue at your own risk."
    read -rp "  Continue anyway? [y/N] " ans
    [[ "${ans,,}" == "y" || "${ans,,}" == "yes" ]] || exit 1
fi

if ! command -v "${TARGET_PY}" &>/dev/null; then
    error "${TARGET_PY} not found on build host. Install with: sudo apt-get install ${TARGET_PY} ${TARGET_PY}-venv"
fi
info "Using $(command -v ${TARGET_PY}) ($(${TARGET_PY} --version))"

for cmd in apt-get apt-cache dpkg-deb; do
    command -v "$cmd" &>/dev/null || error "${cmd} not found — apt-based host required"
done

# ── Reset output dir ──────────────────────────────────────────────────────────
section "Preparing offline/"
rm -rf "${OFFLINE_DIR}"
mkdir -p "${DEBS_DIR}" "${WHEELS_DIR}"

# ── Download .deb packages with full dependency closure ───────────────────────
section "Downloading apt packages (with dependency closure)"

APT_PACKAGES=(
    libmagic1
    libssl-dev
    curl
    openssl
    ca-certificates
    gcc
    python3-dev
    build-essential
    python3.12-venv
    python3-venv
    libcap2-bin
    rsync
)

# Resolve the full transitive dependency set. apt-rdepends would be cleaner but
# isn't always installed; this awk pipeline gives us the same result using
# apt-cache depends, which is in apt itself.
info "Resolving dependency closure for: ${APT_PACKAGES[*]}"

resolve_deps() {
    local seen_file
    seen_file=$(mktemp)
    local queue=("$@")
    while [[ ${#queue[@]} -gt 0 ]]; do
        local pkg="${queue[0]}"
        queue=("${queue[@]:1}")
        if grep -Fxq "${pkg}" "${seen_file}" 2>/dev/null; then continue; fi
        echo "${pkg}" >> "${seen_file}"
        # Pull "Depends:" and "PreDepends:" lines, strip alternatives and version constraints
        while IFS= read -r dep; do
            dep=$(echo "$dep" | sed -e 's/|.*//' -e 's/(.*)//' -e 's/[<>=].*//' | xargs)
            [[ -n "$dep" ]] && queue+=("$dep")
        done < <(apt-cache depends --no-recommends --no-suggests --no-conflicts \
                                   --no-breaks --no-replaces --no-enhances "${pkg}" 2>/dev/null \
                 | awk '/Depends:|PreDepends:/ {print $2}')
    done
    sort -u "${seen_file}"
    rm -f "${seen_file}"
}

ALL_PKGS=()
while IFS= read -r p; do ALL_PKGS+=("$p"); done < <(resolve_deps "${APT_PACKAGES[@]}")
info "Resolved ${#ALL_PKGS[@]} packages total"

# Update apt cache once so apt-get download has fresh metadata
apt-get update -q

cd "${DEBS_DIR}"
# apt-get download fetches a single .deb without installing. Ignore failures
# for virtual packages (they have no .deb).
FAILED=()
for pkg in "${ALL_PKGS[@]}"; do
    if ! apt-get download "${pkg}" 2>/dev/null; then
        FAILED+=("${pkg}")
    fi
done
cd "${SCRIPT_DIR}"

DEB_COUNT=$(find "${DEBS_DIR}" -name '*.deb' | wc -l)
info "Downloaded ${DEB_COUNT} .deb files"
if [[ ${#FAILED[@]} -gt 0 ]]; then
    warn "Skipped (likely virtual or already-essential): ${FAILED[*]}"
fi

# ── Download Python wheels ────────────────────────────────────────────────────
section "Downloading Python wheels"

# Grab a current pip wheel too so the offline installer can upgrade pip.
# Most projects tag wheels against older manylinux baselines (glibc 2.17 / 2.28)
# rather than the newest one. Pass every tag the target can load so pip will pick
# whatever is published — newest first, then progressively older fallbacks.
PLATFORM_ARGS=(
    --platform "${TARGET_PLATFORM}"
    --platform manylinux_2_28_x86_64
    --platform manylinux_2_17_x86_64
    --platform manylinux2014_x86_64
)

"${TARGET_PY}" -m pip download \
    --dest "${WHEELS_DIR}" \
    --only-binary=:all: \
    "${PLATFORM_ARGS[@]}" \
    --python-version "${TARGET_PY_TAG#cp}" \
    --implementation cp \
    --abi "${TARGET_PY_TAG}" \
    pip setuptools wheel || warn "pip/setuptools/wheel download had issues — continuing"

"${TARGET_PY}" -m pip download \
    --dest "${WHEELS_DIR}" \
    --only-binary=:all: \
    "${PLATFORM_ARGS[@]}" \
    --python-version "${TARGET_PY_TAG#cp}" \
    --implementation cp \
    --abi "${TARGET_PY_TAG}" \
    -r "${SCRIPT_DIR}/requirements.txt"

WHEEL_COUNT=$(find "${WHEELS_DIR}" -name '*.whl' -o -name '*.tar.gz' | wc -l)
info "Downloaded ${WHEEL_COUNT} wheel/sdist files"

# ── Manifest ──────────────────────────────────────────────────────────────────
section "Writing manifest"
{
    echo "# Anvil offline bundle manifest"
    echo "built_on: $(date -Iseconds)"
    echo "build_host: ${ID} ${VERSION_ID} ($(uname -m))"
    echo "target: ${TARGET_OS_ID} ${TARGET_OS_VERSION} / ${TARGET_PY} (${TARGET_PY_TAG})"
    echo "deb_count: ${DEB_COUNT}"
    echo "wheel_count: ${WHEEL_COUNT}"
} > "${OFFLINE_DIR}/MANIFEST.txt"
cat "${OFFLINE_DIR}/MANIFEST.txt"

BUNDLE_SIZE=$(du -sh "${OFFLINE_DIR}" | awk '{print $1}')

echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Offline bundle ready                             ║${NC}"
echo -e "${GREEN}╠═══════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}  Location: ${CYAN}${OFFLINE_DIR}${NC}"
echo -e "${GREEN}║${NC}  Size:     ${CYAN}${BUNDLE_SIZE}${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}  Next steps:${NC}"
echo -e "    1. SCP the entire server/ directory to the airgapped host"
echo -e "    2. On the server: sudo bash setup-offline.sh"
echo ""
