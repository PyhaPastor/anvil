"""Hash cracking job model."""
from __future__ import annotations
import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class JobStatus(str, enum.Enum):
    PENDING   = "pending"
    QUEUED    = "queued"
    RUNNING   = "running"
    PAUSED    = "paused"
    COMPLETED = "completed"
    FAILED    = "failed"
    CANCELLED = "cancelled"


class AttackMode(int, enum.Enum):
    DICTIONARY    = 0
    COMBINATOR    = 1
    MASK          = 3
    HYBRID_WM     = 6   # wordlist + mask
    HYBRID_MW     = 7   # mask + wordlist
    ASSOCIATION   = 9


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    customer_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    agent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    template_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("job_templates.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), nullable=False, default=JobStatus.PENDING, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=5)  # 1 (high) – 10 (low)

    # Hashcat parameters
    attack_mode: Mapped[int] = mapped_column(Integer, nullable=False, default=AttackMode.DICTIONARY)
    hash_type: Mapped[int | None] = mapped_column(Integer, nullable=True)   # None = auto-detect
    hash_type_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    wordlist_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("wordlists.id", ondelete="SET NULL"), nullable=True
    )
    rules_json: Mapped[list | None] = mapped_column(JSON, nullable=True)   # list of rule file IDs
    mask: Mapped[str | None] = mapped_column(String(256), nullable=True)
    extra_flags: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Timing
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Progress (populated by agent heartbeats)
    progress_pct: Mapped[float] = mapped_column(Float, default=0.0)
    hashrate: Mapped[float | None] = mapped_column(Float, nullable=True)  # H/s
    eta_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keys_tried: Mapped[int | None] = mapped_column(Integer, nullable=True)  # absolute passwords tested
    phase: Mapped[str | None] = mapped_column(String(32), nullable=True)  # downloading | cracking

    # Results summary
    total_hashes: Mapped[int] = mapped_column(Integer, default=0)
    cracked_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    customer = relationship("Customer", back_populates="jobs", lazy="joined")
    creator = relationship("User", back_populates="jobs_created", lazy="joined")
    agent = relationship("Agent", back_populates="jobs", lazy="joined")
    hash_lists = relationship("HashList", back_populates="job", lazy="noload")

    @property
    def crack_rate_pct(self) -> float:
        if self.total_hashes == 0:
            return 0.0
        return round(self.cracked_count / self.total_hashes * 100, 1)

    @property
    def duration_seconds(self) -> Optional[int]:
        if self.started_at is None:
            return None
        end = self.completed_at or datetime.utcnow()
        return int((end - self.started_at).total_seconds())
