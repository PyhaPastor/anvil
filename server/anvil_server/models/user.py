"""User and role models."""
from __future__ import annotations
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"
    PRESENTATION = "presentation"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False, default=UserRole.VIEWER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    force_password_change: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    audit_entries = relationship("AuditLog", back_populates="user", lazy="noload")
    jobs_created = relationship("Job", back_populates="creator", lazy="noload")

    def can_create_jobs(self) -> bool:
        return self.role in (UserRole.ADMIN, UserRole.ANALYST)

    def can_view_credentials(self) -> bool:
        return self.role in (UserRole.ADMIN, UserRole.ANALYST, UserRole.VIEWER)

    def can_manage_users(self) -> bool:
        return self.role == UserRole.ADMIN

    def can_manage_agents(self) -> bool:
        return self.role == UserRole.ADMIN
