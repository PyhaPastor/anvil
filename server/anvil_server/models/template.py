"""Job template / attack profile model."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from ..database import Base


class JobTemplate(Base):
    __tablename__ = "job_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    attack_mode: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hash_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wordlist_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("wordlists.id", ondelete="SET NULL"), nullable=True
    )
    # List of rule IDs
    rules_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    mask: Mapped[str | None] = mapped_column(String(256), nullable=True)
    extra_flags: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
