# ⚒ Anvil

A self-hosted hash cracking management platform built on Python, FastAPI, and Hashcat.

Anvil provides a multi-user web dashboard for managing hash cracking jobs, customers, wordlists, and cracking agents — with role-based access control that keeps sensitive credential data away from presentation-mode accounts.

---

## Architecture

```
┌─────────────────────────────────────────────┐
│  Browser clients (HTTPS)                    │
│  Admin · Analyst · Viewer · Presentation    │
└────────────────┬────────────────────────────┘
                 │ HTTPS (TLS 1.2+)
┌────────────────▼────────────────────────────┐
│  Anvil Server  (FastAPI + Uvicorn)          │
│  Auth · Jobs · Customers · Wordlists        │
│  REST API · WebSocket · Export · Audit      │
│  SQLAlchemy ORM → SQLite / PostgreSQL       │
└────────────────┬────────────────────────────┘
                 │ HTTPS REST + WSS
┌────────────────▼────────────────────────────┐
│  Anvil Agent  (Python systemd service)      │
│  Job poller · Hashcat wrapper               │
│  HW monitor · WebSocket live stream         │
└────────────────┬────────────────────────────┘
                 │ subprocess (no shell=True)
┌────────────────▼────────────────────────────┐
│  Hashcat binary  (GPU / CPU)                │
└─────────────────────────────────────────────┘
```

---

## Requirements

### Server (online install)
- Python 3.11, 3.12, or 3.13
- Ubuntu 22.04 LTS / Debian 12 (Bookworm) / Debian Trixie
- 1 GB RAM minimum (4 GB recommended)
- Port 443 (or configure a different port)

### Server (offline / airgapped install)
- **Ubuntu 24.04 LTS (Noble), x86_64** — the offline bundle is built specifically against this target's glibc and Python ABI
- Python 3.12 (Ubuntu 24.04 default)
- A second, internet-connected Ubuntu 24.04 host to build the bundle on

> **Python 3.13 note:** The setup script passes `--prefer-binary` to pip, which downloads
> pre-built wheels instead of compiling Rust extensions from source. This avoids the
> `pydantic-core` / PyO3 build failure that occurs when Rust toolchain versions lag
> behind a new Python release.

### Agent
- Python 3.11+
- Hashcat 6.2.6+ installed and in `$PATH`
- NVIDIA GPU (optional but recommended) — CUDA drivers installed
- AMD GPU supported via OpenCL

---

## Quick Start

### 1. Install the server

```bash
git clone <repo> anvil
cd anvil/server
sudo bash setup.sh
```

The setup script:
- Creates the `anvil` service user
- Generates a 4096-bit self-signed TLS certificate
- Creates a random 64-byte secret key in `config.toml`
- Generates a provisioning key for zero-touch agent registration
- Initialises the database and seeds the default admin user
- Installs and starts the `anvil-server` systemd service

**Default credentials:** `admin` / `ChangeMe123!`  
You will be forced to change this on first login.

### 2. Install an agent (zero-touch)

On any machine with Hashcat installed, run a single command:

```bash
curl -sfk https://<server-ip>/agents/install | sudo bash
```

This script is served directly by the Anvil server. It:
- Downloads the agent package
- Installs it to `/opt/anvil/agent/`
- Copies the server's TLS certificate
- Self-registers the agent with the server using the provisioning key
- Starts the `anvil-agent` systemd service

The agent appears in **Admin → Agents** immediately after installation — no manual token entry required.

> **Restrict agent installs:** In **Settings → Install Access Control**, add IP/CIDR ranges
> to limit which machines can download and run the install script.

### 3. Create a job

1. Upload a wordlist under **Wordlists**
2. Go to **Jobs → New job**
3. Select a **customer** — required for every job (used to scope per-customer hash deletion)
4. Upload one or more hash list files
5. Select the hash type (auto-detected via `hashcat --identify`)
6. Choose attack mode, wordlist, rules, and mask
7. Select an online agent and click **Launch job**

---

## Offline / airgapped install

For environments with no internet access on the server, Anvil ships a two-script flow:
build a bundle on a connected machine, copy it across, run the installer.

> **Hard requirement:** both the build host **and** the target server must be
> **Ubuntu 24.04 LTS (Noble), x86_64**. The bundle pins to `python3.12` /
> `cp312` wheels and Noble's `.deb` set — running it on a different Ubuntu
> release or another distro will fail at install time. If you need a different
> target, edit the `TARGET_*` variables at the top of `package-offline.sh`.

### 1. Build the bundle (on a connected Ubuntu 24.04 host)

```bash
git clone <repo> anvil
cd anvil/server
bash package-offline.sh
```

`package-offline.sh`:
- Verifies the build host is Ubuntu 24.04 with `python3.12` available
- Resolves the full transitive dependency closure for the required apt packages
  (`libmagic1`, `libssl-dev`, `openssl`, `python3.12-venv`, `build-essential`,
  `libcap2-bin`, `rsync`, …) and downloads each `.deb` into `offline/debs/`
- Downloads every Python wheel listed in `requirements.txt` (plus `pip`,
  `setuptools`, `wheel`) into `offline/wheels/`, restricted to `cp312` /
  `manylinux` ABI tags so they install cleanly on the airgapped target
- Writes `offline/MANIFEST.txt` with the build host details, target, and counts

### 2. Transfer to the airgapped server

SCP (or otherwise move) the **entire `server/` directory** — including the
freshly built `offline/` folder inside it — to the target host.

```bash
scp -r anvil/server/ user@airgapped-host:/tmp/anvil-server/
```

### 3. Install on the airgapped server

```bash
cd /tmp/anvil-server
sudo bash setup-offline.sh
```

`setup-offline.sh` does everything `setup.sh` does, but without ever touching
the network:
- Verifies `offline/debs/` and `offline/wheels/` are populated
- Installs system packages from the local `.deb` set via `apt-get install --no-download`
- Creates the `anvil` service user and `/opt/anvil/server/` directory layout
- Stages the agent source under `agent-dist/` for in-dashboard download
- Creates the venv and installs Python deps with `pip install --no-index --find-links=offline/wheels/`
- Prompts for the server hostname/IP, generates a 4096-bit self-signed TLS
  certificate with the correct SANs, and writes the SAN into `config.toml`
- Generates the JWT secret and agent provisioning key
- Initialises the database, seeds the default admin and built-in rule files
- Installs and starts the `anvil-server` systemd service

The default credentials and post-install steps are identical to the online
install.

---

## Configuration

### Server — `server/config.toml`

| Key | Default | Description |
|---|---|---|
| `server.secret_key` | *(generated)* | JWT signing key — **must be changed in production** |
| `server.port` | `443` | HTTPS listening port |
| `server.session_max_age` | `28800` | Session cookie lifetime (seconds) |
| `server.kiosk_allowlist` | `[]` | IP/CIDR list for kiosk (presentation bypass) mode |
| `database.url` | `sqlite+aiosqlite:///./anvil.db` | SQLAlchemy database URL |
| `tls.mode` | `self_signed` | `self_signed` or `provided` |
| `tls.cert_file` | `./certs/anvil.crt` | Path to TLS certificate |
| `tls.key_file` | `./certs/anvil.key` | Path to TLS private key |
| `tls.extra_sans` | `[]` | Extra hostnames/IPs added to self-signed cert SAN |
| `agent.provisioning_key` | *(generated)* | Shared key embedded in the install script |
| `agent.install_allowlist` | `[]` | IP/CIDR allowlist for the install script endpoint |
| `agent.heartbeat_timeout` | `60` | Seconds before an agent is marked offline |
| `storage.wordlists_dir` | `./data/wordlists` | Wordlist storage path |
| `security.bcrypt_rounds` | `12` | bcrypt work factor (minimum 10) |
| `notifications.enabled` | `false` | Enable SMTP job-completion notifications |

### Agent — `agent/config.toml`

The agent's `config.toml` is written automatically by the install script. Manual overrides:

| Key | Description |
|---|---|
| `agent.name` | Display name shown in the dashboard |
| `agent.server_url` | Anvil server base URL |
| `agent.verify_tls` | Verify server TLS certificate |
| `agent.ca_bundle` | Path to server CA cert (for self-signed setups) |

### Switching to PostgreSQL

```toml
[database]
url = "postgresql+asyncpg://anvil:password@localhost/anvil"
```

Install the async driver:
```bash
pip install asyncpg
```

### Using a real TLS certificate (e.g. Let's Encrypt)

```toml
[tls]
mode      = "provided"
cert_file = "/etc/letsencrypt/live/example.com/fullchain.pem"
key_file  = "/etc/letsencrypt/live/example.com/privkey.pem"
```

Restart the server after updating:
```bash
sudo systemctl restart anvil-server
```

### Adding hostnames to the self-signed cert

If agents or browsers connect by hostname rather than IP, add the hostname to the cert SAN via **Settings → TLS Extra SANs** in the UI, or directly in `config.toml`:

```toml
[tls]
extra_sans = ["anvil.internal", "192.168.1.50"]
```

The server regenerates the certificate on restart.

---

## User roles

| Role | Create jobs | View credentials | View dashboard | Upload limit | Notes |
|---|---|---|---|---|---|
| `admin` | ✅ | ✅ | ✅ | Unlimited | Full access, user and agent management |
| `analyst` | ✅ | ✅ | ✅ | 30 GB | Create jobs, view all results |
| `viewer` | ❌ | ✅ | ✅ | — | Read-only, can see cracked passwords |
| `presentation` | ❌ | ❌ | ✅ | — | Aggregate stats only — no usernames or passwords; customer real name replaced with presentation alias |

---

## Creating a cracking job

1. Upload a wordlist under **Wordlists** (`.txt`, `.lst`, `.dict`, `.wl`)
2. Optionally upload rule files (`.rule`, `.rules`) under **Wordlists → Upload rule file**
3. Go to **Jobs → New job**
4. Pick a **customer** — required. Every job must be scoped to a customer so the per-customer "Delete hashes" action has a clear blast radius. The form blocks submission without one.
5. Upload one or more hash list files — click **+ Add another hash file** to attach additional files; duplicate hashes across files are automatically deduplicated
6. Select hash type — Anvil auto-detects via `hashcat --identify` with manual override
7. Choose attack mode, wordlist, rules, and mask as needed
8. Select an online agent (pre-selected automatically if only one is available) and click **Launch job**

Live progress (H/s, %, ETA, GPU temp) streams to the job detail page via WebSocket.

### Deleting a customer's hashes

The customers list has a per-row **Delete hashes** action that purges every
`HashList` (and cascaded `Hash` row) for every job belonging to that customer,
and unlinks the underlying hash files from disk when no surviving hash list
still references them. Job rows themselves are kept, and `total_hashes` /
`cracked_count` are preserved so the **Accounts Cracked** dashboard stat and
historical crack-rate remain accurate after a purge. The action is recorded
in the audit log as `customer_hashes_deleted`.

---

## Dashboard

The dashboard shows live KPI tiles, running jobs, and agent status.

**KPI tiles:**
- **Total Customers** — total customer records in the database
- **Total Hashes** — sum of unique hashes across all jobs
- **Accounts Cracked** — total cracked across all completed jobs
- **Running Jobs** — currently active jobs with live hash rates
- **Online Agents** — count of agents that have reported a heartbeat recently

The dashboard **auto-refreshes** every 10 seconds for live data (hash rates, agent status, crack counts) and reloads fully every 60 minutes. Agent and job list pages reload every 30 seconds.

### Kiosk / Presentation mode

Kiosk mode lets you display the dashboard on a screen (TV, projector) without requiring a login, and without exposing any credential data.

**Setup:**
1. Go to **Settings → Kiosk Mode Allowlist**
2. Add the IP address (or CIDR range) of the display device — e.g. `192.168.1.50` or `10.0.0.0/24`
3. Click **Save kiosk list**

Any browser connecting from an allowlisted IP is automatically placed into presentation mode:
- Sidebar navigation is hidden (full-width layout)
- Page title shows **Anvil Dashboard**
- KPI tiles show aggregate counts only — no usernames, passwords, or customer details
- Dashboard polls for live updates every 10 seconds

---

## Agents

The **Agents** page shows all registered agents with:
- Online/offline status indicator
- Hostname
- IP address (admin-only)
- Compute details — GPU model(s), VRAM, and CPU core count

The provisioning key can be rotated at any time via **Settings → Agent Provisioning Key → Rotate key**. Rotating the key prevents new registrations but does not affect existing agents.

---

## Attack templates

Create reusable attack profiles under **Templates**. Common examples:

| Template name | Mode | Hash type | Notes |
|---|---|---|---|
| Quick NTLM dictionary | Dictionary (0) | 1000 NTLM | rockyou + best64 rules |
| Full NTLM hybrid | Hybrid WL+Mask (6) | 1000 NTLM | wordlist + `?d?d?d?d` |
| WPA fast | Dictionary (0) | 22000 WPA-PBKDF2 | large wifi wordlist |
| Linux shadow bruteforce | Mask (3) | 1800 sha512crypt | `?l?l?l?l?l?l?d?d` |

Templates prefill the new job form when selected.

---

## Exports

- **CSV export** — full hash:user:plaintext table (Admin/Analyst/Viewer only)
- **PDF export** — summary report with crack statistics (all roles); credential table appended for credentialed roles only
- Presentation role exports produce summary PDFs with no credential data

---

## Audit log

All significant actions are recorded:

- Login success / failure
- Job created / cancelled / re-run
- User created / role changed / password reset
- Agent registered / deactivated
- Wordlist / rule uploaded or deleted
- Exports (CSV and PDF)
- Settings changes (provisioning key rotation, allowlist updates)

Available at **Admin → Audit Log** with filtering by action type.

---

## Notifications

Configure SMTP in `config.toml` to receive email on job completion:

```toml
[notifications]
enabled       = true
smtp_host     = "smtp.example.com"
smtp_port     = 587
smtp_user     = "anvil@example.com"
smtp_password = "..."
smtp_from     = "anvil@example.com"
smtp_tls      = true
```

---

## Service management

```bash
# Server
sudo systemctl status  anvil-server
sudo systemctl restart anvil-server
sudo journalctl -u     anvil-server -f

# Agent
sudo systemctl status  anvil-agent
sudo systemctl restart anvil-agent
sudo journalctl -u     anvil-agent -f
```

---

## Security notes

- All traffic is TLS-encrypted (minimum TLS 1.2)
- Session cookies are `HttpOnly; Secure; SameSite=strict`
- Agent API tokens are stored as `SHA-256` hashes — never in plaintext
- Hashcat is invoked via `asyncio.create_subprocess_exec` — no shell injection possible
- Dangerous hashcat flags (`--outfile`, `--potfile-path`, `--session`, etc.) are blocked in the job creation form
- File uploads are stream-validated with size limits, extension allowlists, filename sanitisation, and resolved-path confinement checks (30 GB limit for non-admin; no limit for admin)
- All database queries use SQLAlchemy ORM parameterised statements
- bcrypt rounds configurable (default 12, minimum enforced at 10)
- Presentation role never receives plaintext passwords or usernames — enforced at query layer and in exports
- Kiosk mode access is IP-gated; kiosk sessions have the same data restrictions as the presentation role
- Audit log records all user actions including failed logins
- HTTP security headers: HSTS, CSP, X-Frame-Options: DENY, X-Content-Type-Options, Referrer-Policy
- Agent install script endpoint can be restricted to specific IP/CIDR ranges
- Agent-side residual data: per-job hash list files and the hashcat outfile (cracked plaintexts) live under `workdir/jobs/job_<id>/` and are wiped at end-of-run (completed, failed, **and** cancelled). Cached wordlists and rules persist across jobs by design — wipe `/var/lib/anvil-agent/workdir` before redeploying or disposing of an agent host. See [docs/architecture-and-security.md](docs/architecture-and-security.md) §13 for the full lifecycle matrix.

---

## Project structure

```
anvil/
├── server/
│   ├── anvil_server/
│   │   ├── main.py              # FastAPI app, middleware, lifespan
│   │   ├── config.py            # Pydantic settings loader
│   │   ├── database.py          # Async SQLAlchemy engine + init
│   │   ├── hashcat_modes.py     # 350+ hash types + auto-identification
│   │   ├── models/              # SQLAlchemy ORM models
│   │   ├── routers/             # FastAPI route handlers
│   │   │   └── api/             # Agent REST API
│   │   ├── services/            # Business logic
│   │   └── templates/           # Jinja2/Alpine.js UI
│   ├── config.toml
│   ├── requirements.txt
│   ├── setup.sh                # Online installer
│   ├── package-offline.sh      # Build offline bundle (Ubuntu 24.04 build host)
│   ├── setup-offline.sh        # Airgapped installer (Ubuntu 24.04 target)
│   └── offline/                # .deb + .whl bundle, produced by package-offline.sh
└── agent/
    ├── anvil_agent/
    │   ├── main.py              # Entry point, poll + heartbeat loops
    │   ├── config.py            # Agent settings loader
    │   ├── hashcat_wrapper.py   # Subprocess wrapper, progress parser
    │   ├── hardware_monitor.py  # GPU/CPU metrics (GPUtil / nvidia-smi)
    │   ├── job_runner.py        # Single job lifecycle orchestration
    │   └── server_client.py     # REST + WebSocket client
    ├── config.toml
    ├── requirements.txt
    └── setup.sh
```

---

## Roadmap / future work

- [ ] Certbot / ACME integration for automatic certificate renewal
- [ ] Per-job webhook URL configuration in the UI
- [ ] Distributed agents with load balancing
- [ ] Hash potfile deduplication across jobs
- [ ] LDAP / SSO authentication backend
- [ ] REST API for external integrations
