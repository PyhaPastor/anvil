"""Anvil Agent configuration loader."""
from __future__ import annotations
import logging
import os
from pathlib import Path
from typing import Optional
import toml
from pydantic import BaseModel, field_validator

CONFIG_PATH = os.environ.get("ANVIL_AGENT_CONFIG", "./config.toml")
PERSISTENT_WORKDIR = "/var/lib/anvil-agent/workdir"


class AgentConfig(BaseModel):
    name: str = "Agent-01"
    server_url: str = "https://127.0.0.1:443"
    api_token: str = ""
    poll_interval: int = 5
    verify_tls: bool = True
    ca_bundle: Optional[str] = None

    @field_validator("server_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("poll_interval")
    @classmethod
    def min_poll(cls, v: int) -> int:
        return max(v, 2)


class HashcatConfig(BaseModel):
    binary: str = "/usr/bin/hashcat"
    extra_flags: str = ""
    workdir: str = PERSISTENT_WORKDIR
    potfile: str = ""
    cpu_fallback: bool = True       # use CPU device when no GPU is found
    # Restrict OpenCL device types: 1=CPU, 2=GPU, 4=all.  Empty = hashcat default.
    # Set "2" on GPU machines to force GPU OpenCL selection and skip the probe.
    opencl_device_types: str = ""

    @field_validator("workdir")
    @classmethod
    def reject_volatile_workdir(cls, v: str) -> str:
        # /tmp is wiped on reboot; the wordlist cache must survive restarts so
        # large downloads aren't re-fetched. Redirect with a loud warning rather
        # than crash so existing deployments keep running after upgrade.
        normalized = v.replace("\\", "/").rstrip("/") or "/"
        if normalized == "/tmp" or normalized.startswith("/tmp/"):
            logging.getLogger("anvil.agent.config").warning(
                "hashcat.workdir=%r is under /tmp (wiped on reboot). "
                "Using persistent fallback %s. Edit config.toml to silence this warning.",
                v, PERSISTENT_WORKDIR,
            )
            return PERSISTENT_WORKDIR
        return v


class HardwareConfig(BaseModel):
    sample_interval: int = 2
    gpu_backend: str = "gputil"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "./anvil-agent.log"
    max_bytes: int = 10 * 1024 * 1024   # 10 MB per file
    backup_count: int = 5               # keep 5 rotated files


class Settings(BaseModel):
    agent: AgentConfig
    hashcat: HashcatConfig = HashcatConfig()
    hardware: HardwareConfig = HardwareConfig()
    logging: LoggingConfig = LoggingConfig()


def load_settings() -> Settings:
    if not Path(CONFIG_PATH).exists():
        raise FileNotFoundError(f"Config not found: {CONFIG_PATH}")
    raw = toml.load(CONFIG_PATH)
    return Settings(**raw)


settings: Settings = load_settings()
