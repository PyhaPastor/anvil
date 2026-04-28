"""Job runner — downloads required files then executes a single cracking job."""
from __future__ import annotations
import asyncio
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
            wrapper.cancel()  # no-op if already cancelled or process already exited
            self._ws.deregister_cancel(job_id)
            await self._hw.set_job_context(None, None)
            self._current_job_id = None
            # Clean up per-job hash list files (they can be large)
            if job_dir.exists():
                shutil.rmtree(job_dir, ignore_errors=True)

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

        ok = await submit_result(result_payload)
        if ok:
            logger.info(
                "Job %d complete: status=%s cracked=%d",
                job_id, final_status, len(cracked_entries),
            )
        else:
            logger.error("Job %d: failed to submit result to server", job_id)

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
