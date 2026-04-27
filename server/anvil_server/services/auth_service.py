"""Authentication and authorisation service."""
from __future__ import annotations
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Request, status
import bcrypt as _bcrypt
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_db
from ..models.user import User, UserRole

ALGORITHM = "HS256"


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    salt = _bcrypt.gensalt(rounds=settings.security.bcrypt_rounds)
    return _bcrypt.hashpw(plain.encode(), salt).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def create_access_token(user_id: int, role: str, expires_delta: timedelta | None = None) -> str:
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=8))
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": expire,
        "iat": datetime.utcnow(),
        "jti": secrets.token_hex(16),  # unique token ID for future revocation support
    }
    return jwt.encode(payload, settings.server.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT. Raises HTTPException on failure."""
    try:
        payload = jwt.decode(token, settings.server.secret_key, algorithms=[ALGORITHM])
        if payload.get("sub") is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        return payload
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


def create_agent_token(agent_id: int) -> str:
    """Long-lived token for agent<->server authentication."""
    expire = datetime.utcnow() + timedelta(seconds=settings.agent.agent_token_expiry)
    payload = {
        "sub": str(agent_id),
        "type": "agent",
        "exp": expire,
        "iat": datetime.utcnow(),
        "jti": secrets.token_hex(16),
    }
    return jwt.encode(payload, settings.server.secret_key, algorithm=ALGORITHM)


def hash_agent_token(token: str) -> str:
    """Store a one-way hash of the agent token — never store raw tokens."""
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


def create_bootstrap_token(agent_id: int) -> str:
    """
    Short-lived token embedded in the bootstrap curl command.
    type='bootstrap', expires in 1 hour.
    The bootstrap endpoint verifies signature + type + expiry from the JWT itself
    — no extra DB column needed.
    """
    expire = datetime.utcnow() + timedelta(hours=1)
    payload = {
        "sub": str(agent_id),
        "type": "bootstrap",
        "exp": expire,
        "iat": datetime.utcnow(),
        "jti": secrets.token_hex(16),
    }
    return jwt.encode(payload, settings.server.secret_key, algorithm=ALGORITHM)


def verify_bootstrap_token(token: str) -> int:
    """
    Verify and decode a bootstrap token.
    Returns agent_id on success, raises HTTPException on failure.
    """
    try:
        payload = jwt.decode(token, settings.server.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Bootstrap token is invalid or has expired.")
    if payload.get("type") != "bootstrap":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Not a bootstrap token.")
    return int(payload["sub"])


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

async def _get_user_from_token(token: str, db: AsyncSession) -> User:
    payload = decode_access_token(token)
    if payload.get("type") == "agent":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Agent token cannot access UI")
    user_id = int(payload["sub"])
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract user from session cookie (browser UI flows)."""
    token = request.cookies.get("anvil_session")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
        )
    return await _get_user_from_token(token, db)


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    token = request.cookies.get("anvil_session")
    if not token:
        return None
    try:
        return await _get_user_from_token(token, db)
    except HTTPException:
        return None


def require_role(*roles: UserRole):
    """Dependency factory — enforces minimum role."""
    async def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required role: {[r.value for r in roles]}",
            )
        return user
    return _dep


require_admin    = require_role(UserRole.ADMIN)
require_analyst  = require_role(UserRole.ADMIN, UserRole.ANALYST)
require_viewer   = require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.VIEWER)
require_any_role = require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.VIEWER, UserRole.PRESENTATION)


# ---------------------------------------------------------------------------
# Agent token dependency (for agent API endpoints)
# ---------------------------------------------------------------------------

async def get_agent_from_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Validate agent Bearer token and return the Agent ORM object."""
    from ..models.agent import Agent
    import hashlib

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing agent token")

    raw_token = auth_header.split(" ", 1)[1]

    try:
        payload = jwt.decode(raw_token, settings.server.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent token")

    if payload.get("type") != "agent":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not an agent token")

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    result = await db.execute(
        select(Agent).where(Agent.api_token_hash == token_hash, Agent.is_active == True)
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Agent not registered or inactive")

    # Update last_seen
    agent.last_seen = datetime.utcnow()
    await db.commit()
    return agent
