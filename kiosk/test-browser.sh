#!/usr/bin/env bash
# Anvil Kiosk — manual browser test
# Run from a terminal on the kiosk machine to see raw Chromium output.
# Change ANVIL_URL to your server address before running.
#
# Usage:
#   chmod +x test-browser.sh
#   ./test-browser.sh

ANVIL_URL="https://YOUR-SERVER-ADDRESS/dashboard"

DISPLAY="${DISPLAY:-:0}"
export DISPLAY

CHROMIUM="$(command -v chromium-browser 2>/dev/null || command -v chromium)"

if [[ -z "$CHROMIUM" ]]; then
    echo "ERROR: chromium not found"
    exit 1
fi

echo "Using: $CHROMIUM"
echo "URL:   $ANVIL_URL"
echo "Press Ctrl+C to stop."
echo ""

rm -rf /tmp/kiosk-test-profile
mkdir -p /tmp/kiosk-test-profile

"$CHROMIUM" \
    --user-data-dir=/tmp/kiosk-test-profile \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --disable-session-crashed-bubble \
    --disable-restore-session-state \
    --disable-features=TranslateUI,Translate \
    --ignore-certificate-errors \
    --disable-notifications \
    --mute-audio \
    --disable-dev-shm-usage \
    --disable-background-networking \
    --disable-sync \
    --display="$DISPLAY" \
    "$ANVIL_URL"

echo ""
echo "Chromium exited with code: $?"
