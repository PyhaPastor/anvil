"""Async SQLAlchemy engine, session factory, and DB initialisation."""
from __future__ import annotations
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


engine = create_async_engine(
    settings.database.url,
    echo=settings.server.debug,
    # For SQLite — prevent "database is locked" under async concurrent access
    connect_args={"check_same_thread": False} if "sqlite" in settings.database.url else {},
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a DB session and ensures cleanup."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _migrate(conn) -> None:
    """
    Apply additive schema migrations that create_all() cannot handle
    (adding columns to already-existing tables).
    Each ALTER is wrapped in a try/except so it is idempotent — safe to run
    on every startup regardless of current schema version.
    """
    from sqlalchemy import text
    migrations = [
        # session 5: CPU/RAM utilization columns added to agent_health
        "ALTER TABLE agent_health ADD COLUMN cpu_util_pct REAL",
        "ALTER TABLE agent_health ADD COLUMN ram_used_mb  REAL",
        "ALTER TABLE agent_health ADD COLUMN ram_total_mb REAL",
        # session 6: disk space columns added to agent_health
        "ALTER TABLE agent_health ADD COLUMN disk_free_gb  REAL",
        "ALTER TABLE agent_health ADD COLUMN disk_total_gb REAL",
        # session 6: job phase tracking
        "ALTER TABLE jobs ADD COLUMN phase VARCHAR(32)",
        # session 7: absolute passwords-tested counter persisted from agent
        "ALTER TABLE jobs ADD COLUMN keys_tried INTEGER",
    ]
    for stmt in migrations:
        try:
            await conn.execute(text(stmt))
        except Exception:
            pass  # column already exists — safe to ignore


async def init_db() -> None:
    """Create all tables and seed the default admin user."""
    # Import all models so Base.metadata is populated
    from .models import user, customer, job, hash_list, agent, wordlist, template, audit, notification  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)

    # Seed default admin if no users exist
    async with AsyncSessionLocal() as session:
        from .models.user import User, UserRole
        from .services.auth_service import hash_password
        from sqlalchemy import select

        result = await session.execute(select(User).limit(1))
        if result.scalar_one_or_none() is None:
            admin = User(
                username="admin",
                email="admin@anvil.local",
                password_hash=hash_password("ChangeMe123!"),
                role=UserRole.ADMIN,
                is_active=True,
                force_password_change=True,
            )
            session.add(admin)
            await session.commit()
