"""Job runner — downloads required files then executes a single cracking job."""
from __future__ import annotations
import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

from .config import settings
from .hardware_monitor import HardwareMonitor
from .hashcat_wrapper import HashcatWrapper, ProgressEvent
from .server_client import AgentWSClient, download_file, submit_result

logger = logging.getLogger("anvil.agent.runner")


# ---------------------------------------------------------------------------
# Durable result delivery (Bug 13)
#
# A cracked result reaches the server only via submit_result. If that POST fails
# (network blip, or — the Bug 12/210 case — a rotated token returning 401), the
# cracks must not be lost. We persist each unconfirmed result to its job dir and
# a sweep retries it (on startup and while idle) until the server confirms it.
# ---------------------------------------------------------------------------

def _is_confirmed(resp: dict, unique_sent: int) -> bool:
    """A result is persisted iff the server returned 200/accepted and recorded at
    least as many unique cracks as we sent."""
    body = resp.get("json") or {}
    return (
        resp.get("status_code") == 200
        and body.get("status") == "accepted"
        and body.get("cracked_recorded", 0) >= unique_sent
    )


def _write_pending_result(job_dir: Path, payload: dict) -> None:
    """Persist a job result to its (kept) job dir so a crash, kill, or rotated
    token during submit can't lose the cracks — the sweep will retry it."""
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        tmp = job_dir / "result.json.tmp"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(job_dir / "result.json")  # atomic within the dir
    except OSError as exc:
        logger.error("Could not persist pending result to %s: %s", job_dir, exc)


def _move_aside(d: Path, dest_root: Path) -> None:
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        target = dest_root / d.name
        if target.exists():
            target = dest_root / f"{d.name}_{int(time.time())}"
        d.rename(target)
    except OSError as exc:
        logger.error("Could not move %s aside to %s: %s", d, dest_root, exc)


async def resubmit_pending_results(current_job_id: Optional[int] = None) -> None:
    """Re-POST any persisted-but-unconfirmed job results (Bug 13). Confirmed
    results have their job dir removed; results the server reports as gone or
    reassigned (403/404) are moved aside so they stop retrying forever without
    destroying the plaintexts; anything else is left for the next sweep. The
    actively-running job is skipped."""
    jobs_root = Path(settings.hashcat.workdir) / "jobs"
    if not jobs_root.is_dir():
        return
    undeliverable = jobs_root / "undeliverable"
    for d in sorted(jobs_root.glob("job_*")):
        if not d.is_dir():
            continue
        rf = d / "result.json"
        if not rf.exists():
            continue
        try:
            jid: Optional[int] = int(d.name.split("_", 1)[1])
        except (IndexError, ValueError):
            jid = None
        if current_job_id is not None and jid == current_job_id:
            continue
        try:
            payload = json.loads(rf.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error("Pending result %s is unreadable (%s); moving aside.", rf, exc)
            _move_aside(d, undeliverable)
            continue
        unique_sent = len({c.get("hash_value") for c in payload.get("cracked", [])})
        resp = await submit_result(payload)
        if _is_confirmed(resp, unique_sent):
            logger.info(
                "Delivered pending result for job %s (%d unique cracks) — server confirmed.",
                jid, unique_sent,
            )
            shutil.rmtree(d, ignore_errors=True)
        elif resp.get("status_code") in (403, 404):
            logger.error(
                "Pending result for job %s is undeliverable (HTTP %s — job gone or "
                "reassigned). Moving to %s instead of retrying forever.",
                jid, resp.get("status_code"), undeliverable,
            )
            _move_aside(d, undeliverable)
        else:
            logger.warning(
                "Pending result for job %s not yet accepted (resp=%s); will retry.",
                jid, resp,
            )


class JobRunner:
    def __init__(self, hw_monitor: HardwareMonitor, ws_client: AgentWSClient) -> None:
        self._hw = hw_monitor
        self._ws = ws_client
        self._current_job_id: Optional[int] = None

    @property
    def is_busy(self) -> bool:
        return self._current_job_id is not None

    async def run(self, job: dict) -> None:
        job_id: int = job["id"]
        self._current_job_id = job_id
        started = time.time()

        cancel_event = asyncio.Event()
        self._ws.register_cancel(job_id, cancel_event)

        wrapper = HashcatWrapper()

        # Watch for cancel independently of the status-event stream. The main loop
        # below only observes cancel_event between yielded hashcat status events, so
        # if hashcat goes silent (or hangs) a cancel would otherwise sit unread until
        # the stall watchdog fires (up to warmup_timeout). This task calls
        # wrapper.cancel() the instant the cancel arrives, terminating hashcat (and
        # thus unblocking the output stream) within ~1s.
        async def _await_cancel() -> None:
            await cancel_event.wait()
            logger.info("Job %d: cancel received — terminating hashcat", job_id)
            wrapper.cancel()

        cancel_watcher = asyncio.create_task(_await_cancel())
        cracked_entries = []
        last_event: Optional[ProgressEvent] = None
        error_message: Optional[str] = None

        # Directories for this job's files
        workdir = Path(settings.hashcat.workdir)
        job_dir = workdir / "jobs" / f"job_{job_id}"
        cache_dir = workdir / "cache"

        logger.info("Starting job %d (attack_mode=%d, hash_type=%s)",
                    job_id, job.get("attack_mode"), job.get("hash_type"))

        try:
            # ── Stage files ───────────────────────────────────────────────────
            await self._hw.set_job_context(job_id, None, phase="downloading")
            hash_list_files = await self._stage_hash_lists(job, job_dir)
            wordlist_path   = await self._stage_wordlist(job, cache_dir)
            rule_paths      = await self._stage_rules(job, cache_dir)

            # ── Run hashcat ───────────────────────────────────────────────────
            await self._hw.set_job_context(job_id, None, phase="cracking")
            async for event in wrapper.run(
                job_id=job_id,
                attack_mode=job.get("attack_mode", 0),
                hash_type=job.get("hash_type"),
                hash_list_files=hash_list_files,
                wordlist_path=wordlist_path,
                rule_paths=rule_paths,
                mask=job.get("mask"),
                extra_flags=job.get("extra_flags"),
                started_at_ts=started,
                outfile_path=job_dir / "cracked.potfile",
            ):
                if cancel_event.is_set():
                    wrapper.cancel()

                last_event = event

                if event.cracked:
                    cracked_entries.extend(event.cracked)

                if event.error:
                    error_message = event.error

                await self._hw.set_job_context(job_id, event.hashrate_hs, phase="cracking")

                metrics = await self._hw.get_metrics()
                gpu = metrics.gpus[0] if metrics.gpus else None
                await self._ws.send_progress({
                    "type": "progress",
                    "job_id": job_id,
                    "phase": "cracking",
                    "progress_pct": event.progress_pct,
                    "hashrate_hs": event.hashrate_hs,
                    "eta_seconds": event.eta_seconds,
                    "keys_tried": event.keys_tried,
                    # Live cracked count from hashcat's status (recovered_hashes).
                    # Lets the dashboard show the count rising during the run; the
                    # authoritative deduplicated count still settles at job end.
                    "recovered": event.recovered,
                    "total_hashes": event.total,
                    "gpu_temp_c": gpu.temp_c if gpu else None,
                    "gpu_util_pct": gpu.util_pct if gpu else None,
                    "current_candidate": event.current_candidate,
                    "status": event.status,
                })

        except asyncio.CancelledError:
            wrapper.cancel()
            raise
        except Exception as exc:
            logger.exception("Job %d runner error: %s", job_id, exc)
            error_message = str(exc)
        finally:
            cancel_watcher.cancel()  # stop the cancel-watcher task
            wrapper.cancel()  # no-op if already cancelled or process already exited
            self._ws.deregister_cancel(job_id)
            await self._hw.set_job_context(None, None)
            self._current_job_id = None
            # NOTE: job_dir holds the cracked outfile. It is deliberately NOT
            # deleted here — we keep it until the server confirms it persisted the
            # cracked results (below), so a failed or dropped submit can't silently
            # lose plaintexts. Previously this finally (which runs *before* the
            # submit) wiped the outfile unconditionally. See Bug 12.

        # Determine final status
        if cancel_event.is_set():
            final_status = "cancelled"
        elif error_message:
            final_status = "failed"
        elif last_event and last_event.status == "error":
            final_status = "failed"
            if last_event.error and not error_message:
                error_message = last_event.error
        else:
            final_status = "completed"

        result_payload = {
            "job_id": job_id,
            "status": final_status,
            "cracked": [
                {
                    "hash_value": c.hash_value,
                    "plaintext": c.plaintext,
                    "time_to_crack_seconds": c.time_to_crack_seconds,
                }
                for c in cracked_entries
            ],
            "error_message": error_message,
            "final_progress_pct": last_event.progress_pct if last_event else 0.0,
        }

        # Submit, then only delete the per-job dir (which holds the cracked
        # outfile) once the server CONFIRMS it persisted at least as many unique
        # cracks as we sent. Otherwise keep the dir so the plaintexts survive on
        # disk for recovery/resubmission instead of being silently lost (Bug 12).
        unique_sent = len({c.hash_value for c in cracked_entries})
        # Persist to the (kept) job dir BEFORE submitting, so a crash/kill or a
        # rotated token mid-submit can't lose the cracks; the sweep retries it.
        _write_pending_result(job_dir, result_payload)
        resp = await submit_result(result_payload)
        if _is_confirmed(resp, unique_sent):
            logger.info(
                "Job %d complete: status=%s cracked=%d (server confirmed %d recorded)",
                job_id, final_status, len(cracked_entries),
                (resp.get("json") or {}).get("cracked_recorded", 0),
            )
            if job_dir.exists():
                shutil.rmtree(job_dir, ignore_errors=True)
        else:
            logger.error(
                "Job %d: server did NOT confirm result (sent %d unique cracks, "
                "resp=%s). Persisted to %s; the agent will retry until accepted.",
                job_id, unique_sent, resp, job_dir / "result.json",
            )

    async def _stage_hash_lists(self, job: dict, job_dir: Path) -> list[str]:
        """Download hash list files for this job. Always re-downloaded (per-job)."""
        hash_lists = job.get("hash_lists") or []
        if not hash_lists:
            raise RuntimeError("Job has no hash lists")

        job_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for hl in hash_lists:
            hl_id = hl["id"]
            name = hl.get("name") or f"hashlist_{hl_id}.txt"
            dest = job_dir / name
            logger.info("Job %d: downloading hash list %d (%s) ...", job["id"], hl_id, name)
            await download_file(f"/api/v1/agent/files/hashlist/{hl_id}", dest)
            paths.append(str(dest))
        return paths

    def _make_dl_progress_cb(self, job_id: int, filename: str, size_hint: int) -> Callable:
        """Return a throttled async progress callback that pushes WS download_progress events."""
        last_report = [0.0]
        last_pct = [-1.0]

        async def cb(done: int, total: int) -> None:
            now = time.monotonic()
            bytes_total = total or size_hint or 0
            pct = (done / bytes_total * 100) if bytes_total else 0.0
            # Throttle: send at most once per second (pct change not required so
            # the bar stays alive even if download speed is slow or constant).
            if now - last_report[0] < 1.0 and pct < 99.5:
                return
            last_report[0] = now
            last_pct[0] = pct
            await self._ws.send_progress({
                "type": "download_progress",
                "job_id": job_id,
                "filename": filename,
                "bytes_done": done,
                "bytes_total": bytes_total,
                "pct": round(pct, 1),
            })

        return cb

    async def _stage_wordlist(self, job: dict, cache_dir: Path) -> Optional[str]:
        """Download wordlist if needed; cached by ID+size so large files aren't re-downloaded."""
        wl = job.get("wordlist")
        if not wl:
            return None

        wl_id = wl["id"]
        name = wl.get("name") or f"wordlist_{wl_id}.txt"
        wl_cache = cache_dir / "wordlists" / f"{wl_id}_{name}"

        expected_bytes = wl.get("size")  # file_size_bytes from server
        if wl_cache.exists() and expected_bytes and wl_cache.stat().st_size == expected_bytes:
            logger.info("Job %d: wordlist %d cached at %s", job["id"], wl_id, wl_cache)
            return str(wl_cache)

        logger.info("Job %d: downloading wordlist %d (%s) ...", job["id"], wl_id, name)
        # Announce the download immediately so the server has data before any bytes arrive.
        # This ensures the dashboard shows the bar even if the WS wasn't connected when
        # the job started and had to reconnect.
        await self._ws.send_progress({
            "type": "download_progress",
            "job_id": job["id"],
            "filename": name,
            "bytes_done": 0,
            "bytes_total": expected_bytes or 0,
            "pct": 0.0,
        })
        progress_cb = self._make_dl_progress_cb(job["id"], name, expected_bytes or 0)
        await download_file(f"/api/v1/agent/files/wordlist/{wl_id}", wl_cache, on_progress=progress_cb)
        return str(wl_cache)

    async def _stage_rules(self, job: dict, cache_dir: Path) -> list[str]:
        """Download rule files; cached by ID."""
        rules = job.get("rules") or []
        paths = []
        for r in rules:
            r_id = r["id"]
            name = r.get("name") or f"rule_{r_id}.rule"
            r_cache = cache_dir / "rules" / f"{r_id}_{name}"
            if not r_cache.exists():
                logger.info("Job %d: downloading rule %d (%s) ...", job["id"], r_id, name)
                await download_file(f"/api/v1/agent/files/rule/{r_id}", r_cache)
            paths.append(str(r_cache))
        return paths
