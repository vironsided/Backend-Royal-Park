from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.meter_photo_cleanup import (
    cleanup_expired_meter_photos,
    meter_photo_expiration,
    run_meter_photo_cleanup_once,
)


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.deleted = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def query(self, _model):
        return _FakeQuery(self.rows)

    def delete(self, row):
        self.deleted.append(row)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_meter_photo_retention_is_three_calendar_months():
    assert meter_photo_expiration(datetime(2026, 1, 31, 12, 30)) == datetime(
        2026,
        4,
        30,
        12,
        30,
    )
    assert meter_photo_expiration(datetime(2026, 11, 30, 8, 15)) == datetime(
        2027,
        2,
        28,
        8,
        15,
    )


def test_cleanup_removes_expired_file_and_stages_database_row(tmp_path):
    uploads_root = tmp_path / "uploads"
    photo_path = uploads_root / "meter_readings" / "reading.webp"
    photo_path.parent.mkdir(parents=True)
    photo_path.write_bytes(b"photo")
    photo = SimpleNamespace(
        id=7,
        file_path="meter_readings/reading.webp",
        expires_at=datetime.utcnow() - timedelta(seconds=1),
    )
    db = _FakeSession([photo])

    deleted_count = cleanup_expired_meter_photos(
        db,
        now=datetime.utcnow(),
        uploads_root=uploads_root,
    )

    assert deleted_count == 1
    assert not photo_path.exists()
    assert db.deleted == [photo]


def test_committed_cleanup_pass_closes_its_session(tmp_path):
    uploads_root = tmp_path / "uploads"
    photo_path = uploads_root / "meter_readings" / "reading.webp"
    photo_path.parent.mkdir(parents=True)
    photo_path.write_bytes(b"photo")
    photo = SimpleNamespace(
        id=8,
        file_path="meter_readings/reading.webp",
        expires_at=datetime.utcnow() - timedelta(days=1),
    )
    db = _FakeSession([photo])

    deleted_count = run_meter_photo_cleanup_once(
        session_factory=lambda: db,
        uploads_root=uploads_root,
    )

    assert deleted_count == 1
    assert db.committed is True
    assert db.rolled_back is False
    assert db.closed is True
