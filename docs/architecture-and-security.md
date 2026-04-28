# Anvil — Architecture and Security Overview

Audience: information-security review.
Scope: how Anvil is built, the trust boundaries, the controls that enforce them, and the explicit differences between credentialled views and presentation/kiosk views.

This document describes what is implemented in code today. It is not a marketing document and does not list aspirational features.

---

## 1. What Anvil is

Anvil is a self-hosted hash-cracking management platform. It is a multi-user web application that orchestrates one or more remote cracking agents which in turn drive [hashcat](https://hashcat.net) against hash lists supplied by analysts.

It is intended to be run on internal infrastructure during authorised offensive engagements (red team, internal pentest, password-policy assessments). It is **not** intended to be exposed to the public internet.

Two-process design:

- **Anvil Server** — the web UI, REST API, database, and orchestration layer. One per deployment.
- **Anvil Agent** — a thin Python service that runs on each cracking host (typically a GPU box). Agents poll the server for work, download artefacts they need, run hashcat as a subprocess, and stream progress back.

---

## 2. High-level architecture

```
                    ┌─────────────────────────────────────┐
                    │ Browser clients (HTTPS)             │
                    │ admin · analyst · viewer · kiosk    │
                    └──────────────┬──────────────────────┘
                                   │  HTTPS (TLS 1.2+)
                                   │  HttpOnly Secure SameSite=strict cookies
                    ┌──────────────▼──────────────────────┐
                    │ Anvil Server (FastAPI + Uvicorn)    │
                    │  ─ Session middleware (JWT cookie)  │
                    │  ─ Security-headers middleware      │
                    │  ─ slowapi rate limiter             │
                    │  ─ Routers: auth/jobs/customers/    │
                    │     users/agents/wordlists/audit/   │
                    │     templates/settings/api/agent    │
                    │  ─ Services: auth/audit/upload/     │
                    │     export/notify/tls/ws            │
                    │  ─ SQLAlchemy ORM →                 │
                    │     SQLite (default) or PostgreSQL  │
                    └──────────────┬──────────────────────┘
                                   │  HTTPS REST + WSS
                                   │  Bearer JWT (type=agent)
                    ┌──────────────▼──────────────────────┐
                    │ Anvil Agent (systemd service)       │
                    │  ─ Poll loop: GET /jobs/next        │
                    │  ─ Heartbeat loop: hardware/health  │
                    │  ─ Download artefacts on demand     │
                    │  ─ Hashcat wrapper (subprocess_exec)│
                    └──────────────┬──────────────────────┘
                                   │  asyncio.create_subprocess_exec
                                   │  (no shell, argv list)
                    ┌──────────────▼──────────────────────┐
                    │ hashcat binary (GPU/CPU)            │
                    └─────────────────────────────────────┘
```

Implementation references:
- Server entrypoint: [server/anvil_server/main.py](../server/anvil_server/main.py)
- Agent entrypoint: [agent/anvil_agent/main.py](../agent/anvil_agent/main.py)
- Agent REST API: [server/anvil_server/routers/api/agent_api.py](../server/anvil_server/routers/api/agent_api.py)
- Hashcat invocation: [agent/anvil_agent/hashcat_wrapper.py](../agent/anvil_agent/hashcat_wrapper.py)

---

## 3. Components and data flows

### 3.1 Server

- **Framework:** FastAPI on Uvicorn, fully `async`.
- **Persistence:** SQLAlchemy 2.x async ORM. Default driver is `aiosqlite` against a local SQLite file; PostgreSQL via `asyncpg` is supported by changing one TOML key.
- **Templating:** Jinja2 server-side rendering with Alpine.js for client-side interactivity. No SPA — every page is a server-rendered HTML response.
- **Static assets:** Served from `/static`. Third-party JS/CSS comes from CDNs (`cdn.jsdelivr.net`, `unpkg.com`) and is allowed by the CSP.

### 3.2 Agent

- **Framework:** Plain Python 3.11+, `asyncio`.
- **Outbound only:** The agent initiates every connection to the server. The server does not connect to agents. This means agents can sit behind NAT/firewalls without inbound rules.
- **Loops:**
  - *Poll loop* — `GET /api/v1/agent/jobs/next` at `agent.poll_interval`.
  - *Heartbeat loop* — `POST /api/v1/agent/heartbeat` with hardware metrics (GPU temp/util, VRAM, CPU, RAM, disk, current job progress, wordlist cache inventory).
  - *WebSocket* — optional bidirectional channel for low-latency progress and download-progress events. JWT is sent in the first message and validated server-side before the connection is added to the broadcast pool ([websocket.py](../server/anvil_server/routers/websocket.py)).
- **Hashcat execution:** `asyncio.create_subprocess_exec(*argv, ...)`. There is no `shell=True` anywhere. The argv is built from typed Python parameters. The environment is sanitised: `LD_PRELOAD`, `LD_AUDIT`, `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONINSPECT`, `PYTHONASYNCIODEBUG` are stripped before invocation ([hashcat_wrapper.py:505-528](../agent/anvil_agent/hashcat_wrapper.py#L505-L528)).
- **Working directories:** Hashcat's session, potfile, OpenCL kernel cache, and NVIDIA cache are redirected to a writable workdir owned by the `anvil` service user (default `/var/lib/anvil-agent/workdir`) via `XDG_DATA_HOME`, `XDG_CACHE_HOME`, and a fake `HOME`. This avoids needing a real `/home/anvil`. Per-job artefacts (downloaded hash list files **and** the hashcat outfile containing cracked plaintexts) live under `workdir/jobs/job_<id>/` and are wiped at end-of-run; see section 13.

### 3.3 Data at rest

| Data | Where it lives | Notes |
|---|---|---|
| User accounts | `users` table | bcrypt-hashed passwords (work factor configurable, minimum 10) |
| Cracked plaintexts | `hashes.plaintext` | Plaintext only after a successful crack; row-level via ORM |
| Hash list files | `server/data/hashlists/` | Raw uploaded files; access via authenticated endpoints only |
| Wordlists / rules | `server/data/wordlists/`, `server/data/rules/` | Same access model |
| TLS private key | `server/certs/anvil.key` | `chmod 0400`, owner `anvil` |
| Agent token (server side) | `agents.api_token_hash` | **SHA-256 of the token, not the token itself** |
| Agent token (agent side) | `agent/config.toml` | Stored in clear, readable only by the `anvil` service user |
| JWT signing key | `server/config.toml` `secret_key` | Generated by `setup.sh`; validated to be ≥32 chars and not the placeholder ([config.py:23-31](../server/anvil_server/config.py#L23-L31)) |
| Audit log | `audit_log` table | See section 9 |

The repository's [.gitignore](../.gitignore) excludes `server/config.toml`, `agent/config.toml`, `server/anvil.db*`, `server/certs/`, `server/data/{wordlists,hashlists,exports}/`, `server/logs/`, `agent/logs/`, and the offline build staging dir, so secrets and customer data cannot be committed accidentally.

---

## 4. Trust boundaries

```
┌──────────────────┐    TLS    ┌──────────────────┐
│ Browser (user)   │◄─────────►│ Anvil Server     │
│  cookie (JWT)    │           │                  │
└──────────────────┘           │                  │
                               │                  │
┌──────────────────┐    TLS    │                  │
│ Anvil Agent      │◄─────────►│                  │
│  bearer JWT      │           │                  │
└────────┬─────────┘           └──────────────────┘
         │ subprocess (argv, no shell)
         ▼
   hashcat binary
   (treated as untrusted — env scrubbed,
    flags pre-validated server-side)
```

The four parties Anvil distinguishes are:

1. **Browser users** — authenticated by session cookie containing a signed JWT. Authorisation by role.
2. **Kiosk browsers** — an IP-allowlisted bypass that grants a *synthetic* presentation user (no DB account, no credentials access). Section 6 describes this in detail.
3. **Agents** — authenticated by long-lived bearer JWT (`type: "agent"`). Each agent token is bound to one `agents` row by SHA-256 hash.
4. **The hashcat process** — treated as untrusted user-controlled code; its invocation is constrained.

---

## 5. Authentication and authorisation

### 5.1 Browser sessions

- Login is `POST /login` with form fields `username`, `password`. Failed logins are recorded in the audit log with the source IP ([auth.py:50-57](../server/anvil_server/routers/auth.py#L50-L57)).
- bcrypt with configurable rounds (default 12, minimum 10 enforced by the config validator).
- On success the server issues an HS256 JWT containing `sub`, `role`, `iat`, `exp` (8 h default), and a `jti` (UUID-style nonce reserved for future revocation). The token is set as the session cookie.
- The cookie is `HttpOnly`, `Secure`, `SameSite=strict`, and is also enforced by Starlette's `SessionMiddleware` with `https_only=True` ([main.py:154-161](../server/anvil_server/main.py#L154-L161)).
- Logout deletes the cookie and writes an audit entry.
- A dedicated `force_password_change` flag pins the seeded admin (`admin / ChangeMe123!`) to a forced rotation on first login. Password policy on change is enforced server-side: ≥12 characters, must differ from current ([auth.py:99-106](../server/anvil_server/routers/auth.py#L99-L106)).

### 5.2 JWT keys and types

- One signing key (`server.secret_key`) — minimum 32 characters, validated at boot. The application refuses to start with the placeholder value.
- Three token types, distinguished by the `type` claim:
  - *(absent)* — a user session token. Cannot access agent endpoints.
  - `"agent"` — long-lived agent token (default ~30 days). Cannot access UI endpoints (`_get_user_from_token` rejects them).
  - `"bootstrap"` — short-lived (1 h) token used during the install handshake.
- Each access path checks the `type` claim explicitly before doing anything privileged.

### 5.3 Roles

Defined in [models/user.py](../server/anvil_server/models/user.py):

| Role | Create jobs | View credentials | Manage users/agents | Notes |
|---|---|---|---|---|
| `admin` | yes | yes | yes | Full access. Unlimited upload size. |
| `analyst` | yes | yes | no | 30 GB upload cap. |
| `viewer` | no | yes | no | Read-only across cracked data. |
| `presentation` | no | **no** | no | Aggregates only. See section 6. |

Authorisation is enforced by FastAPI dependencies:
- `require_admin`, `require_analyst`, `require_viewer`, `require_any_role` are factories around `require_role(...)` ([auth_service.py:156-171](../server/anvil_server/services/auth_service.py#L156-L171)).
- Templates additionally branch on `user.can_view_credentials()` so the *DOM never contains* credential fields for presentation/kiosk viewers — they are not just CSS-hidden.

### 5.4 Rate limiting

`slowapi` is wired in with a default of 300 requests/minute per remote address ([main.py:28](../server/anvil_server/main.py#L28)). Login and API endpoints can be tightened individually via `security.login_rate_limit` and `security.api_rate_limit`.

---

## 6. Presentation view vs. credentialled view

This is the most security-relevant view distinction in the platform. There are **two** ways a browser ends up in presentation mode:

1. The user logs in with a `presentation` role account.
2. The browser's source IP matches `server.kiosk_allowlist` and hits `/dashboard`. In that case the request is served *without* a session cookie, using a synthetic in-memory `_KioskUser` object whose role is `PRESENTATION` ([dashboard.py:77-91](../server/anvil_server/routers/dashboard.py#L77-L91)).

Both paths converge on the same `is_presentation` template flag and the same `can_view_credentials() == False` check. The differences from a credentialled view are:

| Field / surface | Credentialled (admin/analyst/viewer) | Presentation / kiosk |
|---|---|---|
| Customer display name | Real `customer.name` | `customer.presentation_name` (configured per-customer; intended as a public-safe alias, e.g. project codename) |
| Username column on cracked hashes | Shown | Not rendered in the template at all |
| Plaintext column | Shown | Not rendered |
| CSV export | Full table: `username, hash, plaintext, cracked_at` | Summary only: job name, totals, crack rate, status. Implemented in [export_service.py:25-58](../server/anvil_server/services/export_service.py#L25-L58) with role gating, not just template gating. |
| PDF export | Summary table + full credential table appended | Summary only ([export_service.py:106-133](../server/anvil_server/services/export_service.py#L106-L133)) |
| Sidebar / navigation | Full | Hidden (`_is_kiosk` template branch in [base.html](../server/anvil_server/templates/base.html)) so a viewer cannot click into Users/Agents/Audit/etc. |
| Admin pages (`/users`, `/agents`, `/audit`, `/settings`) | Reachable subject to role | Not linked; routes still enforce `require_admin`, so a manually crafted URL still 403s |
| Live dashboard polling | Authenticated; full payload | The `/api/dashboard/live` endpoint is reachable from kiosk IPs without a cookie, but the response intentionally contains only counts and progress (no usernames, no plaintexts). |
| WebSocket job channel | Subscribable | Same subscription path is used, but the payloads broadcast there are progress/phase/hashrate — never plaintext. |

**Why the dual enforcement (template + service):** even if a future template change accidentally references `h.plaintext` for a presentation user, the export service still strips it server-side, and the `current_candidate` (the live word being tried) is the only password-shaped string ever emitted, by design — it is *attempt* data, not *cracked* data.

**Kiosk bypass implications to flag explicitly to InfoSec:**
- Anyone connecting from an allowlisted IP gets the dashboard with no login. The allowlist is intended for fixed display devices (TV/projector) on a wired internal VLAN. It must not include broad ranges, public IPs, or VPN pools.
- The bypass applies only to `/dashboard` and `/api/dashboard/live`. All other routes (jobs detail, exports, users, agents, audit, settings) still require a session cookie.
- IP-mapped IPv6 addresses are normalised before the allowlist comparison so dual-stack listeners do not silently widen the rule ([dashboard.py:94-110](../server/anvil_server/routers/dashboard.py#L94-L110)).
- The default config ships with `kiosk_allowlist = []`, i.e. disabled. Turning it on is an explicit administrator action via the Settings UI.

---

## 7. Agent provisioning, authentication, and lifecycle

### 7.1 Zero-touch provisioning

`setup.sh` generates an `agent.provisioning_key` (random URL-safe ~64 char) at install time. The server exposes a templated install script at `GET /agents/install` which embeds this key. An operator runs:

```
curl -sfk https://<server>/agents/install | sudo bash
```

The script:
1. Downloads the agent package from the server (`/agents/package`).
2. `POST /api/v1/agent/provision` with `{provisioning_key, name, hostname}`.
3. Receives a freshly minted long-lived agent JWT, writes it to `agent/config.toml`, sets `chmod 0600`, and starts the systemd service.

The provisioning endpoint uses `hmac.compare_digest` to compare the supplied key against the server's, preventing timing attacks ([agent_api.py:111-114](../server/anvil_server/routers/api/agent_api.py#L111-L114)). If a re-install runs against an existing hostname, the server reuses the agent record and rotates its token rather than creating duplicates.

### 7.2 Restricting who can install

Two controls are independent:

- **Provisioning key** — a shared secret. Compromise it and someone can register a rogue agent. Rotate via Settings → Agent Provisioning Key. Rotation does not invalidate already-issued agent tokens (existing agents keep working); only new registrations are blocked.
- **Install allowlist** — `agent.install_allowlist` (IP/CIDR list). The `GET /agents/install` and `GET /agents/package` endpoints are 403'd for clients outside the list ([agents.py:30-37](../server/anvil_server/routers/agents.py#L30-L37)). This is the primary defence; the provisioning key alone is not sufficient if you cannot reach the install endpoints.

### 7.3 Agent runtime authentication

- Every agent → server call carries `Authorization: Bearer <jwt>`.
- The server decodes the JWT, requires `type == "agent"`, then SHA-256s the raw token and looks up `agents.api_token_hash`. The DB only ever stores the hash, so a database read does not leak agent tokens ([auth_service.py:178-211](../server/anvil_server/services/auth_service.py#L178-L211)).
- An agent that is `is_active=False` is rejected even if the JWT is valid and unexpired. Deactivation is the immediate revocation path.
- `last_seen` is updated on every authenticated call; the dashboard derives "online" status from the configurable `agent.heartbeat_timeout`.

### 7.4 What an agent is allowed to do

Only the endpoints under `/api/v1/agent/*`. They cover: provision, capabilities, heartbeat, claim next assigned job, download a hash list / wordlist / rule by ID, submit cracked results, identify-hash, health. An agent **cannot** read other tenants' data, list users, change settings, or fetch arbitrary files — the file endpoints validate the requested resource exists and stream the on-disk file from a fixed storage root.

A submitted job result is rejected unless the job's `agent_id` equals the calling agent's id ([agent_api.py:348-349](../server/anvil_server/routers/api/agent_api.py#L348-L349)).

---

## 8. Hashcat invocation safety

This is the most security-sensitive subsystem because it executes user-influenced arguments against a native binary.

- **No shell.** `asyncio.create_subprocess_exec(*argv)` is used everywhere. There is no `shell=True`, no `os.system`, no string interpolation into a command line.
- **Server-side flag allowlist.** The job-creation form's free-form `extra_flags` field is parsed and checked against a deny-list before the job is persisted ([jobs.py:546-560](../server/anvil_server/routers/jobs.py#L546-L560)):

  ```
  --stdout, --outfile, --outfile-format, --potfile-path,
  --session, --restore-file-path, --debug-file,
  --induction-dir, --outfile-check-dir
  ```

  These are the flags that could be used to redirect output to attacker-chosen paths or read arbitrary files. The deny-list is enforced server-side; agents do not re-validate (the trust direction is server → agent).

- **Mode whitelist.** `attack_mode` is a Python `Enum` (`AttackMode`), not a free-form string.
- **Hash type.** `hash_type` is constrained to the integer modes defined in [hashcat_modes.py](../server/anvil_server/hashcat_modes.py).
- **Environment sanitisation.** As above — `LD_PRELOAD`/`LD_AUDIT` and the dangerous `PYTHON*` vars are stripped from the subprocess env so hashcat cannot be coerced into loading attacker-controlled shared objects via inherited env.
- **Output capture.** Cracked hashes are written by hashcat to an outfile inside the agent's workdir (`--outfile=…/job_<id>.potfile`, `--outfile-format=2`). The wrapper parses this file after the process exits and submits results back over HTTPS — there is no shell parsing of stdout.
- **Hashcat exit code mapping.** Codes 0/1 → completed/exhausted; 3 → aborted; anything else → error with stderr captured to the audit/log path. This prevents hashcat returning a "success" status on bad-args/file-not-found ([hashcat_wrapper.py:217-227](../agent/anvil_agent/hashcat_wrapper.py#L217-L227)).

---

## 9. Audit logging

Implemented in [services/audit_service.py](../server/anvil_server/services/audit_service.py) and [models/audit.py](../server/anvil_server/models/audit.py).

Recorded actions include:
- `login_success`, `login_failed`, `logout`, `password_changed`
- `job_created`, `job_cancelled`, `job_rerun`, `job_deleted`
- `user_created`, `user_role_changed`, `user_password_reset`, `user_deactivated`
- `agent_registered`, `agent_deactivated`, `provisioning_key_rotated`
- `wordlist_uploaded`, `rule_uploaded`, `wordlist_deleted`
- `customer_created`, `customer_updated`, `customer_hashes_deleted`
- `csv_exported`, `pdf_exported`
- `kiosk_allowlist_updated`, `install_allowlist_updated`, `tls_extra_sans_updated`

Each entry stores user id (nullable for anonymous events like failed login), action, optional resource type/id, an arbitrary JSON `details` dict, source IP, and timestamp. Audit writes are wrapped in a try/except that swallows errors so an audit-table outage cannot block the primary action — InfoSec should be aware of this trade-off; the alternative was a denial of service if the audit table is unavailable.

The audit log is browsable at `/audit` for admins, with action-type filtering.

---

## 10. Network controls and HTTP hygiene

### 10.1 TLS

- TLS terminates at Uvicorn inside the `anvil-server` process. There is no reverse proxy by default.
- `tls.mode = "self_signed"` (default): the server generates a 4096-bit RSA cert, valid 10 years, with SANs covering `localhost`, the host's FQDN, `127.0.0.1`, the host's primary IP, and any operator-supplied entries in `tls.extra_sans`. The cert is regenerated on startup if it expires within 30 days or the configured SANs are not covered ([tls_service.py:152-176](../server/anvil_server/services/tls_service.py#L152-L176)).
- `tls.mode = "provided"`: operator supplies cert/key paths (e.g. Let's Encrypt or an internal CA).
- Minimum TLS version: 1.2.
- The private key is written `chmod 0400`, owner `anvil`. The cert is `0444`.
- HTTPSRedirect middleware is wired in.

### 10.2 Security headers

Set globally by the [`SecurityHeadersMiddleware`](../server/anvil_server/main.py) on every response:

- `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload`
- `Content-Security-Policy`: `default-src 'self'`, scripts/styles allowed from self, jsdelivr, unpkg (CDN-served Alpine, Tailwind, Chart.js); `frame-ancestors 'none'`; `img-src 'self' data:`; `connect-src 'self' wss: cdn.jsdelivr.net`.
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: geolocation=(), microphone=(), camera=()`
- `X-XSS-Protection: 1; mode=block` (legacy browsers)

### 10.3 Cookies

`HttpOnly`, `Secure`, `SameSite=strict`, lifetime defaults to 8 h (`session_max_age = 28800`).

### 10.4 Rate limiting

300 req/min per source IP globally. Configurable per-route limits for login and API.

---

## 11. File upload pipeline

Wordlists, rules, and hash lists go through [services/upload_service.py](../server/anvil_server/services/upload_service.py):

1. **Filename sanitisation** — Unicode normalised to ASCII, path separators stripped, `..` removed, regex-replaces anything outside `[A-Za-z0-9._-]`, max 255 chars.
2. **Extension allowlist** — Per-router. Hash lists: `.txt`, `.hash`, `.hashes`, `.lst`, `.pot`. Wordlists: `.txt`, `.lst`, `.dict`, `.wl`. Rules: `.rule`, `.rules`.
3. **Resolved-path confinement** — After joining the sanitised filename to the destination dir, both paths are `.resolve()`d and the destination must be `relative_to` the storage root. This catches any unicode/symlink edge cases the sanitiser misses ([upload_service.py:60-68](../server/anvil_server/services/upload_service.py#L60-L68)).
4. **Streamed write with size cap** — 64 KB chunks, with size enforcement *during* write (not just from the `Content-Length` header). Going over the cap deletes the partial file and returns 413. The cap is 30 GB for non-admin uploaders, 100 GB for admin (effectively unlimited).
5. **No-clobber** — Existing destinations are renamed `name_1.ext`, `name_2.ext`, …
6. **SHA-256** is computed during the stream so files can be deduplicated and integrity-checked.

Hash files are also content-validated when ingested into the `hashes` table; CSV exports prefix any cell starting with `=`, `+`, `-`, `@`, tab, or CR with a single quote to mitigate spreadsheet formula injection ([export_service.py:15-22](../server/anvil_server/services/export_service.py#L15-L22)).

---

## 12. Per-customer data lifecycle

Every job is bound to a `Customer` row. The customer page exposes a **Delete hashes** action (admin-only) which:

- Cascades the deletion of `Hash` rows for all `HashList` records belonging to all jobs of that customer.
- Unlinks the underlying hash files from disk **only if no surviving hash list still references them**.
- Preserves `Job.total_hashes` and `Job.cracked_count` so historical aggregates and the dashboard's crack-rate stat survive a purge.
- Writes a `customer_hashes_deleted` audit entry.

This gives an analyst a clear, auditable blast radius when an engagement ends and the customer asks for credential data to be destroyed.

---

## 13. Agent-side residual data

What an agent host writes to local disk during and after a job, and what is cleaned up. Relevant to data-retention and host-decommissioning policies. All paths are relative to `hashcat.workdir` (default `/var/lib/anvil-agent/workdir`, owned by the `anvil` service user, mode 0700 by default from `setup.sh`).

| Path | Contents | Lifecycle |
|---|---|---|
| `jobs/job_<id>/<hashlist>` | Downloaded hash list files for one job | Wiped via `shutil.rmtree(job_dir)` in the `finally` block of [job_runner.py](../agent/anvil_agent/job_runner.py) — runs on completed, failed, **and** cancelled jobs |
| `jobs/job_<id>/cracked.potfile` | hashcat outfile, format `2` (`hash:plaintext`) | Same `rmtree` — plaintexts do not survive the run. The outfile path is passed into the wrapper so it stays inside `job_dir` ([hashcat_wrapper.py](../agent/anvil_agent/hashcat_wrapper.py)) |
| `cache/wordlists/<id>_<name>` | Cached wordlists | Kept across jobs by design (caching is the point). Deleted only when an admin deletes the wordlist on the server, which fans out a delete command to every agent that had it cached (REST `DELETE /api/v1/agent/cache/wordlist` and the WS `delete_cached_wordlist` message in [server_client.py](../agent/anvil_agent/server_client.py)) |
| `cache/rules/<id>_<name>` | Cached rule files | Kept indefinitely. There is currently no server-initiated deletion path for rule files; if a rule is removed on the server, copies remain on agents that previously cached it. Operators who need to expire these must wipe them manually. |
| `xdg/data/hashcat/sessions/` | hashcat `.pid`/`.induct` session metadata | Not credential-bearing. Accumulates across jobs. |
| `xdg/cache/pocl/`, `xdg/home/.nv/ComputeCache/` | OpenCL/NVIDIA kernel caches | Not credential-bearing. Accumulates. Required for the GPU runtime. |
| `./anvil-agent.log` (configurable, rotated 10 MB × 5) | Agent logs | At default `INFO` level the log does **not** contain plaintexts. At `DEBUG` it logs the live `current_candidate` (the guess being tested) — that is *attempt* data, not crack data, but for rule attacks the mod string can equal the eventual plaintext if the candidate cracks. Production agents should stay at `INFO` or higher. |

Job-outcome matrix:

| Outcome | Hash list files | `cracked.potfile` (plaintexts) | Cached wordlists / rules |
|---|---|---|---|
| Completed | Deleted | Deleted | Kept |
| Failed (hashcat error or runner exception) | Deleted (`finally` block) | Deleted (`finally` block) | Kept |
| Cancelled (server-issued) | Deleted | Deleted (any partial cracks too) | Kept |
| Agent process killed mid-run (SIGKILL, OOM) | May be left behind | May be left behind | Kept |

Worst-case forensic recovery: a SIGKILL during a running job leaves `jobs/job_<id>/` behind. The next clean job that completes does *not* sweep stale siblings — operators who want a guarantee should add `find /var/lib/anvil-agent/workdir/jobs -mindepth 1 -maxdepth 1 -type d -mtime +1 -exec rm -rf {} +` to a periodic systemd timer. Cached wordlists and rules are kept on purpose — they are not customer data; they are public/operator-supplied attack material.

**Note on agents upgraded from earlier builds:** prior to this change the hashcat outfile lived at `workdir/job_<id>.potfile` (one per job, at the workdir root) and was never deleted. Existing agents may carry these from previous engagements; sweep with `rm -f /var/lib/anvil-agent/workdir/job_*.potfile` once after upgrading.

---

## 14. Deployment modes

### 14.1 Online install

`server/setup.sh` provisions a system user `anvil`, a venv at `/opt/anvil/server/venv`, the systemd unit `anvil-server.service`, the TLS cert, the JWT secret, and the provisioning key. Default port 443.

### 14.2 Offline / airgapped install

Two-step flow targeting Ubuntu 24.04 LTS only:

1. On a connected build host: `server/package-offline.sh` resolves the full dependency closure of the required apt packages and downloads them into `server/offline/debs/`. It downloads every Python wheel from `requirements.txt` (cp312/manylinux only) into `server/offline/wheels/`. A `MANIFEST.txt` records build metadata.
2. On the airgapped target: `server/setup-offline.sh` installs the .debs with `apt-get install --no-download` and creates the venv with `pip install --no-index --find-links=offline/wheels/`. No DNS or outbound connectivity is required.

The bundle directory is in `.gitignore` — it is build-host-specific and contains binaries, so it must not be checked in.

### 14.3 Agent install

Either via `curl … | sudo bash` against the install endpoint (subject to `install_allowlist`), or by copying the agent package to the host manually and running `agent/setup.sh`.

---

## 15. Known security trade-offs and residual risks (be transparent with InfoSec)

These are documented limits, not "TODOs we forgot":

1. **Default self-signed TLS.** Browsers will present a warning the first time an admin connects. Internal use is fine; replace with an internal-CA cert (`tls.mode = "provided"`) if your environment requires no warnings.
2. **JWT revocation is coarse.** A user/agent JWT cannot be revoked individually before its expiry. Mitigations: `is_active = False` on the user/agent row instantly invalidates them server-side; the user-facing `last_login` and the agent's `last_seen` are tracked. A `jti` claim is already included for future per-token revocation.
3. **Audit-write failures are swallowed.** A misbehaving DB will not stop login or job creation. This is intentional — availability over completeness.
4. **WebSocket browser channel is not re-authenticated.** The `/ws/jobs/{id}` socket relies on the assumption that the surrounding job-detail page is auth-gated. The same channel is reachable to kiosk IPs but only carries non-credential progress payloads. The agent-side WS channel `/ws/agent/{id}` *is* re-authenticated on the first message and rejects mismatched tokens.
5. **`unsafe-inline` and `unsafe-eval` are present in the CSP** to support Alpine.js and inline `x-data` directives. This is the standard Alpine deployment posture; if your policy forbids it, switch to a build-step Alpine bundle and remove the directives.
6. **Plaintexts are stored in the database.** This is inherent to the use case (a hash-cracking platform). The mitigations are: TLS in transit, role-based access, per-customer purge, audit logging, and presentation/kiosk views that never include plaintext.
7. **The kiosk bypass is IP-based.** IP allowlists are not authentication. They are appropriate for a fixed display device on a wired LAN; they are *not* appropriate as the only control on a Wi-Fi or VPN segment where IPs can be spoofed or reassigned.
8. **Hashcat is a native binary running with full access to the agent's GPU and filesystem.** The agent host should be considered a privileged compute resource and segregated from production assets accordingly. Anvil sandboxes hashcat's *invocation* (no shell, env scrubbed, flag deny-list) but it does not (and cannot) sandbox hashcat's *runtime*.
9. **Cached wordlists and rules persist on agents.** This is by design — caching prevents re-downloading multi-GB wordlists for every job — but it means an agent host accumulates the operator's attack material over its lifetime. They are not customer data, but they are sensitive in environments where the *list* of attack inputs is itself confidential. Sanitise agent hosts before redeployment or disposal (`rm -rf /var/lib/anvil-agent/workdir`).
10. **Agent log level controls candidate disclosure.** At `DEBUG` the agent log records the live hashcat candidate. Keep agents at `INFO` (the default) unless actively troubleshooting, and rotate/clear logs that were captured at `DEBUG`.

---