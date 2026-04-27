# Anvil Kiosk

Turns an Ubuntu Desktop machine into a full-screen, unattended display for the Anvil dashboard.

---

## Requirements

- Ubuntu Desktop (22.04 LTS or later)
- Network access to the Anvil server
- Machine with BIOS scheduled power-on support (for timed startup)

---

## Installation

Run once as root on the kiosk machine:

```bash
sudo bash setup-kiosk.sh
```

The script will prompt for:

| Prompt | Example | Notes |
|---|---|---|
| Anvil server URL | `https://192.168.1.50` | Include `https://` |
| Shutdown time | `18:00` | 24 h, local time |
| Shutdown days | `1-5` | Cron field — see prompt for options |

After confirming, it installs packages, configures autologin, and sets up the shutdown schedule. Then:

```bash
sudo reboot
```

---

## Keyboard shortcuts

These work even while Chromium is in full kiosk mode — they are handled by Openbox at the window manager level, below the browser.

| Shortcut | Action |
|---|---|
| `Ctrl+Alt+Q` | Kill browser, open a terminal (watchdog still running — browser may relaunch) |
| `Ctrl+Alt+X` | **Hard stop** — kills watchdog + browser, drops to desktop permanently |
| `Ctrl+Alt+R` | Restart kiosk — kills and relaunches the browser |
| `Ctrl+Alt+Delete` | Immediate shutdown |

---

## Display manager prompt

During installation, Ubuntu may ask:

> **"Which display manager should be the default?"**
> Options: `lightdm` / `gdm3`

**Select `lightdm`.** The kiosk autologin is configured for LightDM. Choosing GDM3 will break the automatic login and the kiosk session will not start.

---

## Startup / shutdown

| Event | How it works |
|---|---|
| Power on | BIOS scheduled power-on (configure in BIOS/UEFI) |
| Auto login | LightDM logs in as `kiosk` user automatically |
| Browser launch | Openbox autostart fires `kiosk-launch.sh` |
| Auto shutdown | Cron job at the time/days you specified during setup |

---

## Changing settings after install

**Anvil server URL, connection type, WiFi details:**
```bash
sudo nano /opt/kiosk/kiosk.conf
```
Changes take effect on the next kiosk restart (reboot or `Ctrl+Alt+R`).

**Shutdown time/days:**
```bash
sudo nano /etc/cron.d/anvil-kiosk-shutdown
```

---

## How the watchdog works

A background process checks every 60 seconds. On two consecutive failures it does a full **browser kill + restart** back to the Anvil URL (not just F5 — a redirect to `/login` would survive a refresh):

- Server unreachable (network down, server offline) → HTTP 000
- Non-200 HTTP status (500, 404, etc.)
- HTTP 200 but JSON body — Anvil internal errors return JSON even when the page technically "loads"
- Final URL after redirects is not `/dashboard` — catches the `/login` redirect when the kiosk IP is not in the allowlist

If Chromium has crashed entirely, the watchdog restarts it immediately (no failure threshold needed).

---

## Power management

Disabled at three independent layers so the screen stays on from power-on to shutdown:

| Layer | Mechanism |
|---|---|
| Kernel | `systemctl mask sleep/suspend/hibernate` |
| X server | `xset s off`, `xset -dpms`, `xset s noblank` |
| GNOME/desktop | `gsettings` — idle delay = 0, screen lock off |

---

## Files deployed by the script

| Path | Purpose |
|---|---|
| `/opt/kiosk/kiosk.conf` | Runtime config — URL, connection type, WiFi. Edit to change settings |
| `/opt/kiosk/kiosk-launch.sh` | Main launcher — network wait, power settings, browser start |
| `/opt/kiosk/watchdog.sh` | Health check loop — kill+restart browser on error |
| `/opt/kiosk/no-connection.html` | Offline splash page shown when network is unavailable |
| `/etc/lightdm/lightdm.conf.d/50-kiosk.conf` | LightDM autologin config |
| `/etc/cron.d/anvil-kiosk-shutdown` | Scheduled shutdown cron job |
| `/usr/local/share/ca-certificates/anvil-server.crt` | Anvil TLS certificate (system trust store) |
| `~kiosk/.pki/nssdb/` | Chromium NSS certificate database |
| `~kiosk/.config/autostart/anvil-kiosk.desktop` | GNOME autostart entry |
| `~kiosk/.config/openbox/autostart` | Openbox autostart entry |
| `~kiosk/.config/openbox/rc.xml` | Openbox keybindings (Ctrl+Alt shortcuts) |
