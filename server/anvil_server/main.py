"""Anvil Server — FastAPI application entry point."""
from __future__ import annotations
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .config import settings, CONFIG_PATH
from .database import init_db
from .services.tls_service import ensure_tls_cert
from . import templates as _templates

logger = logging.getLogger("anvil.server")

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address, default_limits=["300/minute"])


# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"]    = "nosniff"
        response.headers["X-Frame-Options"]           = "DENY"
        response.headers["X-XSS-Protection"]          = "1; mode=block"
        response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]        = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"]   = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' cdn.jsdelivr.net unpkg.com; "
            "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
            "img-src 'self' data:; "
            "connect-src 'self' wss: cdn.jsdelivr.net; "
            "font-src 'self' data:; "
            "frame-ancestors 'none';"
        )
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains; preload"
        )
        return response


# ---------------------------------------------------------------------------
# Request timing middleware (dev/debug only)
# ---------------------------------------------------------------------------
class TimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - start) * 1000
        response.headers["X-Process-Time-Ms"] = f"{elapsed:.1f}"
        return response


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — configure logging
    log_cfg = settings.logging
    log_level = logging.DEBUG if settings.server.debug else getattr(logging, log_cfg.level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_cfg.file:
        from logging.handlers import RotatingFileHandler
        Path(log_cfg.file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            log_cfg.file,
            maxBytes=log_cfg.max_bytes,
            backupCount=log_cfg.backup_count,
        ))
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    logger.info("Starting Anvil server...")

    # Ensure TLS cert exists and covers all configured SANs
    if settings.tls.mode == "self_signed":
        ensure_tls_cert(settings.tls.cert_file, settings.tls.key_file, settings.tls.extra_sans)

    # Initialise database and seed defaults
    await init_db()
    logger.info("Database ready.")

    # Auto-generate provisioning key if missing (handles servers upgraded from older versions
    # that didn't have this config key, or fresh installs where setup.sh wasn't re-run)
    if not settings.agent.provisioning_key:
        import secrets as _secrets
        import re as _re
        new_key = _secrets.token_urlsafe(48)
        settings.agent.provisioning_key = new_key
        config_path = Path(CONFIG_PATH)
        try:
            content = config_path.read_text()
            # Only write if still empty on disk (avoid stomping another worker that beat us)
            if not _re.search(r'^provisioning_key\s*=\s*".+"', content, _re.MULTILINE):
                if _re.search(r"^provisioning_key\s*=", content, _re.MULTILINE):
                    content = _re.sub(
                        r'^provisioning_key\s*=.*$',
                        f'provisioning_key = "{new_key}"',
                        content, flags=_re.MULTILINE,
                    )
                elif _re.search(r"^\[agent\]", content, _re.MULTILINE):
                    content = _re.sub(
                        r"(\[agent\])", rf'\1\nprovisioning_key = "{new_key}"',
                        content, flags=_re.MULTILINE,
                    )
                else:
                    content += f'\n[agent]\nprovisioning_key = "{new_key}"\n'
                config_path.write_text(content)
                logger.info("Auto-generated agent provisioning key and saved to config.toml.")
        except Exception as exc:
            logger.warning("Could not persist provisioning key to config.toml: %s", exc)

    yield

    # Shutdown
    logger.info("Anvil server shutting down.")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(
        title="Anvil",
        description="Hash cracking management platform",
        version="1.0.0",
        docs_url="/api/docs" if settings.server.debug else None,
        redoc_url="/api/redoc" if settings.server.debug else None,
        openapi_url="/api/openapi.json" if settings.server.debug else None,
        lifespan=lifespan,
    )

    # ---- Middleware (order matters — outermost first) ----
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TimingMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.server.secret_key,
        session_cookie="anvil_session",
        max_age=settings.server.session_max_age,
        same_site="strict",
        https_only=True,
    )

    # ---- Rate limiter ----
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ---- Static files ----
    static_path = Path(__file__).parent / "static"
    static_path.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    # ---- Routers ----
    from .routers import auth, dashboard, jobs, customers, users, agents, wordlists, audit
    from .routers import templates_router, websocket, settings_router
    from .routers.api import agent_api

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(jobs.router)
    app.include_router(customers.router)
    app.include_router(users.router)
    app.include_router(agents.router)
    app.include_router(wordlists.router)
    app.include_router(templates_router.router)
    app.include_router(audit.router)
    app.include_router(websocket.router)
    app.include_router(agent_api.router)
    app.include_router(settings_router.router)

    # ---- Root redirect ----
    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/dashboard", status_code=302)

    # ---- Global exception handlers ----
    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        return _templates.TemplateResponse(request, "errors/404.html", status_code=404)

    @app.exception_handler(403)
    async def forbidden(request: Request, exc):
        return _templates.TemplateResponse(request, "errors/403.html", status_code=403)

    @app.exception_handler(500)
    async def server_error(request: Request, exc):
        logger.exception("Unhandled server error: %s", exc)
        return JSONResponse(
            {"detail": "Internal server error"},
            status_code=500,
        )

    return app


app = create_app()
