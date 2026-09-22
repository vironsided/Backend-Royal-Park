"""Retention and background cleanup for meter-reading photographs."""

from __future__ import annotations

import logging
import os
import threading
from calendar import monthrange
from datetime import datetime
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import MeterReadingPhoto


logger = logging.getLogger("royalpark")

METER_PHOTO_RETENTION_MONTHS = 3
METER_PHOTO_CLEANUP_INTERVAL_SEC = max(
    60,
    int(os.getenv("METER_PHOTO_CLEANUP_INTERVAL_SEC", "300")),
)

_cleanup_thread: threading.Thread | None = None
_stop_event = threading.Event()


def meter_photo_expiration(uploaded_at: datetime) -> datetime:
    """Return the same wall-clock time three calendar months later."""
    month_index = uploaded_at.month - 1 + METER_PHOTO_RETENTION_MONTHS
    year = uploaded_at.year + month_index // 12
    month = month_index % 12 + 1
    day = min(uploaded_at.day, monthrange(year, month)[1])
    return uploaded_at.replace(year=year, month=month, day=day)


def meter_photo_disk_path(
    photo: MeterReadingPhoto,
    *,
    uploads_root: str | Path = "uploads",
) -> Path:
    """Resolve a stored meter-photo path without allowing directory escape."""
    root = Path(uploads_root).resolve()
    relative = Path(str(photo.file_path).replace("\\", "/"))
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Meter photo path escapes the uploads directory")
    relative_to_root = target.relative_to(root)
    if not relative_to_root.parts or relative_to_root.parts[0] != "meter_readings":
        raise ValueError("Meter photo path is outside uploads/meter_readings")
    return target


def cleanup_expired_meter_photos(
    db: Session,
    *,
    now: datetime | None = None,
    uploads_root: str | Path = "uploads",
) -> int:
    """Delete expired files and stage their database rows for deletion.

    The caller owns the transaction. A row is retained when unlinking fails so
    the scheduler can retry instead of losing the reference to an orphan file.
    """
    cutoff = now or datetime.utcnow()
    expired = (
        db.query(MeterReadingPhoto)
        .filter(MeterReadingPhoto.expires_at <= cutoff)
        .all()
    )
    deleted_count = 0
    for photo in expired:
        try:
            path = meter_photo_disk_path(photo, uploads_root=uploads_root)
            if path.exists():
                path.unlink()
        except ValueError as exc:
            # The invalid row must not be allowed to target another upload.
            logger.error("[meter-photo-cleanup] unsafe path for photo %s: %s", photo.id, exc)
        except OSError as exc:
            logger.warning("[meter-photo-cleanup] cannot delete photo %s: %s", photo.id, exc)
            continue

        db.delete(photo)
        deleted_count += 1

    return deleted_count


def run_meter_photo_cleanup_once(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    uploads_root: str | Path = "uploads",
    now: datetime | None = None,
) -> int:
    """Run and commit one cleanup pass."""
    db = session_factory()
    try:
        deleted_count = cleanup_expired_meter_photos(
            db,
            now=now,
            uploads_root=uploads_root,
        )
        db.commit()
        if deleted_count:
            logger.info("[meter-photo-cleanup] deleted %s expired photo(s)", deleted_count)
        return deleted_count
    except Exception:
        db.rollback()
        logger.exception("[meter-photo-cleanup] cleanup pass failed")
        return 0
    finally:
        db.close()


def _cleanup_loop() -> None:
    while not _stop_event.is_set():
        run_meter_photo_cleanup_once()
        _stop_event.wait(METER_PHOTO_CLEANUP_INTERVAL_SEC)


def start_meter_photo_cleanup_scheduler() -> None:
    global _cleanup_thread
    if _cleanup_thread and _cleanup_thread.is_alive():
        return
    _stop_event.clear()
    _cleanup_thread = threading.Thread(
        target=_cleanup_loop,
        name="meter-photo-cleanup",
        daemon=True,
    )
    _cleanup_thread.start()


def stop_meter_photo_cleanup_scheduler() -> None:
    _stop_event.set()
