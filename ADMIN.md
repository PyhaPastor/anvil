# Anvil — Administration & Troubleshooting Guide

## Table of Contents

1. [Directory Layout](#directory-layout)
2. [Log Files](#log-files)
3. [Configuration Files](#configuration-files)
4. [Database](#database)
5. [Common Admin Tasks](#common-admin-tasks)
6. [Troubleshooting](#troubleshooting)
7. [Customisation](#customisation)

---

## Directory Layout

```
/opt/anvil/
├── server/
│   ├── anvil.db                   # SQLite database (all persistent data)
│   ├── logs/
│   │   └── server.log             # Server log (rotating, see config)
│   ├── data/
│   │   ├── wordlists/             # Uploaded wordlist files
│   │   ├── rules/                 # Rule files (built-in + uploaded)
│   │   └── hashlists/             # Per-job hash list uploads
│   ├── anvil_server/              # Python package
│   └── config.toml                # Server configuration
│
└── agent/
    ├── logs/
    │   └── agent.log              # Agent log (rotating)
    ├── workdir/                   # Hashcat working directory
    │   ├── cache/
    │   │   ├── wordlists/         # Cached wordlists (persistent across reboots)
    │   │   └── rules/             # Cached rule files
    │   └── jobs/                  # Per-job temp files (cleaned after each job)
    └── config.toml                # Agent configuration
```

---

## Log Files

### Server log

**Location:** `/opt/anvil/server/logs/server.log`  
**Rotation:** 10 MB per file, 5 backups (configurable in `config.toml`)

Configured in `server/config.toml`:
```toml
[logging]
level = "INFO"          # DEBUG / INFO / WARNING / ERROR
file  = "logs/server.log"
max_bytes   = 10485760  # 10 MB
backup_count = 5
```

To follow the live log:
```bash
tail -f /opt/anvil/server/logs/server.log
```

To increase verbosity temporarily (without restart), you can edit the log level and restart, or set `level = "DEBUG"` to see all SQL queries, WS frames, and request details.

### Agent log

**Location:** `/opt/anvil/agent/logs/agent.log`  
**Rotation:** same rotating handler, configurable in `agent/config.toml`

```bash
tail -f /opt/anvil/agent/logs/agent.log
```

Key agent log prefixes:
- `anvil.agent` — startup, job pickup, shutdown
- `anvil.agent.client` — HTTP / WS communication with server
- `anvil.agent.runner` — file staging, hashcat invocation, result submission
- `anvil.agent.hw` — hardware monitor (GPU/CPU sampling)

### systemd journal (if installed as a service)

```bash
# Server
journalctl -u anvil-server -f

# Agent
journalctl -u anvil-agent -f
```

---

## Configuration Files

### Server — `/opt/anvil/server/config.toml`

```toml
[server]
host = "0.0.0.0"
port = 8000
secret_key = "..."          # JWT signing key — keep secret, changing invalidates all sessions
kiosk_allowlist = []        # IPs that get auto presentation-mode (no login)

[agent]
provisioning_key = "..."    # Shared secret for zero-touch agent registration

[storage]
wordlists_dir = "data/wordlists"
rules_dir     = "data/rules"
hashlists_dir = "data/hashlists"

[tls]
cert_file  = "certs/server.crt"
key_file   = "certs/server.key"
extra_sans = []             # Additional SANs for the self-signed cert

[logging]
level        = "INFO"
file         = "logs/server.log"
max_bytes    = 10485760
backup_count = 5
```

Restart required after config changes:
```bash
systemctl restart anvil-server
```

### Agent — `/opt/anvil/agent/config.toml`

```toml
[agent]
name         = "cracker-01"          # Display name in the UI
server_url   = "https://10.0.0.1"   # Server base URL (no trailing slash)
api_token    = "eyJ..."              # Bearer token — from server registration
verify_tls   = false                 # Set true + ca_bundle if using a trusted cert
ca_bundle    = ""                    # Path to custom CA cert for TLS verification
poll_interval = 5                    # Seconds between job polls

[hashcat]
bin     = "hashcat"                  # Path to hashcat binary
workdir = "/opt/anvil/agent/workdir" # Must be on a persistent, fast disk

[logging]
level        = "INFO"
file         = "logs/agent.log"
max_bytes    = 10485760
backup_count = 5

[hardware]
sample_interval = 2                  # GPU/CPU sampling interval in seconds
```

**Important:** `workdir` is where wordlist cache and hash files are stored. Ensure this path is on a disk with sufficient space and is NOT a tmpfs/ramdisk (data would be lost on reboot). Default after setup: `/opt/anvil/agent/workdir`.

---

## Database

Anvil uses **SQLite** stored at `/opt/anvil/server/anvil.db`.

### Backup

```bash
# Safe online backup (no server stop needed — SQLite WAL mode)
sqlite3 /opt/anvil/server/anvil.db ".backup /opt/anvil/backups/anvil-$(date +%Y%m%d).db"
```

### Schema migrations

Anvil does not use Alembic. New columns are added by the install/upgrade script. If you see `OperationalError: no such column`, run the relevant ALTER TABLE manually:

```bash
sqlite3 /opt/anvil/server/anvil.db "ALTER TABLE agents ADD COLUMN wordlist_cache JSON"
```

### Useful queries

```bash
# Open interactive shell
sqlite3 /opt/anvil/server/anvil.db

# List all agents
SELECT id, name, is_active, last_seen FROM agents;

# List recent jobs
SELECT id, name, status, progress_pct, cracked_count FROM jobs ORDER BY created_at DESC LIMIT 20;

# Check crack rate for a specific job
SELECT j.name, j.cracked_count, j.total_hashes,
       ROUND(j.cracked_count * 100.0 / j.total_hashes, 1) || '%' AS rate
FROM jobs j WHERE j.id = <JOB_ID>;

# List users
SELECT id, username, role, is_active FROM users;
```

---

## Common Admin Tasks

### Reset a user password

```bash
cd /opt/anvil/server
python3 - <<'EOF'
import asyncio
from anvil_server.database import AsyncSessionLocal
from anvil_server.models.user import User
from anvil_server.services.auth_service import hash_password
from sqlalchemy import select

async def reset():
    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
        u.password_hash = hash_password("newpassword123")
        await db.commit()
        print("Password updated.")

asyncio.run(reset())
EOF
```

### Re-register an agent (rotate its token)

Run the install script again on the agent machine — it detects the existing hostname and rotates the token without creating a duplicate record:
```bash
curl -sfk https://<server>/agents/install | sudo bash
```

Or manually via the UI: **Agents → Deactivate** the old record, then re-run the install script.

### Free disk space on agent (wordlist cache)

Via the UI: **Agents → Wordlist cache** button → delete individual cached files.

Manually on the agent:
```bash
ls -lh /opt/anvil/agent/workdir/cache/wordlists/
rm /opt/anvil/agent/workdir/cache/wordlists/<filename>
```

### Restart services

```bash
systemctl restart anvil-server
systemctl restart anvil-agent

# Check status
systemctl status anvil-server
systemctl status anvil-agent
```

---

## Troubleshooting

### Agent shows "Offline" in dashboard

1. Check agent service: `systemctl status anvil-agent`
2. Check agent can reach server: `curl -k https://<server>/api/v1/agent/health -H "Authorization: Bearer <token>"`
3. Check agent log for connection errors: `tail -50 /opt/anvil/agent/logs/agent.log`
4. Verify `server_url` in agent `config.toml` is correct (no trailing slash)
5. If using TLS, check `verify_tls` and `ca_bundle` settings

### Job stuck in "Queued" forever

1. Check the assigned agent is online (Dashboard → Agent Status)
2. Check the agent log — look for `Poll error` or download failures
3. Verify the agent's API token is valid: `curl -k https://<server>/api/v1/agent/health -H "Authorization: Bearer <token>"`
4. Check there's no conflicting job already running on the agent (agent only runs one job at a time)

### "Internal Server Error" on any page

1. Check server log immediately: `tail -100 /opt/anvil/server/logs/server.log`
2. Look for `ERROR` or `CRITICAL` lines with a Python traceback
3. Common causes:
   - Missing DB column → run the relevant `ALTER TABLE` (see Database section)
   - Missing file on disk (wordlist/hashlist deleted manually) → job will fail on next run
   - Corrupted `config.toml` → check with `python3 -c "import tomllib; tomllib.load(open('config.toml','rb'))"`

### Hashcat crashes immediately

Check the agent log for the full hashcat command line and error output:
```bash
grep -A 10 "hashcat.*error\|Error\|ATTENTION" /opt/anvil/agent/logs/agent.log
```

Common causes:
- Hash type mismatch — use "Auto-detect" or verify manually with `hashcat --identify`
- No OpenCL/CUDA runtime → install GPU drivers and runtime
- Wordlist file missing from cache → delete the cache entry and let the agent re-download

### TLS certificate errors

If browsers show certificate warnings or agents can't connect:
```bash
# Check certificate SANs
openssl x509 -in /opt/anvil/server/certs/server.crt -noout -text | grep -A 5 "Subject Alternative"

# Regenerate cert with additional SANs
# Edit config.toml: extra_sans = ["192.168.1.50", "cracker.local"]
# Then re-run setup.sh (regenerates cert only, keeps database)
```

### config.toml appears corrupted (non-ASCII characters)

This can happen if `setup.sh` was run multiple times and the sed substitution was applied twice. Fix:
```bash
python3 - <<'EOF'
import re, pathlib
p = pathlib.Path("config.toml")
text = p.read_bytes().decode("utf-8", errors="replace")
# Remove any non-ASCII bytes that snuck in
text = re.sub(r'[^\x00-\x7F]+', '', text)
p.write_text(text)
print("Fixed.")
EOF
```

---

## Customisation

### Kiosk / presentation mode

Any IP in `kiosk_allowlist` gets automatic presentation mode (no login, no credentials shown). Useful for a TV dashboard in the office:

```toml
[server]
kiosk_allowlist = ["192.168.1.100", "192.168.1.0/24"]
```

### Adding built-in rule files

Built-in rules live in `server/data/rules/`. Any `.rule` file placed there and seeded into the database (via `setup.sh` or manually) appears in the job creation form.

To add a custom rule without re-running setup:
```bash
cp my_rules.rule /opt/anvil/server/data/rules/

python3 - <<'EOF'
import asyncio
from anvil_server.database import AsyncSessionLocal
from anvil_server.models.wordlist import Rule
import anvil_server.models  # ensure FK resolution

async def seed():
    async with AsyncSessionLocal() as db:
        r = Rule(name="my-rules", description="Custom ruleset",
                 file_path="/opt/anvil/server/data/rules/my_rules.rule")
        db.add(r)
        await db.commit()
        print("Rule seeded.")

asyncio.run(seed())
EOF
```

### Changing the theme / colours

All UI colours and button styles are defined in `server/anvil_server/templates/base.html` inside the `<style>` block (lines ~60–90). CSS variables control the dark theme:

```css
:root {
  --bg-primary:    #09090b;   /* page background */
  --bg-secondary:  #18181b;   /* card / surface background */
  --border:        #27272a;   /* card borders */
  --text-primary:  #f4f4f5;   /* main text */
  --text-dim:      #a1a1aa;   /* secondary text */
  --text-dimmer:   #71717a;   /* tertiary / labels */
  --nav-hover:     #1c1c1f;
}
```

Button styles (`.btn-create`, `.btn-destruct`, `.btn-warn`, etc.) are also in `base.html` and follow the same RGBA tint pattern — easy to recolour without touching Tailwind config.

### Adjusting agent poll / heartbeat intervals

In `agent/config.toml`:
```toml
[agent]
poll_interval = 5       # How often agent checks for new jobs (seconds)

[hardware]
sample_interval = 2     # GPU/CPU sampling frequency (seconds)
                        # Heartbeat is sent every sample_interval × 5 seconds
```

Lower values give more responsive live metrics but increase server load. For production, `poll_interval = 5` and `sample_interval = 2` (heartbeat every ~10s) is a good balance.

### Importing large wordlists

Use the import script rather than the web UI for files over a few GB:
```bash
cd /opt/anvil/server
python3 scripts/import_wordlists.py /path/to/wordlist.txt --name "my-list" --category wifi
```

The script shows a live progress bar during line counting. Files are registered in the database and immediately available in the job creation form.
