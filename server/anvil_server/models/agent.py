"""Cracking agent model."""
from __future__ import annotations
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Hashed API token — use secrets.token_urlsafe(48) for raw token
    api_token_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # JSON blob: {gpus: [{name, vram_mb}, ...], cpu_count, os}
    capabilities: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # JSON list: [{name, size_bytes, wordlist_id}, ...] — reported by agent in heartbeat
    wordlist_cache: Mapped[list | None] = mapped_column(JSON, nullable=True)

    jobs = relationship("Job", back_populates="agent", lazy="noload")
    health_snapshots = relationship("AgentHealth", back_populates="agent", lazy="noload")

    @property
    def is_online(self) -> bool:
        from ..config import settings
        if self.last_seen is None:
            return False
        delta = (datetime.utcnow() - self.last_seen).total_seconds()
        return delta < settings.agent.heartbeat_timeout


class AgentHealth(Base):
    """Point-in-time hardware metrics from an agent."""
    __tablename__ = "agent_health"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    # GPU metrics (nullable if no GPU)
    gpu_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    gpu_util_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    gpu_mem_util_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # CPU / RAM / disk metrics
    cpu_util_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ram_used_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    ram_total_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_free_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_total_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Current hashrate for running job
    hashrate_hs: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Active job at time of snapshot
    current_job_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )

    agent = relationship("Agent", back_populates="health_snapshots", lazy="joined")
