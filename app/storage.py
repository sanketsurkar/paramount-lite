"""
app/storage.py
--------------
Where uploaded course notes live.

* Locally (development): files go to instance/uploads/<key>.
* Online (production): Supabase Storage, called with plain HTTP via httpx.
  Set SUPABASE_URL, SUPABASE_SERVICE_KEY and SUPABASE_BUCKET (a PRIVATE bucket).

Files are always served THROUGH our app (/files/<id>), never by a public
URL, so permissions are checked and the service worker can cache them.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

import httpx

from app.config import UPLOAD_DIR, settings, utcnow

logger = logging.getLogger("paramount.storage")


class StorageError(Exception):
    """Storage failed; the message is safe to show to the user."""


def detect_file_type(head: bytes) -> tuple[str, str, str] | None:
    """
    Identify a file by its first bytes ("magic number"), NOT by its name or the
    browser's claim. Returns (kind, mime type, extension) or None if not allowed.
    """
    if head.startswith(b"%PDF-"):
        return "pdf", "application/pdf", ".pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png", ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg", ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "image/webp", ".webp"
    return None


def new_key(extension: str) -> str:
    """Random, unguessable storage key, e.g. notes/2026/10/3f2a...e1.pdf"""
    now = utcnow()
    return f"notes/{now:%Y/%m}/{uuid.uuid4().hex}{extension}"


# ---------------------------------------------------------------------------
# Local disk
# ---------------------------------------------------------------------------
def _local_path(key: str) -> Path:
    root = UPLOAD_DIR.resolve()
    path = (root / key).resolve()
    if root not in path.parents:          # blocks "../" tricks
        raise StorageError("Invalid file location.")
    return path


# ---------------------------------------------------------------------------
# Supabase Storage (REST API)
# ---------------------------------------------------------------------------
def _supabase_object_url(key: str) -> str:
    return f"{settings.supabase_url}/storage/v1/object/{settings.supabase_bucket}/{key}"


def _supabase_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {settings.supabase_key}",
        "apikey": settings.supabase_key,
    }
    headers.update(extra or {})
    return headers


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------
def save(key: str, data: bytes, mime: str) -> None:
    if settings.use_supabase:
        try:
            response = httpx.post(
                _supabase_object_url(key),
                content=data,
                headers=_supabase_headers({"Content-Type": mime, "x-upsert": "false"}),
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise StorageError("Could not reach file storage. Please try again.") from exc
        if response.status_code >= 300:
            logger.error("Supabase upload failed: %s %s", response.status_code, response.text[:300])
            raise StorageError("File storage refused the upload. Check the Supabase settings.")
        return
    path = _local_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def load(key: str) -> bytes:
    if settings.use_supabase:
        try:
            response = httpx.get(_supabase_object_url(key), headers=_supabase_headers(), timeout=60)
        except httpx.HTTPError as exc:
            raise StorageError("Could not reach file storage.") from exc
        if response.status_code != 200:
            raise StorageError("The file is missing from storage.")
        return response.content
    try:
        return _local_path(key).read_bytes()
    except FileNotFoundError:
        raise StorageError("The file is missing from storage.") from None


def delete(key: str) -> None:
    """Best effort: a failed delete only leaves an orphan file behind."""
    if settings.use_supabase:
        try:
            httpx.request(
                "DELETE",
                f"{settings.supabase_url}/storage/v1/object/{settings.supabase_bucket}",
                json={"prefixes": [key]},
                headers=_supabase_headers(),
                timeout=30,
            )
        except httpx.HTTPError:
            logger.warning("Could not delete %s from Supabase", key)
        return
    try:
        _local_path(key).unlink(missing_ok=True)
    except (OSError, StorageError):
        logger.warning("Could not delete local file %s", key)
