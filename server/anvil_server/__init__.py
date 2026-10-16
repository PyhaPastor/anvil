# Anvil Server package
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# strftime patterns per format token used by the `localdt` filter.
_LOCALDT_PATTERNS = {
    "datetime": "%Y-%m-%d %H:%M",
    "datetime-sec": "%Y-%m-%d %H:%M:%S",
    "date": "%Y-%m-%d",
    "time": "%H:%M:%S",
}


def _localdt(value, fmt: str = "datetime", none: str = "—") -> str:
    """
    Render a stored (naive UTC) datetime in the configured display timezone.

    Timestamps are persisted as naive UTC (SQLite func.now() / datetime.utcnow()),
    so we attach UTC, convert to settings.server.display_timezone, then format.
    Any failure (unknown zone, missing tzdata) falls back to rendering in UTC so
    a page never 500s over a timestamp.
    """
    if value is None:
        return none
    pattern = _LOCALDT_PATTERNS.get(fmt, _LOCALDT_PATTERNS["datetime"])
    if not isinstance(value, datetime):
        # Not a datetime (already a string, etc.) — return as-is.
        return str(value)
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    from .config import settings
    try:
        local = aware.astimezone(ZoneInfo(settings.server.display_timezone))
    except Exception:
        local = aware  # fall back to UTC
    return local.strftime(pattern)


def _shorten_gpu_name(name: str) -> str:
    """Strip vendor prefixes so 'NVIDIA NVIDIA GeForce RTX 5090' → 'RTX 5090'."""
    if not name:
        return name
    s = name.strip()
    # Collapse repeated NVIDIA/AMD prefixes
    s = re.sub(r'\b(NVIDIA\s+){2,}', 'NVIDIA ', s)
    s = re.sub(r'\b(AMD\s+){2,}', 'AMD ', s)
    # Strip "NVIDIA GeForce/Quadro/Tesla" prefix
    s = re.sub(r'^NVIDIA\s+(?:GeForce|Quadro|Tesla)\s+', '', s)
    # Strip "AMD Radeon" prefix (keep RX/Pro/Vega/etc.)
    s = re.sub(r'^AMD\s+Radeon\s+', '', s)
    return s.strip() or name


templates.env.filters["shorten_gpu"] = _shorten_gpu_name
templates.env.filters["localdt"] = _localdt
