"""Hardware monitor — collects GPU temp, utilisation, and hashrate metrics."""
from __future__ import annotations
import asyncio
import logging
import platform
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

import psutil

try:
    import GPUtil as _GPUtil
except ImportError:
    _GPUtil = None

from .config import settings

logger = logging.getLogger("anvil.agent.hw")


@dataclass
class GPUMetrics:
    index: int
    name: str
    temp_c: Optional[float] = None
    util_pct: Optional[float] = None
    mem_util_pct: Optional[float] = None
    mem_used_mb: Optional[float] = None
    mem_total_mb: Optional[float] = None


@dataclass
class SystemMetrics:
    gpus: List[GPUMetrics] = field(default_factory=list)
    cpu_util_pct: float = 0.0
    ram_used_mb: float = 0.0
    ram_total_mb: float = 0.0
    disk_free_gb: float = 0.0
    disk_total_gb: float = 0.0
    # Filled in by job tracker
    current_hashrate_hs: Optional[float] = None
    current_job_id: Optional[int] = None
    current_phase: Optional[str] = None


def _collect_gputil() -> List[GPUMetrics]:
    try:
        if _GPUtil is None:
            return []
        gpus = _GPUtil.getGPUs()
        return [
            GPUMetrics(
                index=g.id,
                name=g.name,
                temp_c=g.temperature,
                util_pct=g.load * 100,
                mem_util_pct=(g.memoryUsed / g.memoryTotal * 100) if g.memoryTotal else None,
                mem_used_mb=g.memoryUsed,
                mem_total_mb=g.memoryTotal,
            )
            for g in gpus
        ]
    except Exception as exc:
        logger.debug("GPUtil collection failed: %s", exc)
        return []


def _collect_nvidia_smi() -> List[GPUMetrics]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,temperature.gpu,utilization.gpu,utilization.memory,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                continue
            try:
                gpus.append(GPUMetrics(
                    index=int(parts[0]),
                    name=parts[1],
                    temp_c=float(parts[2]),
                    util_pct=float(parts[3]),
                    mem_util_pct=float(parts[4]),
                    mem_used_mb=float(parts[5]),
                    mem_total_mb=float(parts[6]),
                ))
            except (ValueError, IndexError):
                continue
        return gpus
    except Exception as exc:
        logger.debug("nvidia-smi collection failed: %s", exc)
        return []


def collect_system_metrics() -> SystemMetrics:
    """Synchronous snapshot of current hardware state."""
    backend = settings.hardware.gpu_backend.lower()

    if backend == "nvidia-smi":
        gpus = _collect_nvidia_smi()
    elif backend == "gputil":
        gpus = _collect_gputil()
        if not gpus:
            # GPUtil failed or detected nothing — try nvidia-smi as fallback
            gpus = _collect_nvidia_smi()
    else:
        gpus = []

    vm = psutil.virtual_memory()
    try:
        import os
        disk = psutil.disk_usage(os.path.abspath(os.sep))
        disk_free_gb = disk.free / 1024 ** 3
        disk_total_gb = disk.total / 1024 ** 3
    except Exception:
        disk_free_gb = 0.0
        disk_total_gb = 0.0

    return SystemMetrics(
        gpus=gpus,
        cpu_util_pct=psutil.cpu_percent(interval=0.1),
        ram_used_mb=vm.used / 1024 / 1024,
        ram_total_mb=vm.total / 1024 / 1024,
        disk_free_gb=disk_free_gb,
        disk_total_gb=disk_total_gb,
    )


def get_capabilities() -> dict:
    """Return static hardware capabilities for agent registration."""
    backend = settings.hardware.gpu_backend.lower()
    if backend == "nvidia-smi":
        gpus = _collect_nvidia_smi() or _collect_gputil()
    else:
        gpus = _collect_gputil() or _collect_nvidia_smi()
    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu_count": psutil.cpu_count(logical=False) or 1,
        "gpus": [
            {"name": g.name, "vram_mb": g.mem_total_mb, "index": g.index}
            for g in gpus
        ],
    }


class HardwareMonitor:
    """Async background task that periodically collects metrics."""

    # EMA decay rates — fast up, slow down so brief 0-readings don't snap the display
    _EMA_ALPHA_UP   = 0.40   # weight for rising samples (responds quickly)
    _EMA_ALPHA_DOWN = 0.10   # weight for falling samples (decays slowly)

    def __init__(self) -> None:
        self._metrics = SystemMetrics()
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._ema_gpu_util: Optional[float] = None
        self._ema_gpu_temp: Optional[float] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="hw_monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                snapshot = await asyncio.get_event_loop().run_in_executor(
                    None, collect_system_metrics
                )
                # Apply EMA smoothing to GPU util/temp so brief 0-readings don't snap the value
                for gpu in snapshot.gpus:
                    if gpu.util_pct is not None:
                        if self._ema_gpu_util is None:
                            self._ema_gpu_util = gpu.util_pct
                        else:
                            alpha = (self._EMA_ALPHA_UP if gpu.util_pct >= self._ema_gpu_util
                                     else self._EMA_ALPHA_DOWN)
                            self._ema_gpu_util = alpha * gpu.util_pct + (1 - alpha) * self._ema_gpu_util
                        gpu.util_pct = self._ema_gpu_util
                    if gpu.temp_c is not None:
                        if self._ema_gpu_temp is None:
                            self._ema_gpu_temp = gpu.temp_c
                        else:
                            alpha = (self._EMA_ALPHA_UP if gpu.temp_c >= self._ema_gpu_temp
                                     else self._EMA_ALPHA_DOWN)
                            self._ema_gpu_temp = alpha * gpu.temp_c + (1 - alpha) * self._ema_gpu_temp
                        gpu.temp_c = self._ema_gpu_temp
                async with self._lock:
                    snapshot.current_hashrate_hs = self._metrics.current_hashrate_hs
                    snapshot.current_job_id = self._metrics.current_job_id
                    snapshot.current_phase = self._metrics.current_phase
                    self._metrics = snapshot
            except Exception as exc:
                logger.warning("HW monitor error: %s", exc)
            await asyncio.sleep(settings.hardware.sample_interval)

    async def get_metrics(self) -> SystemMetrics:
        async with self._lock:
            return self._metrics

    async def set_job_context(
        self, job_id: Optional[int], hashrate_hs: Optional[float],
        phase: Optional[str] = None,
    ) -> None:
        async with self._lock:
            self._metrics.current_job_id = job_id
            self._metrics.current_hashrate_hs = hashrate_hs
            self._metrics.current_phase = phase
