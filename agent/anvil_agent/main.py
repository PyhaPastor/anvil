"""Anvil Agent — main entry point."""
from __future__ import annotations
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .config import settings
from .hardware_monitor import HardwareMonitor, get_capabilities
from .hashcat_wrapper import kill_stray_hashcat
from .job_runner import JobRunner, resubmit_pending_results
from .server_client import (
    AgentWSClient,
    auth_failures_exceeded,
    consecutive_auth_failures,
    get_next_job,
    get_wordlist_cache,
    post_capabilities,
    post_heartbeat,
)


def _setup_logging() -> None:
    log_cfg = settings.logging
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_cfg.file:
        from logging.handlers import RotatingFileHandler
        Path(log_cfg.file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            log_cfg.file,
            maxBytes=log_cfg.max_bytes,
            backupCount=log_cfg.backup_count,
        ))
    logging.basicConfig(
        level=getattr(logging, log_cfg.level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


logger = logging.getLogger("anvil.agent")


class AnvilAgent:
    def __init__(self) -> None:
        self._hw = HardwareMonitor()
        self._ws = AgentWSClient()
        self._runner = JobRunner(self._hw, self._ws)
        self._shutdown = asyncio.Event()
        self._auth_dead = False  # set when the server keeps rejecting our token

    def _check_auth_dead(self) -> None:
        """If the server has rejected our token too many times in a row, the agent
        was almost certainly re-provisioned (token rotated in the DB while this
        process holds the old one). Exit so systemd restarts us and reloads the
        fresh token from config.toml (Bug 13)."""
        if self._auth_dead or not auth_failures_exceeded():
            return
        self._auth_dead = True
        logger.critical(
            "Server rejected the agent token %d times in a row (HTTP 401). The agent "
            "was most likely re-provisioned, rotating its token while this process kept "
            "the old one. Exiting so systemd restarts the agent and reloads the new "
            "token from config.toml. If this persists, re-run the agent installer.",
            consecutive_auth_failures(),
        )
        self._shutdown.set()

    async def start(self) -> None:
        if not settings.agent.api_token:
            logger.critical(
                "api_token is not set in config.toml. "
                "Register this agent on the Anvil dashboard and paste the token."
            )
            sys.exit(1)

        logger.info("Anvil Agent '%s' starting...", settings.agent.name)
        logger.info("Server: %s", settings.agent.server_url)

        # Reap any hashcat left running by a previous agent instance (e.g. after a
        # crash/restart) so it can't keep a GPU pegged with no job managing it.
        try:
            n = kill_stray_hashcat()
            if n:
                logger.warning("Cleaned up %d stray hashcat process(es) from a previous run.", n)
        except Exception as exc:
            logger.warning("Stray-hashcat cleanup failed: %s", exc)

        # Register capabilities
        caps = get_capabilities()
        ok = await post_capabilities(caps)
        if ok:
            logger.info("Capabilities registered: %d GPU(s), %d CPU core(s)",
                        len(caps.get("gpus", [])), caps.get("cpu_count", 0))
        else:
            logger.warning("Could not register capabilities — server may be unreachable.")

        # Start background tasks
        await self._hw.start()
        await self._ws.connect()

        # Deliver any results a previous agent run persisted but never got the
        # server to confirm (killed mid-submit, or token rotated under it) — Bug 13.
        try:
            await resubmit_pending_results()
        except Exception as exc:
            logger.warning("Pending-result sweep at startup failed: %s", exc)

        # Install signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: self._shutdown.set())

        # Run poll + heartbeat loops concurrently
        try:
            await asyncio.gather(
                self._poll_loop(),
                self._heartbeat_loop(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self._hw.stop()
            await self._ws.disconnect()
            logger.info("Agent stopped.")

        # If we're shutting down because the server rejected our token, exit
        # non-zero so systemd (Restart=on-failure) restarts us with the fresh
        # token the installer wrote to config.toml (Bug 13).
        if self._auth_dead:
            sys.exit(1)

    async def _poll_loop(self) -> None:
        """Periodically check for a new job and run it."""
        while not self._shutdown.is_set():
            if not self._runner.is_busy:
                # While idle, retry any persisted-but-unconfirmed results (Bug 13).
                try:
                    await resubmit_pending_results()
                except Exception as exc:
                    logger.debug("Pending-result sweep failed: %s", exc)
                try:
                    job = await get_next_job()
                    if job:
                        logger.info("Picked up job %d", job["id"])
                        # Run job without blocking the poll loop for heartbeats
                        asyncio.create_task(
                            self._runner.run(job),
                            name=f"job_{job['id']}",
                        )
                except Exception as exc:
                    logger.error("Poll error: %s", exc)
                self._check_auth_dead()

            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=settings.agent.poll_interval,
                )
            except asyncio.TimeoutError:
                pass

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeat with live hardware metrics."""
        while not self._shutdown.is_set():
            try:
                metrics = await self._hw.get_metrics()
                gpu = metrics.gpus[0] if metrics.gpus else None
                payload = {
                    "gpu_temp_c": gpu.temp_c if gpu else None,
                    "gpu_util_pct": gpu.util_pct if gpu else None,
                    "gpu_mem_util_pct": gpu.mem_util_pct if gpu else None,
                    "cpu_util_pct": metrics.cpu_util_pct,
                    "ram_used_mb": metrics.ram_used_mb,
                    "ram_total_mb": metrics.ram_total_mb,
                    "disk_free_gb": metrics.disk_free_gb,
                    "disk_total_gb": metrics.disk_total_gb,
                    "hashrate_hs": metrics.current_hashrate_hs,
                    "current_job_id": metrics.current_job_id,
                    "phase": metrics.current_phase,
                    "wordlist_cache": get_wordlist_cache(),
                }
                await post_heartbeat(payload)
                self._check_auth_dead()
            except Exception as exc:
                logger.debug("Heartbeat error: %s", exc)

            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=settings.hardware.sample_interval * 5,
                )
            except asyncio.TimeoutError:
                pass


def main() -> None:
    _setup_logging()
    agent = AnvilAgent()
    asyncio.run(agent.start())


if __name__ == "__main__":
    main()
