# Anvil — Development Log

This document captures the full design and development history of the Anvil project,
generated from the original Claude session. Use it as context when continuing work in
any AI-assisted editor.

---

## Project brief

Build a self-hosted hash cracking management platform called **Anvil**, similar to
[Hashview](https://github.com/hashview/hashview), targeting Linux (Ubuntu Server /
Debian Trixie). The platform wraps Hashcat and provides a multi-user web dashboard
with role-based access control, live job progress, customer management, and
presentation-safe views that hide credentials from certain accounts.

---

## Architecture decisions

### Stack chosen

| Layer | Choice | Reason |
|---|---|---|
| Server framework | FastAPI + Uvicorn/Gunicorn | Async, fast, auto OpenAPI, lighter than Django |
| Frontend | Jinja2 + HTMX + Alpine.js | No JS build pipeline; server-rendered with lightweight reactivity |
| CSS | TailwindCSS (CDN) | No Node.js build step required |
| Database | SQLAlchemy ORM → SQLite (default) | Zero-ops default; swap to PostgreSQL via one config line |
| Auth | JWT (HS256) + bcrypt | Stateless tokens, industry standard |
| Agent comms | REST (job poll/results) + WebSocket (live progress) | WS for real-time H/s/temp streaming; REST for everything else |
| TLS | Self-signed cert (auto-generated) | Certbot/ACME hook point left in config for future upgrade |
| Deployment | systemd service + setup.sh | Standard Linux service management |

### Why not Django?
Too heavy for this use case. FastAPI gives us async native, automatic request validation
via Pydantic, and a clean separation between the browser UI routes and the agent API.

### Why not React/Vue?
Avoided a JS build pipeline entirely. HTMX handles partial page updates (e.g. live
progress bars). Alpine.js handles lightweight interactivity (dropdowns, modals, form
toggles). Everything is server-rendered Jinja2.

### Why SQLite as default?
Single-server deployment, zero ops overhead, trivially backed up with `cp`. The
SQLAlchemy ORM means switching to PostgreSQL for a multi-server future is a one-line
config change — no code changes needed.

---

## User roles

Four roles, enforced at the dependency injection layer in `auth_service.py`:

| Role | Create jobs | See passwords/usernames | See dashboard |
|---|---|---|---|
| `admin` | ✅ | ✅ | ✅ |
| `analyst` | ✅ | ✅ | ✅ |
| `viewer` | ❌ | ✅ | ✅ |
| `presentation` | ❌ | ❌ | ✅ (aggregate stats only) |

The presentation role was a key requirement — clients sit in a room watching a screen
showing crack percentages and counts, but never see actual passwords or usernames.
Customer real names are also substituted with a `presentation_name` field at query time.

---

## File-by-file reference

### Server — `server/anvil_server/`

#### `main.py`
FastAPI application factory (`create_app()`). Registers all middleware and routers.

Middleware stack (outermost first):
1. `SecurityHeadersMiddleware` — HSTS, CSP, X-Frame-Options, X-Content-Type-Options
2. `TimingMiddleware` — adds `X-Process-Time-Ms` header (useful for debugging)
3. `SessionMiddleware` — Starlette session with `SameSite=strict; Secure; HttpOnly`
4. SlowAPI rate limiter

Lifespan startup: generates TLS cert if missing, runs `init_db()`.

OpenAPI docs (`/api/docs`) only exposed when `debug=true` in config.

#### `config.py`
Pydantic v2 settings loaded from `config.toml`. Key validators:
- `secret_key` — refuses to start with default value or anything under 32 chars
- `bcrypt_rounds` — minimum 10 enforced

#### `database.py`
Async SQLAlchemy engine + session factory. `init_db()` creates all tables and seeds
the default `admin` / `ChangeMe123!` user with `force_password_change=True`.

#### `hashcat_modes.py`
350+ hashcat hash types (as of hashcat 6.2.6), with:
- Full categorised list (`HASHCAT_MODES`) — all modes with category and name
- Favourites shortlist (`FAVOURITE_MODES`) — 15 most common types shown prominently in UI
- Regex auto-identification (`identify_hash_modes()`) — server-side heuristic fallback
  when `hashcat --identify` is not available

#### `models/`

| File | Model(s) | Notes |
|---|---|---|
| `user.py` | `User`, `UserRole` | Role enum, helper methods (`can_create_jobs()` etc.) |
| `customer.py` | `Customer` | Has both `name` and `presentation_name` fields |
| `job.py` | `Job`, `JobStatus`, `AttackMode` | Tracks progress, hashrate, ETA, cracked count |
| `hash_list.py` | `HashList`, `Hash` | Individual hash records; `plaintext` null until cracked |
| `agent.py` | `Agent`, `AgentHealth` | `is_online` property checks last heartbeat age |
| `wordlist.py` | `Wordlist`, `Rule` | File-backed library entries |
| `template.py` | `JobTemplate` | Saved attack profiles |
| `audit.py` | `AuditLog` | Immutable action log |

#### `services/`

| File | Purpose |
|---|---|
| `auth_service.py` | JWT encode/decode, bcrypt, FastAPI role dependencies, agent token auth |
| `audit_service.py` | Non-blocking audit writer (failures silently ignored to not block main flow) |
| `export_service.py` | CSV (injection-safe) and PDF (ReportLab) exports, role-gated |
| `notification_service.py` | SMTP email + webhook dispatch on job completion |
| `tls_service.py` | Self-signed cert generation via `cryptography` lib; auto-regenerates if expiring |
| `upload_service.py` | Streaming upload with path traversal protection, size limits, extension allowlist |
| `ws_manager.py` | Async WebSocket connection manager — browser job subscriptions + agent connections |

#### `routers/`

| File | Prefix | Auth required |
|---|---|---|
| `auth.py` | `/login`, `/logout`, `/change-password` | Public (login), session (change pw) |
| `dashboard.py` | `/dashboard` | Any role |
| `jobs.py` | `/jobs` | Viewer+ (read), Analyst+ (create) |
| `customers.py` | `/customers` | Analyst+ |
| `users.py` | `/users` | Admin only |
| `agents.py` | `/agents` | Admin only |
| `wordlists.py` | `/wordlists` | Analyst+ |
| `templates_router.py` | `/templates` | Analyst+ |
| `audit.py` | `/audit` | Admin only |
| `websocket.py` | `/ws/jobs/{id}`, `/ws/agent/{id}` | Cookie session / agent token |
| `api/agent_api.py` | `/api/v1/agent` | Agent Bearer token |

#### `templates/`
19 Jinja2 templates. All extend `base.html` (except login and error pages).
Dark theme throughout — Zinc colour palette from Tailwind.

Jinja2's HTML autoescaping is active by default. Values interpolated into
`<script>` blocks use the `| tojson` filter to prevent XSS.

---

### Agent — `agent/anvil_agent/`

#### `main.py`
Entry point. Runs two concurrent async loops:
- **Poll loop** — checks for queued jobs every `poll_interval` seconds; spawns a
  task to run the job without blocking the loop
- **Heartbeat loop** — POSTs GPU temp, utilisation, hashrate to server every
  `sample_interval * 5` seconds

Signal handlers for `SIGINT`/`SIGTERM` trigger graceful shutdown.

#### `hashcat_wrapper.py`
The most complex file in the agent. Key design decisions:
- Uses `asyncio.create_subprocess_exec` — **never `shell=True`** (prevents injection)
- Passes `--machine-readable` and `--status-json` to hashcat for structured output
- Streams stdout line-by-line, parses JSON status blobs into `ProgressEvent` dataclasses
- Auto-detects hash type via `hashcat --identify` before running if `hash_type` is None
- Strips environment to a safe minimal set before launching hashcat subprocess
- Results parsed from hashcat's `--outfile` after completion (not from stdout)
- `cancel()` sends `SIGTERM` to the hashcat process

#### `hardware_monitor.py`
Async background task. Supports two GPU backends:
- `gputil` — Python library, works with NVIDIA
- `nvidia-smi` — subprocess fallback, more reliable on some systems

Falls back gracefully to CPU-only metrics if no GPU detected.

#### `server_client.py`
- REST calls via `httpx.AsyncClient` with configurable TLS verification and CA bundle
- WebSocket client with automatic reconnect (exponential backoff, max 60s)
- Agent authenticates WS connection by sending `{"type":"auth","token":"..."}` as
  the first message — server validates before accepting the connection
- JWT `sub` claim decoded locally (stdlib base64) without re-verifying signature,
  since we only need the agent ID to construct the WS URL

#### `job_runner.py`
Orchestrates a single job:
1. Registers a cancel event with the WS client
2. Streams progress events from `HashcatWrapper.run()`
3. Updates hardware monitor context (so heartbeats include current job info)
4. Pushes live progress to server via WebSocket
5. On completion: submits full result payload via REST POST

---

## Security audit results (session 4)

### Fixed during development

| Vulnerability | Location | Fix |
|---|---|---|
| Path traversal | `upload_service.py` | Added `Path.resolve().relative_to()` confinement check |
| XSS in JS context | `templates/jobs/detail.html` | Switched to `\| tojson` filter for script-block interpolation |
| DB session passed as None | `routers/auth.py` login_page | Fixed to use `Depends(get_db)` properly |
| Agent used server-only `jose` lib | `agent/server_client.py` | Replaced with stdlib base64 JWT claim decode |
| File open without encoding | `routers/jobs.py` | Added `encoding="utf-8", errors="replace"` |

### Verified clean

- SQL injection — all queries use SQLAlchemy ORM parameterised statements
- Command injection — no `shell=True` anywhere; hashcat uses `create_subprocess_exec`
- `os.system` / `eval` / `exec` — none found
- JWT algorithm confusion — `decode()` pins `algorithms=["HS256"]`
- bcrypt rounds — minimum 10, validated at startup (default config uses 12)
- Secret key — server refuses to start with default or key < 32 chars
- Agent token storage — only `SHA-256(token)` stored, never plaintext
- CSRF — `SameSite=strict; Secure; HttpOnly` on all session cookies
- Clickjacking — `X-Frame-Options: DENY` + CSP `frame-ancestors 'none'`
- CSV injection — `_sanitise_cell()` prefixes dangerous leading characters
- Hashcat flag injection — `_validate_extra_flags()` blocks `--outfile`, `--potfile-path`, etc.
- Presentation role leakage — credential check enforced at query layer AND in every export

---

## Bugs fixed post-install

### Bug 1 — `status=226/NAMESPACE` (systemd sandboxing)
**Symptom:** Service immediately exits with `status=226/NAMESPACE`

**Cause:** `PrivateTmp=yes` and `ProtectSystem=strict` in the service file require Linux
kernel namespace support, which is not available in containers, LXC, or some VMs.

**Fix:** Removed those directives from the service file. The capability directives
(`AmbientCapabilities`, `CapabilityBoundingSet`) do not require namespaces and were kept.

**Affected file:** `/etc/systemd/system/anvil-server.service`

---

### Bug 2 — `[Errno 13] Permission denied` on port 443
**Symptom:** Gunicorn starts but immediately fails to bind port 443

**Cause:** When we removed the namespace directives we also removed
`AmbientCapabilities=CAP_NET_BIND_SERVICE`, which is what allowed the `anvil`
non-root user to bind ports below 1024.

**Fix (immediate):**
```bash
sudo setcap 'cap_net_bind_service=+ep' $(readlink -f /opt/anvil/server/venv/bin/python3)
sudo systemctl restart anvil-server
```

**Fix (permanent — in service file):** Add back to `[Service]`:
```ini
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
```

`AmbientCapabilities` does not require namespace support — it is safe in containers.

**Affected file:** `/etc/systemd/system/anvil-server.service`, `server/setup.sh`

---

### Bug 3 — `pydantic-core` build failure on Python 3.13
**Symptom:** `pip install` fails building `pydantic-core` wheel with Rust/PyO3 error:
`the configured Python interpreter version (3.13) is newer than PyO3's maximum supported version (3.12)`

**Cause:** Pinned `pydantic-core==2.7.4` uses PyO3 0.21.2 which caps at Python 3.12.
On Python 3.13 pip falls back to building from source, which fails.

**Fix:** Changed all requirements from exact pins (`==`) to minimum-version pins (`>=`).
`pydantic>=2.9.0` resolves to pydantic-core 2.23+ which ships pre-built Python 3.13 wheels.

Added `--prefer-binary` flag to all `pip install` calls in `setup.sh` — this tells pip
to always prefer a pre-built wheel over a source distribution, eliminating the entire
class of Rust compilation failures.

**Affected files:** `server/requirements.txt`, `agent/requirements.txt`, `server/setup.sh`, `agent/setup.sh`

---

## Bugs fixed — session 2

### Bug 4 — Starlette `TemplateResponse` argument order changed (Starlette ≥ 0.36)
**Symptom:** `{"detail":"Internal server error"}` on every page load; error log shows
`TypeError: unhashable type: 'dict'` inside Jinja2's template cache.

**Cause:** All 20 `TemplateResponse()` calls used the old positional argument order
`(name, context, status_code=...)`. In Starlette 0.36+ the first argument is `request`,
not `name`, so the context dict was landing in the `name` slot and Jinja2 tried to hash it.

**Fix:** Updated every call across 9 files to the new form `(request, name, context)`.
The `"request"` key was also removed from the context dicts — Starlette now injects it
automatically.

**Affected files:** `main.py`, all 8 router files.

---

### Bug 5 — `passlib` incompatible with `bcrypt >= 4.0.0`
**Symptom:** `ValueError: password cannot be longer than 72 bytes` on login; error
originates inside passlib's internal `detect_wrap_bug()` test.

**Cause:** passlib is unmaintained. The `bcrypt` 4.0 library now raises `ValueError`
when a password ≥ 72 bytes is passed (previously it silently truncated). passlib's
internal compatibility test hits this limit and crashes before any user password is
even checked.

**Fix:** Removed `passlib[bcrypt]` from `requirements.txt`. Replaced with `bcrypt>=4.0.0`
and updated `auth_service.py` to call `bcrypt.hashpw()` / `bcrypt.checkpw()` directly.
Existing password hashes in the DB are standard bcrypt format and verify correctly.

**Affected files:** `server/requirements.txt`, `server/anvil_server/services/auth_service.py`.

---

### Bug 6 — Flask `get_flashed_messages()` call in `base.html`
**Symptom:** `UndefinedError: 'get_flashed_messages' is undefined` on every page render.

**Cause:** `base.html` contained a `{% with messages = get_flashed_messages(...) %}`
block — a Flask-only Jinja2 global that does not exist in Starlette's Jinja2 environment.
No `flash()` calls exist anywhere in the Python code, so the block was dead code.

**Fix:** Removed the block entirely.

**Affected file:** `server/anvil_server/templates/base.html`.

---

### Bug 7 — `setup.sh` rsync overwrites live `config.toml` on re-runs
**Symptom:** Re-running `setup.sh` on a live server would replace the production-generated
secret key with the dev key from the repo's `config.toml`, invalidating all active
sessions.

**Fix:**
- Added `--exclude='config.toml'` to the `rsync` call.
- `config.toml` is now only copied from the repo on first install (when it doesn't exist
  at the destination).
- The secret-key generation grep was updated to detect both the original placeholder
  and the dev key, and `sed` now replaces the entire `secret_key = "..."` line.

**Affected file:** `server/setup.sh`.

---

### Bug 8 — Missing `import base64` in agent `server_client.py`
**Symptom:** Agent WebSocket connects then immediately crashes with
`NameError: name 'base64' is not defined`.

**Cause:** A prior cleanup removed `import base64, json` from inside `_connect_once()`
(where they were redundant), but `base64` was never present at module level. `json` was
already imported at the top of the file; `base64` was not.

**Fix:** Added `import base64` to the module-level imports.

**Affected file:** `agent/anvil_agent/server_client.py`.

---

## Bugs fixed — session 3

### Bug 9 — `rsync: command not found` in `setup.sh`
**Symptom:** `setup.sh` aborts at the "Setting up directories" section with
`server/setup.sh: line 67: rsync: command not found` on Debian Trixie.

**Cause:** `rsync` is not installed by default on Debian Trixie minimal images. It was
not listed as a system package dependency in `setup.sh`.

**Fix:** Replaced the `rsync` call with a `find ... -exec cp -a` equivalent that
uses only POSIX-standard tools available on all Debian/Ubuntu installations.
The same excludes (`venv`, `*.db`, `*.db-shm`, `*.db-wal`) are applied via `find`
predicates. The `config.toml` exclude (Bug 7) was already present and is preserved.

**Affected file:** `server/setup.sh`

---

### Bug 10 — `passlib` + `bcrypt >= 4.0.0` incompatibility crashes `init_db`
**Symptom:** Database initialisation step in `setup.sh` crashes with:
```
(trapped) error reading bcrypt version
AttributeError: module 'bcrypt' has no attribute '__about__'
ValueError: password cannot be longer than 72 bytes, truncate manually if necessary
```
The error originates inside `passlib`'s internal `detect_wrap_bug()` test — the
admin seed password is never even reached.

**Cause:** `passlib` (last released 2020, effectively unmaintained) uses
`bcrypt.__about__.__version__` to detect the backend version. The `bcrypt` 4.0
library removed the `__about__` submodule entirely. When version detection fails,
passlib falls back to a compatibility test that calls `hashpw()` with a 72-byte
string — which `bcrypt` 4.0 now rejects with `ValueError` (previously it silently
truncated). This causes a hard crash before any user password is processed.

**Fix:** Removed `passlib[bcrypt]` from `requirements.txt`. Replaced with
`bcrypt>=4.0.0`. Updated `auth_service.py` to call `bcrypt.hashpw()` /
`bcrypt.checkpw()` directly, reading `settings.security.bcrypt_rounds` for the
salt generation. Existing password hashes in the DB are standard `$2b$` bcrypt
format and verify correctly against the new code.

**Affected files:** `server/requirements.txt`, `server/anvil_server/services/auth_service.py`

---

## Features added — session 5

### Zero-touch agent installation

The manual 7-step token registration flow was replaced with a self-contained install
script served by the server at `/agents/install`. On any machine:

```bash
curl -sfk https://<server>/agents/install | sudo bash
```

The script downloads the agent package, installs it, copies the server TLS cert, reads
the `provisioning_key` from `config.toml`, self-registers with the server, and starts
`anvil-agent`. The agent appears in the dashboard immediately.

The provisioning key is rotated via **Settings → Agent Provisioning Key → Rotate key** in
the UI, or by editing `config.toml` directly. Rotating revokes future auto-registrations
but does not invalidate existing agent tokens.

Install script access can be restricted to specific IP/CIDR ranges via **Settings →
Install Access Control** (`agent.install_allowlist` in config).

---

### Multiple hash files per job

The new job form now supports attaching multiple hash list files in a single submission.
An **+ Add another hash file** button appends additional file inputs; individual files
can be removed before submission.

Server-side, all files are processed together. A `set[str]` accumulates unique hash values
across all files — duplicate hashes (same value in two files, or repeated within one file)
are discarded. `job.total_hashes` reflects the count of unique values only.

**Implementation:** Alpine.js `hashFileList()` component in `jobs/new.html`; `create_job`
endpoint in `routers/jobs.py` accepts `hash_files: List[UploadFile]`.

---

### Role-based upload limits

File uploads now enforce per-role size limits rather than a flat config value:

| Role | Limit |
|---|---|
| `admin` | 100 GB (effectively unlimited) |
| All other roles | 30 GB |

Applies to both wordlist/rule uploads (`wordlists.py`) and hash list uploads (`jobs.py`).
Constants: `_ADMIN_MAX_UPLOAD_BYTES = 100 * 1024**3`, `_NONADMIN_MAX_UPLOAD_BYTES = 30 * 1024**3`.

---

### Crack rate deduplication

Previously, `submit_job_result` in `agent_api.py` found only the first row matching a
`hash_value` and updated it. This produced inaccurate `cracked_count` when multiple
accounts shared the same password hash (e.g. two users with the same password).

**Fix:** The result processor now fetches **all** rows sharing a `hash_value`, marks
every matching row as cracked with the same plaintext, and counts unique hash values
(via `set[str]`) rather than row count.

`job.cracked_count` now correctly reflects the number of distinct password hashes cracked,
regardless of how many accounts share each hash.

---

### Zero-cracked job banner

When a job reaches `completed` status with `cracked_count == 0` and `total_hashes > 0`,
the job detail page shows an amber informational banner explaining no hashes were cracked.
This prevents confusion where the page appeared to succeed but showed no results.

**Template:** `jobs/detail.html`, conditional on `job.status.value == 'completed' and job.cracked_count == 0 and job.total_hashes > 0`.

---

### Agent IP address capture

The `/api/v1/agent/capabilities` endpoint now captures `request.client.host` and stores
it as `agent.ip_address`. The IP is displayed in the agents list below the hostname,
visible to admin users only.

---

### Agent auto-selection on new job form

When creating a new job, the agent dropdown pre-selects an agent automatically:
- If exactly one agent is online → that agent is pre-selected
- If two or more agents are online → the first registered agent (lowest `Agent.id`) is pre-selected, with a hint indicating multiple are available
- If no agents are online → a warning is shown; no pre-selection

---

### Kiosk / presentation mode

Kiosk mode allows a display device (TV, projector) to show the dashboard without a login
account, while enforcing the same data restrictions as the `presentation` role.

**Config:** `server.kiosk_allowlist` in `config.toml` — list of IP addresses or CIDR
ranges. Managed via **Settings → Kiosk Mode Allowlist** in the UI.

**How it works:**
1. Every request to `/dashboard` and `/api/dashboard/live` checks whether the client IP
   matches `kiosk_allowlist` (CIDR-aware, via `ipaddress.ip_address` / `ip_network`).
2. Matching IPs receive a synthetic `_KioskUser` object (not a DB account) with
   `role = UserRole.PRESENTATION`. No session cookie is required.
3. Non-matching IPs continue through the normal `get_current_user` authentication flow.

`_KioskUser` is a plain Python class (not an ORM model) that mimics the `User` interface:
```python
class _KioskUser:
    id = None; username = "kiosk"; role = UserRole.PRESENTATION; is_active = True
    def can_view_credentials(self): return False
    def can_create_jobs(self): return False
    def can_manage_users(self): return False
    def can_manage_agents(self): return False
```

**Template behaviour:** `base.html` detects `user.role.value == 'presentation'` and hides
the `<aside>` sidebar, giving the dashboard a full-width layout with an "Anvil Dashboard"
heading.

---

### Dashboard auto-refresh and live polling

The dashboard (`dashboard/index.html`) uses an Alpine.js `dashboardLive()` component that:
- Polls `/api/dashboard/live` every **10 seconds** — updates KPI tile values, agent
  online dots, and running job hash rates without a full page reload
- Triggers `location.reload()` every **60 minutes** to pick up new jobs/agents and
  refresh the full DOM

The job list and agent list pages each have a `setTimeout(() => location.reload(), 30000)`
to refresh every 30 seconds.

`/api/dashboard/live` is a lightweight JSON endpoint that returns aggregated counts and
per-job/per-agent state. It is accessible to kiosk IPs without a session cookie.

**Hash rate formatting:** `fmtSpeed(hs)` in the dashboard template converts raw H/s to
a human-readable unit (H/s → kH/s → MH/s → GH/s).

---

### Dashboard KPI tiles

The dashboard now shows five KPI tiles in a grid:
1. **Total Customers** (cyan) — row count from the `Customer` table
2. **Total Hashes** — sum of `total_hashes` across all jobs
3. **Accounts Cracked** — sum of `cracked_count` across all jobs
4. **Running Jobs** — active job count with live hash rates
5. **Online Agents** — agents with a recent heartbeat

Agent cards in the dashboard show GPU model, VRAM, and CPU core count, right-aligned in
bold for visibility on large screens.

---

### Settings page colour consistency

Each settings card now uses a single colour throughout (icon + action button):

| Card | Colour | Button class |
|---|---|---|
| TLS Extra SANs | Emerald | `btn-save` |
| Agent Provisioning Key | Indigo | `btn-create` |
| Install Access Control | Amber | `btn-warn` |
| Kiosk Mode Allowlist | Violet | `btn-violet` (new class) |

`btn-violet` was added to `base.html`:
```css
background: rgba(139,92,246,0.22); color: #c4b5fd; border: 1px solid rgba(139,92,246,0.4)
```

---

## Bugs fixed — session 5

### Bug 11 — Pydantic v2 nested model mutation silently fails

**Symptom:** Settings changes (install allowlist, kiosk allowlist, provisioning key, TLS
extra SANs) returned a success response but the new values were lost on the next request.
The config was never updated in memory.

**Cause:** Pydantic v2 BaseModel instances are effectively immutable by default. Assigning
directly to a nested model's attribute:
```python
settings.server.kiosk_allowlist = new_list   # WRONG — silently does nothing
```
does not trigger the parent model's `__setattr__`, so the in-memory `settings` object is
never actually mutated.

**Fix:** All runtime settings mutations now use `model_copy(update={...})`:
```python
settings.server = settings.server.model_copy(update={"kiosk_allowlist": new_list})
```
This creates a new model instance with the updated field and assigns it to the parent,
which does trigger `__setattr__` at the parent level where validation is active.

Applied to all four settings mutations in `settings_router.py`.

**Affected file:** `server/anvil_server/routers/settings_router.py`

---

### Bug 12 — Kiosk mode required a DB presentation-role account

**Symptom:** Kiosk IPs were correctly identified, but the dashboard endpoint then
queried the database for a `presentation`-role user. If no such account existed,
`scalar_one_or_none()` returned `None`, the code fell through to `get_current_user`,
and the kiosk IP was redirected to `/login`.

**Fix:** Replaced the DB lookup entirely. Kiosk IPs now receive a `_KioskUser()` instance
(see Kiosk mode section above). No database account of any role is required — the kiosk
allowlist is the sole gate.

**Affected file:** `server/anvil_server/routers/dashboard.py`

---

## Outstanding / next to fix

None known. Server reaches the dashboard and all pages render. Agent WebSocket auth
and job polling are functional once a token is configured.

---

## Deployment reference

### Service management
```bash
sudo systemctl status  anvil-server
sudo systemctl restart anvil-server
sudo systemctl stop    anvil-server
sudo journalctl -u     anvil-server -f
sudo tail -f /opt/anvil/server/logs/error.log
```

### File locations (production)
```
/opt/anvil/server/                  ← server root
/opt/anvil/server/config.toml       ← server config (contains secret key)
/opt/anvil/server/certs/anvil.crt   ← TLS cert (copy to agents)
/opt/anvil/server/certs/anvil.key   ← TLS private key (never copy)
/opt/anvil/server/anvil.db          ← SQLite database
/opt/anvil/server/data/wordlists/   ← uploaded wordlists
/opt/anvil/server/data/hashlists/   ← uploaded hash lists
/opt/anvil/server/logs/error.log    ← application errors

/opt/anvil/agent/                   ← agent root
/opt/anvil/agent/config.toml        ← agent config (contains api_token)
/opt/anvil/agent/certs/anvil.crt    ← server cert copy (for TLS verification)
```

### Switching to PostgreSQL
```toml
# server/config.toml
[database]
url = "postgresql+asyncpg://anvil:password@localhost/anvil"
```
```bash
sudo -u anvil /opt/anvil/server/venv/bin/pip install asyncpg
sudo systemctl restart anvil-server
```

### Replacing the self-signed cert (Let's Encrypt)
```toml
# server/config.toml
[tls]
mode      = "provided"
cert_file = "/etc/letsencrypt/live/example.com/fullchain.pem"
key_file  = "/etc/letsencrypt/live/example.com/privkey.pem"
```
Update gunicorn flags in service file to match, then `systemctl daemon-reload && systemctl restart anvil-server`.

---

## VSCode setup

Open with:
```bash
code anvil/anvil.code-workspace
```

Install recommended extensions when prompted (Continue, Pylint, Black, Jinja HTML, TOML).

**Dev launch (F5):** Starts uvicorn on port **8443** with `--reload`. No sudo needed,
no port permission issues. Use `https://localhost:8443` for local dev.

**Install deps (Ctrl+Shift+B):** Runs `python3 -m venv venv && pip install --prefer-binary -r requirements.txt` in the correct folder.

---

## Key design constraints to keep in mind

- **Presentation role is a hard boundary.** Any new route or template that shows job
  data must check `user.can_view_credentials()` or `user.role == UserRole.PRESENTATION`.
  The check must happen in Python, not just in the template.

- **Never `shell=True`.** All subprocess calls must use explicit arg lists.

- **All uploads go through `save_upload()`.** Never write a user-supplied filename
  directly to disk. The function handles sanitisation, size limits, extension checking,
  and path confinement.

- **Agent tokens are never stored plaintext.** Only `hashlib.sha256(token).hexdigest()`
  goes in the DB. The raw token is shown once in the UI and never again.

- **Audit log must not block.** `audit_service.log_action()` uses `try/except` and
  silently ignores failures. An audit write error must never cause a user-facing error.

- **Config must load at import time.** `settings` is a module-level singleton in
  `config.py`. If config is missing or invalid, the process exits before binding.
  This is intentional — fail fast, don't serve broken.

- **Pydantic v2 nested model mutation requires `model_copy`.** Direct attribute
  assignment on a nested Pydantic model (`settings.server.key = value`) silently fails
  in Pydantic v2 — the parent is never notified and the value is lost. Always use:
  ```python
  settings.server = settings.server.model_copy(update={"key": value})
  ```

- **`config.toml` writes must use targeted `re.sub()`.** Never call `toml.dumps()` to
  rewrite the whole file — it strips all comments. Always use `re.sub()` with a pattern
  that matches the specific key line, leaving the rest of the file untouched.

- **Kiosk sessions are IP-gated, not account-gated.** The `_KioskUser` synthetic class
  replaces any DB lookup for allowlisted IPs. Do not add a presentation-role requirement
  for kiosk access — the allowlist is the sole gate.
