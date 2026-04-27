"""Hash list and individual hash models."""
from __future__ import annotations
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class HashList(Base):
    __tablename__ = "hash_lists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)  # server-side path
    total_hashes: Mapped[int] = mapped_column(Integer, default=0)
    cracked_count: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    job = relationship("Job", back_populates="hash_lists", lazy="joined")
    hashes = relationship("Hash", back_populates="hash_list", lazy="noload")

    @property
    def crack_rate_pct(self) -> float:
        if self.total_hashes == 0:
            return 0.0
        return round(self.cracked_count / self.total_hashes * 100, 1)


class Hash(Base):
    """Individual hash record — stores hash value and cracked plaintext."""
    __tablename__ = "hashes"
    __table_args__ = (
        Index("ix_hashes_hashlist_hash", "hash_list_id", "hash_value"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    hash_list_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("hash_lists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Username associated with the hash (e.g. from NTLM dump), may be null
    username: Mapped[str | None] = mapped_column(String(256), nullable=True)
    hash_value: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL until cracked
    plaintext: Mapped[str | None] = mapped_column(Text, nullable=True)
    cracked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    time_to_crack_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    hash_list = relationship("HashList", back_populates="hashes", lazy="joined")
