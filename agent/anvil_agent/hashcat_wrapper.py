"""
Hashcat wrapper — manages the hashcat subprocess, parses machine-readable
status output, and yields progress events to callers.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, List, Optional

from .config import settings

logger = logging.getLogger("anvil.agent.hashcat")

# Hashcat --machine-readable --status-json progress line pattern
_STATUS_JSON_RE = re.compile(r"^\{.*\"status\".*\}$")
# Hashcat cracked line: hash:plaintext or user:hash:plaintext
_CRACKED_RE = re.compile(r"^(.+?):(.+)$")


@dataclass
class CrackResult:
    hash_value: str
    plaintext: str
    time_to_crack_seconds: Optional[float] = None


@dataclass
class ProgressEvent:
    progress_pct: float = 0.0
    hashrate_hs: float = 0.0
    eta_seconds: Optional[int] = None
    recovered: int = 0
    total: int = 0
    keys_tried: int = 0        # absolute number of passwords tested so far
    status: str = "running"    # running | exhausted | cracked | aborted | error
    cracked: List[CrackResult] = field(default_factory=list)
    raw_status: Optional[dict] = None
    error: Optional[str] = None
    current_candidate: Optional[str] = None


class HashcatWrapper:
    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass

    async def run(
        self,
        job_id: int,
        attack_mode: int,
        hash_type: Optional[int],
        hash_list_files: List[str],
        wordlist_path: Optional[str],
        rule_paths: List[str],
        mask: Optional[str],
        extra_flags: Optional[str],
        started_at_ts: float,
    ) -> AsyncIterator[ProgressEvent]:
        self._cancelled = False

        # Resolve binary
        binary = settings.hashcat.binary
        if not shutil.which(binary):
            yield ProgressEvent(status="error", error=f"hashcat binary not found: {binary}")
            return

        # Build output file for cracked results
        workdir = Path(settings.hashcat.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        outfile = workdir / f"job_{job_id}.potfile"

        # Pre-create XDG dirs under our workdir.
        # - XDG_DATA_HOME: hashcat session .pid / .induct files
        # - XDG_CACHE_HOME: pocl OpenCL kernel cache — pocl silently fails to
        #   initialise (no devices) if it cannot write here, which happens when
        #   the service user has no home directory (--no-create-home).
        # - fake_home / .nv: NVIDIA OpenCL runtime kernel cache; without a
        #   writable HOME the NVIDIA ICD silently enumerates zero devices.
        xdg_root       = workdir / "xdg"
        xdg_data_home  = xdg_root / "data"
        xdg_cache_home = xdg_root / "cache"
        fake_home      = xdg_root / "home"
        (xdg_data_home  / "hashcat" / "sessions").mkdir(parents=True, exist_ok=True)
        (xdg_cache_home / "pocl").mkdir(parents=True, exist_ok=True)
        (fake_home / ".nv" / "ComputeCache").mkdir(parents=True, exist_ok=True)

        # Auto-detect hash type if not specified
        effective_hash_type = hash_type
        if effective_hash_type is None:
            effective_hash_type = await self._identify_hash_type(
                binary, hash_list_files,
                xdg_data_home=str(xdg_data_home),
                xdg_cache_home=str(xdg_cache_home),
            )
            if effective_hash_type is None:
                yield ProgressEvent(
                    status="error",
                    error="Could not auto-detect hash type. Please specify manually.",
                )
                return
            logger.info("Job %d: auto-detected hash type %d", job_id, effective_hash_type)

        # Probe for GPU devices; fall back to CPU virtual device if none found.
        # Skip the probe entirely if the user has explicitly configured which
        # OpenCL device types to use — they know their hardware better than the probe.
        use_cpu_fallback = False
        if settings.hashcat.cpu_fallback and not settings.hashcat.opencl_device_types:
            has_gpu = await self._probe_has_gpu(
                binary,
                xdg_data_home=str(xdg_data_home),
                xdg_cache_home=str(xdg_cache_home),
                home=str(fake_home),
            )
            if not has_gpu:
                use_cpu_fallback = True
                logger.warning(
                    "Job %d: no GPU devices found — using CPU fallback (-D 1). "
                    "Performance will be limited. "
                    "If this machine has a GPU, set opencl_device_types = \"2\" in config.toml "
                    "to skip this probe and force GPU selection.",
                    job_id,
                )

        cmd = self._build_command(
            binary=binary,
            attack_mode=attack_mode,
            hash_type=effective_hash_type,
            hash_list_files=hash_list_files,
            wordlist_path=wordlist_path,
            rule_paths=rule_paths,
            mask=mask,
            extra_flags=extra_flags,
            outfile=str(outfile),
            cpu_fallback=use_cpu_fallback,
        )

        logger.info("Job %d: launching hashcat: %s", job_id, " ".join(shlex.quote(a) for a in cmd))

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._safe_env(
                    xdg_data_home=str(xdg_data_home),
                    xdg_cache_home=str(xdg_cache_home),
                    home=str(fake_home),
                ),
            )
        except OSError as exc:
            yield ProgressEvent(status="error", error=str(exc))
            return

        # Collect stderr concurrently so it doesn't block the process
        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            assert self._proc.stderr is not None
            while True:
                chunk = await self._proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        stderr_task = asyncio.create_task(_drain_stderr())

        try:
            async for event in self._stream_output(self._proc, started_at_ts):
                if self._cancelled:
                    self._proc.terminate()
                    await stderr_task
                    yield ProgressEvent(status="aborted")
                    return
                yield event

            await stderr_task
        finally:
            # Ensure the subprocess is dead regardless of how the generator exits
            # (normal completion, exception, or caller dropping the async-for loop).
            if self._proc.returncode is None:
                try:
                    self._proc.terminate()
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except (ProcessLookupError, asyncio.TimeoutError):
                    try:
                        self._proc.kill()
                    except ProcessLookupError:
                        pass

        # Collect final cracked results from outfile
        cracked = self._parse_outfile(outfile)
        rc = self._proc.returncode

        # Hashcat exit codes:
        #   0 — OK (some hashes cracked)
        #   1 — Warning (no hashes cracked, but ran to completion — treat as exhausted)
        #   2 — Error (bad args, file not found, etc.)
        #   3 — Aborted by user
        # Any other non-zero code is also an error.
        if self._cancelled:
            final_status = "aborted"
            error_msg = None
        elif rc in (0, 1):
            # 0 = cracked something, 1 = ran to exhaustion without cracking
            final_status = "exhausted" if rc == 1 else "completed"
            error_msg = None
        else:
            final_status = "error"
            stderr_text = b"".join(stderr_chunks).decode(errors="replace").strip()
            error_msg = stderr_text or f"hashcat exited with code {rc}"
            logger.error("Job %d: hashcat rc=%d stderr=%s", job_id, rc, stderr_text[:500])

        yield ProgressEvent(
            progress_pct=100.0 if rc in (0, 1) else 0.0,
            status=final_status,
            cracked=cracked,
            error=error_msg,
        )

    def _build_command(
        self,
        binary: str,
        attack_mode: int,
        hash_type: int,
        hash_list_files: List[str],
        wordlist_path: Optional[str],
        rule_paths: List[str],
        mask: Optional[str],
        extra_flags: Optional[str],
        outfile: str,
        cpu_fallback: bool = False,
    ) -> List[str]:
        cmd = [
            binary,
            f"--attack-mode={attack_mode}",
            f"--hash-type={hash_type}",
            "--status",
            "--status-timer=1",
            "--status-json",
            "--quiet",
            "--potfile-disable",         # we manage cracked output ourselves
            f"--outfile={outfile}",
            "--outfile-format=2",        # hash:plain
        ]

        # No GPU found — restrict hashcat to OpenCL CPU devices (type 1).
        # Requires pocl-opencl-icd (or another CPU OpenCL runtime) to be installed.
        if cpu_fallback:
            cmd.extend(["-D", "1"])

        if settings.hashcat.opencl_device_types:
            cmd.extend(["--opencl-device-types", settings.hashcat.opencl_device_types])

        if settings.hashcat.potfile:
            cmd.append(f"--potfile-path={settings.hashcat.potfile}")

        # Hash list(s)
        cmd.extend(hash_list_files)

        # Attack-mode specific positional arguments
        if attack_mode == 0 and wordlist_path:      # dictionary
            cmd.append(wordlist_path)
        elif attack_mode == 1 and wordlist_path:     # combinator (needs 2 wordlists; reuse same)
            cmd.extend([wordlist_path, wordlist_path])
        elif attack_mode == 3 and mask:              # brute-force mask
            cmd.append(mask)
        elif attack_mode in (6, 7) and wordlist_path and mask:
            if attack_mode == 6:
                cmd.extend([wordlist_path, mask])
            else:
                cmd.extend([mask, wordlist_path])

        # Rule files
        for rule_path in rule_paths:
            cmd.extend(["--rules-file", rule_path])

        # Global extra flags from server config
        _global_ef = (settings.hashcat.extra_flags or "").strip()
        if _global_ef:
            cmd.extend(shlex.split(_global_ef))

        # Per-job extra flags (already validated server-side)
        # Guard against Jinja rendering Python None as the literal string "None"
        _job_ef = (extra_flags or "").strip()
        if _job_ef and _job_ef.lower() not in ("none", "null"):
            cmd.extend(shlex.split(_job_ef))

        return cmd

    async def _probe_has_gpu(
        self, binary: str, xdg_data_home: str = "", xdg_cache_home: str = "", home: str = ""
    ) -> bool:
        """
        Run `hashcat -I` (backend info) and return True if at least one GPU
        device is listed.  Falls back to False on any error so the CPU path
        is used safely.
        """
        try:
            probe_cmd = [binary, "-I"]
            if settings.hashcat.opencl_device_types:
                probe_cmd.extend(["--opencl-device-types", settings.hashcat.opencl_device_types])

            proc = await asyncio.create_subprocess_exec(
                *probe_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._safe_env(
                    xdg_data_home=xdg_data_home,
                    xdg_cache_home=xdg_cache_home,
                    home=home,
                ),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            output = stdout.decode(errors="replace")
            if stderr:
                logger.debug("hashcat -I stderr: %s", stderr.decode(errors="replace")[:500])
            logger.debug("hashcat -I stdout: %s", output[:1000])

            # hashcat -I prints "Type...: GPU" for GPU devices
            for line in output.splitlines():
                stripped = line.strip()
                if re.match(r"Type[\.\s]+:\s+GPU", stripped, re.IGNORECASE):
                    return True
            return False
        except Exception as exc:
            logger.debug("hashcat -I probe failed: %s", exc)
            return False

    async def _identify_hash_type(
        self, binary: str, hash_list_files: List[str],
        xdg_data_home: str = "", xdg_cache_home: str = ""
    ) -> Optional[int]:
        """Try hashcat --identify to auto-detect hash type."""
        if not hash_list_files:
            return None
        try:
            identify_cmd = [binary, "--identify", hash_list_files[0]]
            proc = await asyncio.create_subprocess_exec(
                *identify_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._safe_env(
                    xdg_data_home=xdg_data_home,
                    xdg_cache_home=xdg_cache_home,
                ),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            for line in stdout.decode(errors="replace").splitlines():
                # hashcat --identify outputs: "<mode>  <name>  <example>"
                parts = line.strip().split()
                if parts and parts[0].isdigit():
                    return int(parts[0])
        except Exception as exc:
            logger.debug("hashcat --identify failed: %s", exc)
        return None

    async def _stream_output(
        self, proc: asyncio.subprocess.Process, started_at_ts: float
    ) -> AsyncIterator[ProgressEvent]:
        assert proc.stdout is not None

        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    break
                continue

            if not line:
                if proc.returncode is not None:
                    break
                await asyncio.sleep(0.1)
                continue

            decoded = line.decode(errors="replace").strip()
            if not decoded:
                continue

            # Machine-readable status JSON line
            if _STATUS_JSON_RE.match(decoded):
                try:
                    data = json.loads(decoded)
                    logger.debug("hashcat JSON keys: %s", list(data.keys()))
                    yield self._parse_status_json(data, started_at_ts)
                except json.JSONDecodeError as exc:
                    logger.warning("Failed to parse hashcat JSON: %s | line: %s", exc, decoded[:200])
            else:
                logger.debug("hashcat: %s", decoded)

    @staticmethod
    def _parse_status_json(data: dict, started_at_ts: float) -> ProgressEvent:
        # hashcat 6.x nests guess fields under a "guess" sub-object; older/other
        # builds put them at the top level — support both layouts.
        guess = data.get("guess") if isinstance(data.get("guess"), dict) else data

        # ── Progress ─────────────────────────────────────────────────────────
        # Prefer guess_base_count/guess_base_count_total (either nested or flat)
        g_count = guess.get("guess_base_count") or data.get("guess_base_count") or 0
        g_total = guess.get("guess_base_count_total") or data.get("guess_base_count_total") or 0
        if not g_total:
            # Fall back to top-level "progress": [done, total]
            prog = data.get("progress")
            if isinstance(prog, list) and len(prog) >= 2:
                g_count, g_total = prog[0], prog[1]
        progress_pct = min(g_count / max(g_total, 1) * 100, 100.0)

        # ── Speed ────────────────────────────────────────────────────────────
        # hashcat uses "devices" in 6.x; older builds used "speed_dev"
        speed_all = data.get("devices") or data.get("speed_dev") or []
        total_hs = sum(s.get("speed", 0) for s in speed_all)

        # ── ETA ──────────────────────────────────────────────────────────────
        eta = None
        msec_estimated = data.get("estimated_stop", 0)
        if msec_estimated:
            remaining = (msec_estimated / 1000) - time.time()
            eta = max(0, int(remaining))

        # ── Status code ──────────────────────────────────────────────────────
        status_code = data.get("status", 0)
        status_map = {
            1: "running", 2: "exhausted", 3: "cracked",
            4: "aborted", 5: "quit", 6: "bypass",
        }
        status = status_map.get(status_code, "running")

        # ── Recovered hashes ─────────────────────────────────────────────────
        recovered_total = data.get("recovered_hashes", [0, 0])
        recovered = recovered_total[0] if isinstance(recovered_total, list) else 0
        total = recovered_total[1] if isinstance(recovered_total, list) and len(recovered_total) > 1 else 0

        # ── Current candidate ────────────────────────────────────────────────
        # For dictionary attacks, guess_base is the wordlist FILE PATH, not a word.
        # Only use it as a displayable candidate if it looks like an actual word
        # (no path separators, no common file extensions).
        raw_base = guess.get("guess_base") or data.get("guess_base") or ""
        mod = guess.get("guess_mod") or data.get("guess_mod") or ""
        _is_path = (
            raw_base.startswith("/") or raw_base.startswith("\\")
            or ("/" in raw_base and "." in raw_base.split("/")[-1])
            or ("\\" in raw_base)
        )
        if _is_path:
            # For rule attacks, the mod IS the actual candidate word being tested
            candidate = mod if (mod and not mod.startswith("/") and "\\" not in mod) else ""
        else:
            candidate = raw_base
            if candidate and mod and mod != candidate and not mod.startswith("/"):
                candidate = f"{candidate} → {mod}"

        logger.debug(
            "status_json: status=%d progress=%.2f%% hs=%.0f candidate=%r",
            status_code, progress_pct, total_hs, candidate,
        )

        return ProgressEvent(
            progress_pct=round(progress_pct, 2),
            hashrate_hs=total_hs,
            eta_seconds=eta,
            recovered=recovered,
            total=total,
            keys_tried=int(g_count),
            status=status,
            raw_status=data,
            current_candidate=candidate or None,
        )

    @staticmethod
    def _parse_outfile(outfile: Path) -> List[CrackResult]:
        if not outfile.exists():
            return []
        results = []
        try:
            with open(outfile, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.rstrip("\n")
                    m = _CRACKED_RE.match(line)
                    if m:
                        results.append(CrackResult(
                            hash_value=m.group(1),
                            plaintext=m.group(2),
                        ))
        except OSError:
            pass
        return results

    @staticmethod
    def _safe_env(xdg_data_home: str = "", xdg_cache_home: str = "", home: str = "") -> dict:
        """
        Environment for hashcat subprocess.
        Inherits the full process environment (needed by GPU OpenCL runtimes)
        and overrides specific vars to point at writable dirs under workdir.
        Strips vars that could allow output-redirection attacks.
        """
        # Strip only genuinely dangerous shell vars; keep everything else so
        # GPU drivers (NVIDIA/AMD OpenCL, CUDA) get the env they need.
        strip = {"LD_PRELOAD", "LD_AUDIT", "PYTHONPATH", "PYTHONSTARTUP",
                 "PYTHONINSPECT", "PYTHONASYNCIODEBUG"}
        env = {k: v for k, v in os.environ.items() if k not in strip}
        # XDG_DATA_HOME  — hashcat session .pid/.induct files
        # XDG_CACHE_HOME — pocl kernel cache; pocl silently produces zero devices
        #                  if it cannot write here (service user has no home dir)
        if xdg_data_home:
            env["XDG_DATA_HOME"] = xdg_data_home
        if xdg_cache_home:
            env["XDG_CACHE_HOME"] = xdg_cache_home
        # HOME — NVIDIA OpenCL runtime writes kernel cache to ~/.nv/ComputeCache;
        #        without a writable HOME it silently enumerates zero GPU devices.
        if home:
            env["HOME"] = home
        return env
