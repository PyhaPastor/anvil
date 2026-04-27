#!/usr/bin/env bash
# =============================================================================
# Anvil Kiosk Setup Script
# =============================================================================
# Run ONCE on the kiosk machine as root:
#   sudo bash setup-kiosk.sh
#
# What this script does:
#   1.  Interactive config prompts (URL, connection type, WiFi, schedule)
#   2.  Installs required packages
#   3.  Creates the kiosk OS user
#   4.  Configures LightDM autologin (X11, no Wayland)
#   5.  Fetches and installs the Anvil server's TLS certificate (system + NSS)
#   6.  Configures WiFi via NetworkManager (if selected)
#   7.  Writes /opt/kiosk/kiosk.conf — single source of truth for runtime config
#   8.  Deploys kiosk-launch.sh, watchdog.sh, and no-connection.html
#   9.  Wires up autostart entries (GNOME + Openbox)
#   10. Suppresses OS update pop-ups
#   11. Schedules daily shutdown via cron
#   12. Masks systemd sleep/suspend/hibernate
# =============================================================================

set -euo pipefail

# ── Helpers ───────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; CYN='\033[0;36m'; BLD='\033[1m'; RST='\033[0m'
info()  { echo -e "${GRN}[kiosk]${RST} $*"; }
warn()  { echo -e "${YLW}[warn] ${RST} $*"; }
die()   { echo -e "${RED}[error]${RST} $*" >&2; exit 1; }

prompt() {
    # prompt <VAR> <question> <default>
    local var="$1" question="$2" default="${3:-}"
    local input
    echo -ne "${CYN}  ${question}${RST}"
    [[ -n "$default" ]] && echo -ne " ${BLD}[${default}]${RST}"
    echo -ne " : "
    read -r input
    printf -v "$var" '%s' "${input:-$default}"
}

prompt_secret() {
    # prompt_secret <VAR> <question>
    local var="$1" question="$2"
    local input
    echo -ne "${CYN}  ${question}${RST} : "
    read -rs input
    echo ""
    printf -v "$var" '%s' "$input"
}

confirm() {
    local ans
    echo -ne "${CYN}  $1${RST} ${BLD}[y/N]${RST} : "
    read -r ans
    [[ "${ans,,}" == "y" || "${ans,,}" == "yes" ]]
}

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"

# ── Interactive configuration ─────────────────────────────────────────────────

echo ""
echo -e "${BLD}╔══════════════════════════════════════════════════════╗${RST}"
echo -e "${BLD}║        Anvil Kiosk — Interactive Setup               ║${RST}"
echo -e "${BLD}╚══════════════════════════════════════════════════════╝${RST}"
echo ""
echo -e "  Press ${BLD}Enter${RST} to accept the default shown in [brackets]."
echo ""

# Anvil server URL
prompt ANVIL_URL "Anvil server URL (include https://, no trailing slash)" "https://anvil.local"
# Strip any trailing slash or /dashboard the user may have typed, then pin to /dashboard
ANVIL_URL="${ANVIL_URL%/}"
ANVIL_URL="${ANVIL_URL%/dashboard}/dashboard"

# Connection type
echo ""
echo -e "  Connection type:"
echo -e "    ${BLD}1${RST}  Wired (Ethernet)"
echo -e "    ${BLD}2${RST}  WiFi"
echo ""
echo -ne "${CYN}  Select [1/2]${RST} ${BLD}[1]${RST} : "
read -r CONN_CHOICE
CONN_CHOICE="${CONN_CHOICE:-1}"

CONN_TYPE="cable"
WIFI_SSID=""
WIFI_PASSWORD=""
WIFI_HIDDEN="no"

if [[ "$CONN_CHOICE" == "2" ]]; then
    CONN_TYPE="wifi"
    echo ""
    prompt WIFI_SSID "WiFi network name (SSID)" ""
    [[ -n "$WIFI_SSID" ]] || die "WiFi SSID cannot be empty."
    prompt_secret WIFI_PASSWORD "WiFi password (hidden)"
    echo ""
    echo -ne "${CYN}  Is this a hidden network (does not broadcast SSID)?${RST} ${BLD}[y/N]${RST} : "
    read -r _hidden_ans
    [[ "${_hidden_ans,,}" == "y" || "${_hidden_ans,,}" == "yes" ]] && WIFI_HIDDEN="yes"
fi

# Shutdown schedule
echo ""
echo ""
echo -e "  Display zoom — use 100 for normal, 150 for 4K/HiDPI screens:"
echo -e "    ${BLD}100${RST} = default (1:1)"
echo -e "    ${BLD}125${RST} = 125%"
echo -e "    ${BLD}150${RST} = 150% (recommended for 4K)"
echo -e "    ${BLD}200${RST} = 200%"
echo ""
prompt ZOOM_PCT "Display zoom percentage" "100"
[[ "$ZOOM_PCT" =~ ^[0-9]+$ ]] || die "Zoom must be a number (e.g. 100, 150, 200)."

prompt SHUTDOWN_TIME "Daily auto-shutdown time (HH:MM, 24 h)" "18:00"

echo ""
echo -e "  Shutdown days — cron day-of-week field:"
echo -e "    ${BLD}*${RST}     = every day"
echo -e "    ${BLD}1-5${RST}   = Monday–Friday"
echo -e "    ${BLD}1,3,5${RST} = Mon, Wed, Fri"
echo -e "    ${BLD}6,0${RST}   = Sat + Sun"
echo ""
prompt SHUTDOWN_DAYS "Shutdown days (cron field)" "*"

# Validate
[[ "$SHUTDOWN_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] \
    || die "Invalid time '${SHUTDOWN_TIME}'. Use HH:MM (e.g. 18:00)."

# Confirm
echo ""
echo -e "${BLD}  Summary:${RST}"
echo "  ┌────────────────────────────────────────────────────"
echo "  │  Anvil URL    : ${ANVIL_URL}"
if [[ "$CONN_TYPE" == "wifi" ]]; then
echo "  │  Connection   : WiFi — ${WIFI_SSID}$([[ "$WIFI_HIDDEN" == "yes" ]] && echo " (hidden)")"
else
echo "  │  Connection   : Wired (Ethernet)"
fi
echo "  │  Zoom         : ${ZOOM_PCT}%"
echo "  │  Shutdown     : ${SHUTDOWN_TIME}  days '${SHUTDOWN_DAYS}'"
echo "  └────────────────────────────────────────────────────"
echo ""
confirm "Proceed with installation?" || { echo "Aborted."; exit 0; }
echo ""

# ── Derived values ────────────────────────────────────────────────────────────

KIOSK_USER="kiosk"
KIOSK_HOME="/home/${KIOSK_USER}"
KIOSK_DIR="/opt/kiosk"
SHUTDOWN_HOUR="${SHUTDOWN_TIME%%:*}"
SHUTDOWN_MIN="${SHUTDOWN_TIME##*:}"

# Extract host:port from URL for cert fetch
ANVIL_HOST=$(echo "$ANVIL_URL" | sed 's|https\?://||' | cut -d/ -f1)
ANVIL_PORT="${ANVIL_HOST##*:}"
ANVIL_HOST="${ANVIL_HOST%%:*}"
[[ "$ANVIL_PORT" == "$ANVIL_HOST" ]] && ANVIL_PORT="443"   # no explicit port

# ── 1. WiFi — connect before anything else ───────────────────────────────────
#
# apt-get needs internet. No point installing packages if WiFi isn't working.
# NetworkManager and nmcli ship with Ubuntu Desktop so no install needed first.

if [[ "$CONN_TYPE" == "wifi" ]]; then
    info "Configuring WiFi before package install..."

    systemctl enable NetworkManager 2>/dev/null || true
    systemctl start  NetworkManager 2>/dev/null || true

    # NM global config: disable power saving + random MAC (hidden SSID killer)
    mkdir -p /etc/NetworkManager/conf.d
    cat > /etc/NetworkManager/conf.d/99-kiosk-wifi.conf << 'NM_CONF'
[connection]
wifi.powersave=2

[device]
wifi.scan-rand-mac-address=no
NM_CONF

    # Kernel driver level power save — catches drivers that ignore NM config
    mkdir -p /etc/pm/power.d
    cat > /etc/pm/power.d/99-kiosk-wifi << 'PM_CONF'
#!/bin/sh
for iface in /sys/class/net/wl*; do
    iface_name=$(basename "$iface")
    /sbin/iwconfig "$iface_name" power off 2>/dev/null || true
done
PM_CONF
    chmod +x /etc/pm/power.d/99-kiosk-wifi

    # Apply power-off immediately to active wireless interfaces
    for iface in /sys/class/net/wl*; do
        iwconfig "$(basename "$iface")" power off 2>/dev/null || true
    done

    # Build connection profile
    nmcli connection delete "$WIFI_SSID" 2>/dev/null || true

    if [[ "$WIFI_HIDDEN" == "yes" ]]; then
        nmcli connection add \
            type wifi \
            con-name  "$WIFI_SSID" \
            ssid      "$WIFI_SSID" \
            802-11-wireless.hidden yes \
            wifi-sec.key-mgmt wpa-psk \
            wifi-sec.psk      "$WIFI_PASSWORD" 2>/dev/null \
            || warn "Could not create hidden WiFi profile."
    else
        nmcli device wifi connect "$WIFI_SSID" \
            password "$WIFI_PASSWORD" \
            name     "$WIFI_SSID" 2>/dev/null \
            || warn "Could not connect to '${WIFI_SSID}' right now."
    fi

    # Tune for maximum persistence
    nmcli connection modify "$WIFI_SSID" \
        connection.autoconnect          yes \
        connection.autoconnect-priority 100 \
        connection.autoconnect-retries  0   \
        connection.permissions          ""  2>/dev/null || true

    # Restart NM to apply global config, then bring the connection up
    systemctl restart NetworkManager
    sleep 3
    nmcli connection up "$WIFI_SSID" 2>/dev/null || true

    # Verify connectivity — wait up to 30 s for an IP
    info "Verifying WiFi connectivity..."
    WIFI_OK=false
    for i in $(seq 1 10); do
        if nmcli -t -f active,ssid dev wifi 2>/dev/null | grep -q "^yes:"; then
            WIFI_OK=true
            break
        fi
        sleep 3
    done

    if $WIFI_OK; then
        info "WiFi connected to '${WIFI_SSID}'. Proceeding with installation."
    else
        warn "Could not verify WiFi connection to '${WIFI_SSID}'."
        warn "Installation will continue — the profile is saved and will retry on boot."
        confirm "Continue anyway?" || { echo "Aborted."; exit 1; }
    fi
fi

# ── 2. Install required packages ──────────────────────────────────────────────

info "Installing packages..."
apt-get update -qq
apt-get install -y -qq \
    xdotool \
    curl \
    openssl \
    libnss3-tools \
    unclutter \
    alsa-utils \
    x11-xserver-utils \
    xorg \
    openbox \
    lightdm \
    lightdm-gtk-greeter \
    network-manager \
    2>/dev/null || true

# ── Install Google Chrome (proper .deb — avoids snap AppArmor conflicts) ──────
# The Ubuntu snap version of chromium-browser is confined by AppArmor in a way
# that breaks Chromium's internal IPC (zygote/Mojo) when running as a kiosk
# user in a custom Openbox session.  Google Chrome ships as a plain .deb with
# no snap confinement and works reliably in this context.
if ! command -v google-chrome-stable &>/dev/null; then
    info "Installing Google Chrome..."
    _chrome_deb="/tmp/google-chrome-stable.deb"
    curl -fsSL "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" \
         -o "$_chrome_deb" \
    && apt-get install -y "$_chrome_deb" \
    && rm -f "$_chrome_deb" \
    || warn "Chrome install failed — falling back to system chromium"
fi

# Remove snap chromium if present — it conflicts with the kiosk user session
if snap list chromium &>/dev/null 2>&1; then
    info "Removing snap chromium (incompatible with kiosk session)..."
    snap remove chromium 2>/dev/null || true
fi

CHROMIUM_BIN="$(command -v google-chrome-stable 2>/dev/null \
             || command -v google-chrome 2>/dev/null \
             || command -v chromium-browser 2>/dev/null \
             || command -v chromium)"

if [[ -z "$CHROMIUM_BIN" ]]; then
    die "No usable browser found. Install Google Chrome and re-run."
fi
info "Browser: $CHROMIUM_BIN"

# ── 2. Create kiosk user ──────────────────────────────────────────────────────

if ! id "$KIOSK_USER" &>/dev/null; then
    info "Creating user '${KIOSK_USER}'..."
    useradd -m -s /bin/bash -G audio,video,plugdev,netdev "$KIOSK_USER"
    passwd -l "$KIOSK_USER"
else
    info "User '${KIOSK_USER}' already exists."
fi

# ── 3. Display manager: autologin + force X11 ─────────────────────────────────

info "Configuring LightDM autologin (X11)..."

systemctl set-default graphical.target
systemctl enable lightdm 2>/dev/null || true

# If GDM3 is active, switch default to LightDM
if systemctl is-active --quiet gdm3 2>/dev/null; then
    warn "GDM3 is currently active — switching default to LightDM."
    warn "If prompted during install, select 'lightdm' as the display manager."
    systemctl disable gdm3 2>/dev/null || true
    systemctl enable lightdm 2>/dev/null || true
fi

mkdir -p /etc/lightdm/lightdm.conf.d
cat > /etc/lightdm/lightdm.conf.d/50-kiosk.conf << EOF
[SeatDefaults]
autologin-user=${KIOSK_USER}
autologin-user-timeout=0
user-session=openbox
xserver-command=X -s 0 -dpms
EOF

# ── 4. TLS certificate — fetch and install ────────────────────────────────────
#
# Anvil uses a self-signed certificate. We fetch it directly from the server
# and install it in two places:
#   a) System CA store  — so curl and OS tools trust it
#   b) Chromium NSS db  — so the browser trusts it without a warning page
#
# --ignore-certificate-errors is kept as a safety fallback in Chromium flags.

info "Fetching TLS certificate from ${ANVIL_HOST}:${ANVIL_PORT}..."

CERT_DIR="/usr/local/share/ca-certificates"
CERT_FILE="${CERT_DIR}/anvil-server.crt"
mkdir -p "$CERT_DIR"

if openssl s_client -connect "${ANVIL_HOST}:${ANVIL_PORT}" \
        -servername "$ANVIL_HOST" </dev/null 2>/dev/null \
        | openssl x509 -outform PEM > "$CERT_FILE" 2>/dev/null \
   && [[ -s "$CERT_FILE" ]]; then

    info "Certificate fetched — installing in system trust store..."
    update-ca-certificates --fresh 2>/dev/null || update-ca-certificates

    # Install into Chromium's NSS database for the kiosk user
    NSS_DIR="${KIOSK_HOME}/.pki/nssdb"
    mkdir -p "$NSS_DIR"
    certutil -d "sql:${NSS_DIR}" -N --empty-password 2>/dev/null || true
    certutil -d "sql:${NSS_DIR}" -A -n "anvil-server" -t "CT,," -i "$CERT_FILE" 2>/dev/null \
        && info "Certificate installed in Chromium NSS database." \
        || warn "NSS install failed — browser will fall back to --ignore-certificate-errors."
    chown -R "${KIOSK_USER}:${KIOSK_USER}" "${KIOSK_HOME}/.pki"

else
    warn "Could not reach ${ANVIL_HOST}:${ANVIL_PORT} to fetch certificate."
    warn "Chromium will use --ignore-certificate-errors as fallback."
    rm -f "$CERT_FILE"
fi

# ── 5. Runtime config file ────────────────────────────────────────────────────
#
# Single source of truth for launcher and watchdog.
# Edit this file to change settings without re-running setup.

info "Writing runtime config to ${KIOSK_DIR}/kiosk.conf..."
mkdir -p "$KIOSK_DIR"

cat > "${KIOSK_DIR}/kiosk.conf" << EOF
# Anvil Kiosk — runtime configuration
# Generated by setup-kiosk.sh on $(date -I)
# Edit and save — changes take effect on next kiosk restart.

ANVIL_URL="${ANVIL_URL}"
CHROMIUM="${CHROMIUM_BIN}"
CONN_TYPE="${CONN_TYPE}"       # "cable" or "wifi"
WIFI_SSID="${WIFI_SSID}"       # ignored when CONN_TYPE=cable
WIFI_HIDDEN="${WIFI_HIDDEN}"   # "yes" if SSID is not broadcast
ZOOM_PCT="${ZOOM_PCT}"         # display zoom percentage (100 = normal, 150 = 4K)
EOF

chmod 640 "${KIOSK_DIR}/kiosk.conf"

# ── 7. No-connection splash page ──────────────────────────────────────────────

cat > "${KIOSK_DIR}/no-connection.html" << 'HTML'
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta http-equiv="refresh" content="15"/>
<title>No Connection</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #09090b;
    color: #a1a1aa;
    font-family: ui-monospace, monospace;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100vh;
    gap: 1.5rem;
  }
  svg { opacity: .35; }
  h1  { color: #e4e4e7; font-size: 1.4rem; font-weight: 600; letter-spacing: .02em; }
  p   { font-size: .9rem; opacity: .6; }
  .dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: #52525b;
    animation: pulse 2s ease-in-out infinite;
  }
  .dot:nth-child(2) { animation-delay: .3s; }
  .dot:nth-child(3) { animation-delay: .6s; }
  .dots { display: flex; gap: .5rem; }
  @keyframes pulse { 0%,100%{opacity:.2} 50%{opacity:1} }
</style>
</head>
<body>
  <svg width="56" height="56" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24">
    <path stroke-linecap="round" stroke-linejoin="round"
      d="M8.288 15.038a5.25 5.25 0 0 1 7.424 0M5.106 11.856c3.807-3.808 9.98-3.808 13.788 0M1.924 8.674c5.565-5.565 14.587-5.565 20.152 0M12 18.75h.008v.008H12v-.008Z"/>
    <line x1="3" y1="3" x2="21" y2="21" stroke-linecap="round"/>
  </svg>
  <h1>Waiting for network</h1>
  <p>Retrying every 15 seconds&hellip;</p>
  <div class="dots"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>
</body>
</html>
HTML

# ── 8. Main launcher ──────────────────────────────────────────────────────────

cat > "${KIOSK_DIR}/kiosk-launch.sh" << 'LAUNCH'
#!/usr/bin/env bash
# Anvil Kiosk — main launcher
# Started by Openbox/GNOME autostart as the kiosk user.

source /opt/kiosk/kiosk.conf

DISPLAY="${DISPLAY:-:0}"
export DISPLAY

# ── Raise open-file limit before launching Chromium ──────────────
# limits.conf is often ignored by lightdm/GUI sessions — set it here
# directly so it always applies regardless of PAM configuration.
ulimit -n 65536 2>/dev/null || ulimit -n 4096 2>/dev/null || true

# ── Kill any leftover browser ─────────────────────────────────────
pkill -f "google-chrome"     2>/dev/null || true
pkill -f "chromium"          2>/dev/null || true
sleep 1

# ── Disable ALL power management / screensaver ────────────────────
xset s off          # disable screensaver timer
xset s noblank      # never blank the screen
xset -dpms          # disable DPMS (monitor standby/suspend/off)

unclutter -idle 3 -root &

# GNOME overrides — harmless if not running GNOME
gsettings set org.gnome.desktop.session                    idle-delay                       0         2>/dev/null || true
gsettings set org.gnome.desktop.screensaver                lock-enabled                     false     2>/dev/null || true
gsettings set org.gnome.desktop.screensaver                idle-activation-enabled          false     2>/dev/null || true
gsettings set org.gnome.settings-daemon.plugins.power      sleep-inactive-ac-type           'nothing' 2>/dev/null || true
gsettings set org.gnome.settings-daemon.plugins.power      sleep-inactive-battery-type      'nothing' 2>/dev/null || true
gsettings set org.gnome.settings-daemon.plugins.power      power-button-action              'nothing' 2>/dev/null || true
gsettings set org.gnome.settings-daemon.plugins.power      idle-dim                         false     2>/dev/null || true

# ── Mute all audio ────────────────────────────────────────────────
# Try all three audio stacks — whichever is present will respond.
# PipeWire (Ubuntu 22.04+)
wpctl set-mute @DEFAULT_AUDIO_SINK@ 1       2>/dev/null || true
wpctl set-volume @DEFAULT_AUDIO_SINK@ 0     2>/dev/null || true
# PulseAudio
pactl set-sink-mute @DEFAULT_SINK@ 1        2>/dev/null || true
# ALSA
amixer -q sset Master mute                  2>/dev/null || true
amixer -q sset PCM    mute                  2>/dev/null || true
# GNOME sound events
gsettings set org.gnome.desktop.sound event-sounds false 2>/dev/null || true

# ── Wait for network connectivity ─────────────────────────────────
_wait_network() {
    local timeout=120 elapsed=0

    if [[ "$CONN_TYPE" == "wifi" ]]; then
        echo "[kiosk] Waiting for WiFi: ${WIFI_SSID}..."
        while true; do
            CURRENT=$(nmcli -t -f active,ssid dev wifi 2>/dev/null \
                      | grep "^yes:" | cut -d: -f2 | tr -d '"')
            [[ "$CURRENT" == "$WIFI_SSID" ]] && { echo "[kiosk] WiFi connected."; return 0; }
            nmcli con up "$WIFI_SSID" 2>/dev/null \
                || { [[ "$WIFI_HIDDEN" == "yes" ]] && \
                     nmcli device wifi connect "$WIFI_SSID" hidden yes 2>/dev/null; } \
                || true
            sleep 3; elapsed=$((elapsed + 3))
            if [[ $elapsed -ge $timeout ]]; then
                echo "[kiosk] WiFi not available — showing no-connection page."
                return 1
            fi
        done
    else
        echo "[kiosk] Waiting for Ethernet..."
        until nmcli -t -f state g 2>/dev/null | grep -q "connected"; do
            sleep 2; elapsed=$((elapsed + 2))
            [[ $elapsed -ge $timeout ]] && { echo "[kiosk] No Ethernet — continuing anyway."; return 1; }
        done
        return 0
    fi
}

# ── Wait for Anvil to return 200 at /dashboard ────────────────────
# This prevents launching the browser before the server is ready,
# and confirms the kiosk IP bypass is active before we open anything.
_wait_server() {
    local timeout=120 elapsed=0
    echo "[kiosk] Waiting for Anvil server..."
    while true; do
        FINAL=$(curl -skL --max-time 5 \
                     -w "%{url_effective}" -o /dev/null \
                     "$ANVIL_URL" 2>/dev/null || true)
        if [[ "$FINAL" == *"/dashboard"* ]]; then
            echo "[kiosk] Server ready."
            return 0
        fi
        sleep 3; elapsed=$((elapsed + 3))
        if [[ $elapsed -ge $timeout ]]; then
            echo "[kiosk] Server not ready after ${timeout}s — launching anyway."
            return 1
        fi
    done
}

_launch_browser() {
    local url="$1"
    # Wipe the Chromium profile before every launch so no cached redirects
    # survive across reboots (e.g. a stale /dashboard→/login 302 from before
    # the kiosk IP was added to the allowlist).
    rm -rf /tmp/kiosk-profile
    mkdir -p /tmp/kiosk-profile

    "$CHROMIUM" \
        --user-data-dir=/tmp/kiosk-profile \
        --kiosk \
        --noerrdialogs \
        --disable-infobars \
        --no-first-run \
        --disable-session-crashed-bubble \
        --disable-restore-session-state \
        --disable-features=TranslateUI,Translate \
        --check-for-update-interval=604800 \
        --ignore-certificate-errors \
        --disable-pinch \
        --overscroll-history-navigation=0 \
        --disable-notifications \
        --mute-audio \
        --start-fullscreen \
        --disable-smooth-scrolling \
        --disable-background-networking \
        --disable-default-apps \
        --disable-sync \
        --disable-translate \
        --disable-background-timer-throttling \
        --disable-renderer-backgrounding \
        --disable-dev-shm-usage \
        --no-sandbox \
        --password-store=basic \
        --disable-features=TabDiscarding,AutomaticTabDiscarding,MemorySaver,CalculateNativeWinOcclusion \
        --disable-hang-monitor \
        --force-device-scale-factor="$(awk "BEGIN{printf \"%.2f\", ${ZOOM_PCT}/100}")" \
        --display="$DISPLAY" \
        "$url" >> /tmp/chromium-kiosk.log 2>&1 &
}

if _wait_network; then
    _wait_server
    _launch_browser "$ANVIL_URL"
else
    _launch_browser "file:///opt/kiosk/no-connection.html"
fi

# ── Start watchdog (kill any stale instance first) ───────────────
pkill -f "watchdog.sh" 2>/dev/null || true
sleep 0.5
/opt/kiosk/watchdog.sh &

wait
LAUNCH

# ── 9. Watchdog ───────────────────────────────────────────────────────────────

cat > "${KIOSK_DIR}/watchdog.sh" << 'WATCHDOG'
#!/usr/bin/env bash
# Anvil Kiosk — watchdog
# Loops every 60 s:
#   - Re-asserts DPMS/blanking off (display managers can re-enable it)
#   - WiFi: verifies connected to expected SSID; shows no-connection page if not
#   - Crash recovery: relaunches Chromium only if the process has died

source /opt/kiosk/kiosk.conf

INTERVAL=60
DISPLAY="${DISPLAY:-:0}"
export DISPLAY

# ── Singleton: kill any OTHER running watchdog instances first ────────────────
# Multiple instances accumulate when kiosk-launch.sh is run more than once
# (e.g. Ctrl+Alt+R, reinstall without reboot). Each runs its own 60s health
# check loop at a different phase offset, causing restarts every ~30s instead
# of ~120s. Kill stale copies before doing anything else.
MYPID=$$
for _stale in $(pgrep -f "watchdog.sh" 2>/dev/null); do
    [[ "$_stale" == "$MYPID" ]] && continue
    kill "$_stale" 2>/dev/null || true
done
unset _stale

# ── Logging ───────────────────────────────────────────────────────────────────
LOG=/tmp/anvil-watchdog.log
_log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }
# Keep log under 500 KB — rotate by truncating the oldest half
if [[ -f "$LOG" ]] && (( $(stat -c%s "$LOG" 2>/dev/null || echo 0) > 500000 )); then
    tail -n 200 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi
_log "Watchdog started (pid=$MYPID, interval=${INTERVAL}s)"

# Track whether we are currently showing the no-connection page
SHOWING_NO_CONNECTION=false

_browser_running() {
    # Google Chrome's actual process name is "chrome", not "google-chrome".
    # Use -f (full command line match) to catch all variants reliably.
    pgrep -f "google-chrome" > /dev/null \
    || pgrep -f "chromium"   > /dev/null
}

_kill_browser() {
    pkill -f "google-chrome"     2>/dev/null || true
    pkill -f "chromium"          2>/dev/null || true
    sleep 1
}

_launch_browser() {
    local url="$1"
    rm -rf /tmp/kiosk-profile
    mkdir -p /tmp/kiosk-profile
    "$CHROMIUM" \
        --user-data-dir=/tmp/kiosk-profile \
        --kiosk \
        --noerrdialogs \
        --disable-infobars \
        --no-first-run \
        --disable-session-crashed-bubble \
        --disable-restore-session-state \
        --disable-features=TranslateUI,Translate \
        --check-for-update-interval=604800 \
        --ignore-certificate-errors \
        --disable-pinch \
        --overscroll-history-navigation=0 \
        --disable-notifications \
        --mute-audio \
        --start-fullscreen \
        --disable-smooth-scrolling \
        --disable-background-networking \
        --disable-default-apps \
        --disable-sync \
        --disable-translate \
        --disable-background-timer-throttling \
        --disable-renderer-backgrounding \
        --disable-dev-shm-usage \
        --no-sandbox \
        --password-store=basic \
        --disable-features=TabDiscarding,AutomaticTabDiscarding,MemorySaver,CalculateNativeWinOcclusion \
        --disable-hang-monitor \
        --force-device-scale-factor="$(awk "BEGIN{printf \"%.2f\", ${ZOOM_PCT}/100}")" \
        --display="$DISPLAY" \
        "$url" >> /tmp/chromium-kiosk.log 2>&1 &
}

while true; do
    sleep "$INTERVAL"

    # ── WiFi reconnect (cable installs skip this block) ───────────
    if [[ "$CONN_TYPE" == "wifi" ]]; then
        CURRENT_SSID=$(nmcli -t -f active,ssid dev wifi 2>/dev/null \
                       | grep "^yes:" | cut -d: -f2 | tr -d '"')
        if [[ "$CURRENT_SSID" != "$WIFI_SSID" ]]; then
            nmcli con up "$WIFI_SSID" 2>/dev/null \
                || { [[ "$WIFI_HIDDEN" == "yes" ]] && \
                     nmcli device wifi connect "$WIFI_SSID" hidden yes 2>/dev/null; } \
                || true
            if ! $SHOWING_NO_CONNECTION; then
                _kill_browser
                _launch_browser "file:///opt/kiosk/no-connection.html"
                SHOWING_NO_CONNECTION=true
            fi
            continue
        fi
        if $SHOWING_NO_CONNECTION; then
            _kill_browser
            sleep 2
            _launch_browser "$ANVIL_URL"
            SHOWING_NO_CONNECTION=false
            continue
        fi
    fi

    # ── Crash recovery: relaunch only if browser process is gone ──
    # No HTTP health checks — those were causing false-positive restarts.
    # The browser is left alone as long as the process is alive.
    if ! _browser_running; then
        _log "RESTART: browser not running — relaunching (watchdog instances running: $(pgrep -ac watchdog 2>/dev/null || echo '?'))"
        _launch_browser "$ANVIL_URL"
        SHOWING_NO_CONNECTION=false
        sleep 10   # let Chromium fully start before the next pgrep check
    fi

done
WATCHDOG

chmod +x "${KIOSK_DIR}/kiosk-launch.sh"
chmod +x "${KIOSK_DIR}/watchdog.sh"
chmod 644 "${KIOSK_DIR}/no-connection.html"
chown -R "${KIOSK_USER}:${KIOSK_USER}" "${KIOSK_DIR}"

# ── 10. Autostart entries ─────────────────────────────────────────────────────

info "Wiring up autostart entries..."

AUTOSTART_DIR="${KIOSK_HOME}/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "${AUTOSTART_DIR}/anvil-kiosk.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Anvil Kiosk
Exec=${KIOSK_DIR}/kiosk-launch.sh
X-GNOME-Autostart-enabled=true
Hidden=false
NoDisplay=false
Comment=Anvil kiosk browser launcher
EOF

OPENBOX_DIR="${KIOSK_HOME}/.config/openbox"
mkdir -p "$OPENBOX_DIR"
cat > "${OPENBOX_DIR}/autostart" << EOF
xset s off &
xset -dpms &
xset s noblank &
${KIOSK_DIR}/kiosk-launch.sh &
EOF

# Openbox keybindings — active even inside Chromium kiosk mode
# Ctrl+Alt+Q  : kill browser, open terminal (admin access)
# Ctrl+Alt+R  : restart kiosk (reload Anvil)
# Ctrl+Alt+Del: shutdown immediately
cat > "${OPENBOX_DIR}/rc.xml" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <keyboard>

    <!-- Exit kiosk: kill browser and open a terminal for admin work -->
    <keybind key="C-A-q">
      <action name="Execute">
        <command>bash -c 'pkill -f google-chrome; pkill -f chromium; x-terminal-emulator &amp;'</command>
      </action>
    </keybind>

    <!-- Hard stop: kill watchdog + browser entirely, no restart, drop to desktop -->
    <keybind key="C-A-x">
      <action name="Execute">
        <command>bash -c 'pkill -f watchdog.sh; pkill -f kiosk-launch.sh; pkill -f google-chrome; pkill -f chromium; x-terminal-emulator &amp;'</command>
      </action>
    </keybind>

    <!-- Restart kiosk: kill and relaunch the browser -->
    <keybind key="C-A-r">
      <action name="Execute">
        <command>bash -c 'pkill -f google-chrome; pkill -f chromium; sleep 1; /opt/kiosk/kiosk-launch.sh &amp;'</command>
      </action>
    </keybind>

    <!-- Emergency shutdown -->
    <keybind key="C-A-Delete">
      <action name="Execute">
        <command>/sbin/shutdown -h now</command>
      </action>
    </keybind>

  </keyboard>
</openbox_config>
EOF

chown -R "${KIOSK_USER}:${KIOSK_USER}" "${KIOSK_HOME}/.config"

# ── 11. Suppress OS update notifications ──────────────────────────────────────

info "Suppressing update pop-ups..."
[[ -f /etc/update-manager/release-upgrades ]] \
    && sed -i 's/^Prompt=.*/Prompt=never/' /etc/update-manager/release-upgrades

mkdir -p /etc/apt/apt.conf.d
cat > /etc/apt/apt.conf.d/99kiosk-no-notify << EOF
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Unattended-Upgrade "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::AutocleanInterval "0";
EOF

# ── 12. Scheduled shutdown ────────────────────────────────────────────────────

info "Scheduling shutdown at ${SHUTDOWN_TIME} (days: ${SHUTDOWN_DAYS})..."

CRON_FILE="/etc/cron.d/anvil-kiosk-shutdown"
cat > "$CRON_FILE" << EOF
# Anvil kiosk — daily auto-shutdown
# Generated: $(date -I)
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

${SHUTDOWN_MIN} ${SHUTDOWN_HOUR} * * ${SHUTDOWN_DAYS} root /sbin/shutdown -h now
EOF
chmod 644 "$CRON_FILE"

# ── 13. Mask systemd sleep targets ────────────────────────────────────────────

info "Masking systemd sleep targets..."
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target 2>/dev/null || true

# ── 14. Disable DPMS / blanking at the Xorg driver level ─────────────────────
#
# xset is session-level only — display managers (LightDM/GDM) and the GNOME
# power daemon can re-enable DPMS after the kiosk user logs in, overriding the
# launcher's xset call.  A xorg.conf.d snippet disables it permanently at the
# driver layer so no session-level override can re-enable it.

info "Writing Xorg no-blanking config..."
mkdir -p /etc/X11/xorg.conf.d
cat > /etc/X11/xorg.conf.d/99-no-dpms.conf << 'XORG'
# Anvil Kiosk — disable screen blanking and DPMS at the Xorg driver level.
# This overrides any session-level xset call by the display manager or DE.
Section "ServerFlags"
    Option "BlankTime"   "0"
    Option "StandbyTime" "0"
    Option "SuspendTime" "0"
    Option "OffTime"     "0"
EndSection

Section "Monitor"
    Identifier "DefaultMonitor"
    Option     "DPMS" "false"
EndSection
XORG

# ── 15. Disable systemd-logind idle action ────────────────────────────────────
#
# logind has its own idle-action mechanism entirely separate from X DPMS.
# Without this, logind can blank/suspend the VT even when X says not to.

info "Writing logind no-idle config..."
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/99-kiosk.conf << 'LOGIND'
[Login]
# Never blank, suspend, or lock from idle — kiosk runs 24/7
IdleAction=ignore
HandleSuspendKey=ignore
HandleHibernateKey=ignore
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleActionSec=0
LOGIND
systemctl daemon-reload 2>/dev/null || true
systemctl restart systemd-logind 2>/dev/null || true

# ── 16. Raise open-file limit for kiosk user ─────────────────────────────────
#
# The default Linux limit of 1024 open file descriptors is too low for
# Chromium — it spawns multiple processes (renderer, GPU, network service)
# each needing FDs, plus WebSocket connections and profile file handles.
# Hitting the limit causes silent crashes with no OOM entry in dmesg.

info "Setting open-file limits for ${KIOSK_USER}..."
grep -qF "kiosk soft nofile" /etc/security/limits.conf 2>/dev/null \
    || echo "${KIOSK_USER} soft nofile 65536" >> /etc/security/limits.conf
grep -qF "kiosk hard nofile" /etc/security/limits.conf 2>/dev/null \
    || echo "${KIOSK_USER} hard nofile 65536" >> /etc/security/limits.conf

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GRN}═══════════════════════════════════════════════════════${RST}"
echo -e "${GRN}  Anvil Kiosk setup complete${RST}"
echo -e "${GRN}═══════════════════════════════════════════════════════${RST}"
echo ""
echo "  Anvil URL      : ${ANVIL_URL}"
if [[ "$CONN_TYPE" == "wifi" ]]; then
echo "  Connection     : WiFi — ${WIFI_SSID}"
else
echo "  Connection     : Wired (Ethernet)"
fi
echo "  Shutdown       : ${SHUTDOWN_TIME}  days '${SHUTDOWN_DAYS}'"
echo "  Config file    : ${KIOSK_DIR}/kiosk.conf"
echo "  Shutdown cron  : ${CRON_FILE}"
echo ""
echo "  Keyboard shortcuts (active in kiosk mode):"
echo "    Ctrl+Alt+Q      — kill browser, open terminal (watchdog may relaunch browser)"
echo "    Ctrl+Alt+X      — HARD STOP: kill watchdog + browser, drop to desktop"
echo "    Ctrl+Alt+R      — restart kiosk / reload browser"
echo "    Ctrl+Alt+Delete — immediate shutdown"
echo ""
echo "  To change any setting after install, edit:"
echo "    sudo nano ${KIOSK_DIR}/kiosk.conf"
echo ""
echo "  Watchdog log (written at runtime):"
echo "    /tmp/anvil-watchdog.log"
echo ""
echo "  Reboot to start the kiosk session (recommended after reinstall):"
echo "    sudo reboot"
echo ""
