# Anvil Server package
import re
from pathlib import Path
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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
