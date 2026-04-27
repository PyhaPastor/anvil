"""Secure file upload helpers — validates type, size, and filename."""
from __future__ import annotations
import hashlib
import re
import unicodedata
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from ..config import settings


# Allowed MIME types for wordlists and hash lists
_ALLOWED_MIME = {
    "text/plain",
    "application/octet-stream",  # many tools produce this for .hash files
}

_UNSAFE_FILENAME_RE = re.compile(r"[^\w\.\-]")


def sanitise_filename(name: str) -> str:
    """Return a safe filename, stripping path traversal and special chars."""
    # Normalise unicode, strip non-ascii
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    # Remove path separators entirely
    name = name.replace("/", "").replace("\\", "").replace("..", "")
    # Replace any remaining unsafe characters
    name = _UNSAFE_FILENAME_RE.sub("_", name)
    name = name.strip("._")
    return name[:255] or "upload"


async def save_upload(
    upload: UploadFile,
    dest_dir: str,
    max_bytes: int,
    allowed_extensions: list[str],
) -> tuple[str, int]:
    """
    Stream-read, validate, and save an uploaded file.
    Returns (saved_path, file_size_bytes).
    Raises HTTPException on any validation failure.
    """
    if not upload.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No filename provided")

    safe_name = sanitise_filename(upload.filename)
    suffix = Path(safe_name).suffix.lower()
    if allowed_extensions and suffix not in allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"File extension '{suffix}' not allowed. Allowed: {allowed_extensions}",
        )

    dest = Path(dest_dir) / safe_name

    # Defense-in-depth: resolve the real path and confirm it stays inside dest_dir
    # This catches any edge cases the sanitiser misses (symlinks, unusual unicode, etc.)
    resolved_dest = dest.resolve()
    resolved_dir = Path(dest_dir).resolve()
    try:
        resolved_dest.relative_to(resolved_dir)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename: path traversal detected",
        )

    # Avoid clobbering existing files
    if dest.exists():
        stem = dest.stem
        ext = dest.suffix
        counter = 1
        while dest.exists():
            dest = Path(dest_dir) / f"{stem}_{counter}{ext}"
            counter += 1

    total = 0
    sha = hashlib.sha256()
    CHUNK = 65_536  # 64 KB

    try:
        with open(dest, "wb") as f:
            while True:
                chunk = await upload.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File exceeds maximum size of {max_bytes} bytes",
                    )
                sha.update(chunk)
                f.write(chunk)
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save file: {exc}",
        )

    return str(dest), total
