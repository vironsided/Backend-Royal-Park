import os, pathlib, secrets
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Header, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_

from ..database import get_db
from ..deps import get_current_user, require_any_role
from ..models import (
    User, RoleEnum, Resident, Block,
    AccessVehicle, AccessRequest, AccessEvent,
    VehicleType, VehicleStatus, AccessDirection, RequestStatus,
    EventDecision, EventSource, user_residents,
    Notification, NotificationStatus,
    AccessGateEvent, GateHint, GateStatus,
)
from ..services.plates import normalize_plate
from ..services.access_logic import decide_access
from ..services.barrier import open_barrier
from ..utils import looks_like_image, IMAGE_MAX_UPLOAD_BYTES
from ..config import settings

router = APIRouter(prefix="/api/access", tags=["access"])

GUARD_ROLES = (RoleEnum.GUARD, RoleEnum.ADMIN, RoleEnum.ROOT)
guard_dep = require_any_role(*GUARD_ROLES)


def _now() -> datetime:
    return datetime.utcnow()


# Asia/Baku is fixed UTC+4 (no DST since 2016). created_at is stored as naive
# UTC, so a "day" filter must use the BAKU calendar day converted to UTC bounds
# — otherwise local 00:00-04:00 events land in the previous day's stats/journal.
BAKU_UTC_OFFSET = timedelta(hours=4)


def _baku_day_bounds_utc(day_local: datetime) -> tuple[datetime, datetime]:
    """[start, end) in naive UTC for the Asia/Baku calendar day of day_local."""
    local_midnight = day_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_midnight - BAKU_UTC_OFFSET
    return start_utc, start_utc + timedelta(days=1)


def _house_label(resident: Optional[Resident]) -> str:
    if not resident:
        return ""
    block = resident.block.name if resident.block else ""
    return f"{block} · {resident.unit_number}".strip(" ·")


@router.get("/ping")
def ping():
    return {"ok": True}


# ───────────────────────── resident: my vehicles ─────────────────────────

class VehicleIn(BaseModel):
    plate: str
    label: Optional[str] = None
    resident_id: Optional[int] = None  # which of the resident's units; default = first linked

class VehicleOut(BaseModel):
    id: int
    plate: str
    type: str
    status: str
    label: Optional[str]
    valid_from: Optional[datetime]
    valid_to: Optional[datetime]
    house: Optional[str] = None


def _resident_unit_ids(db: Session, user: User) -> list[int]:
    rows = db.execute(user_residents.select().where(user_residents.c.user_id == user.id)).fetchall()
    return [row.resident_id for row in rows]


def _vehicle_out(v: AccessVehicle) -> VehicleOut:
    return VehicleOut(id=v.id, plate=v.plate, type=v.type.value, status=v.status.value,
                      label=v.label, valid_from=v.valid_from, valid_to=v.valid_to,
                      house=_house_label(v.resident))


@router.get("/my/vehicles", response_model=List[VehicleOut])
def my_vehicles(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(AccessVehicle).filter(
        AccessVehicle.type == VehicleType.RESIDENT,
        AccessVehicle.created_by_user_id == user.id,
    ).order_by(AccessVehicle.id.desc())
    return [_vehicle_out(v) for v in q.all()]


@router.post("/my/vehicles", response_model=VehicleOut, status_code=201)
def add_my_vehicle(body: VehicleIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    plate = normalize_plate(body.plate)
    if not plate:
        raise HTTPException(400, "Некорректный номер")
    unit_ids = _resident_unit_ids(db, user)
    resident_id = body.resident_id if body.resident_id in unit_ids else (unit_ids[0] if unit_ids else None)
    count = db.query(AccessVehicle).filter(
        AccessVehicle.type == VehicleType.RESIDENT,
        AccessVehicle.created_by_user_id == user.id,
    ).count()
    if count >= settings.ACCESS_VEHICLES_PER_RESIDENT:
        raise HTTPException(400, f"Лимит {settings.ACCESS_VEHICLES_PER_RESIDENT} авто на жителя")
    v = AccessVehicle(plate=plate, type=VehicleType.RESIDENT, status=VehicleStatus.ACTIVE,
                      resident_id=resident_id, created_by_user_id=user.id, label=body.label)
    db.add(v); db.commit(); db.refresh(v)
    return _vehicle_out(v)


@router.delete("/my/vehicles/{vehicle_id}", status_code=204)
def delete_my_vehicle(vehicle_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    v = db.get(AccessVehicle, vehicle_id)
    if not v or v.created_by_user_id != user.id or v.type != VehicleType.RESIDENT:
        raise HTTPException(404, "Не найдено")
    db.delete(v); db.commit()


# ───────────────────────── resident: guest passes ─────────────────────────

class GuestPassIn(BaseModel):
    plate: str
    valid_from: datetime
    valid_to: datetime
    label: Optional[str] = None
    resident_id: Optional[int] = None


@router.get("/my/guest-passes", response_model=List[VehicleOut])
def my_guest_passes(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(AccessVehicle).filter(
        AccessVehicle.type == VehicleType.GUEST,
        AccessVehicle.created_by_user_id == user.id,
    ).order_by(AccessVehicle.id.desc())
    return [_vehicle_out(v) for v in q.all()]


@router.post("/my/guest-passes", response_model=VehicleOut, status_code=201)
def add_guest_pass(body: GuestPassIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    plate = normalize_plate(body.plate)
    if not plate:
        raise HTTPException(400, "Некорректный номер")
    if body.valid_to <= body.valid_from:
        raise HTTPException(400, "Окончание должно быть позже начала")
    unit_ids = _resident_unit_ids(db, user)
    resident_id = body.resident_id if body.resident_id in unit_ids else (unit_ids[0] if unit_ids else None)
    v = AccessVehicle(plate=plate, type=VehicleType.GUEST, status=VehicleStatus.ACTIVE,
                      resident_id=resident_id, created_by_user_id=user.id, label=body.label,
                      valid_from=body.valid_from, valid_to=body.valid_to)
    db.add(v); db.commit(); db.refresh(v)
    return _vehicle_out(v)


@router.delete("/my/guest-passes/{vehicle_id}", status_code=204)
def delete_guest_pass(vehicle_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    v = db.get(AccessVehicle, vehicle_id)
    if not v or v.created_by_user_id != user.id or v.type != VehicleType.GUEST:
        raise HTTPException(404, "Не найдено")
    db.delete(v); db.commit()


# ───────────────────────── resident: requests ─────────────────────────

class RequestIn(BaseModel):
    plate: str
    direction: AccessDirection
    note: Optional[str] = None
    resident_id: Optional[int] = None

class RequestOut(BaseModel):
    id: int
    plate: str
    direction: str
    note: Optional[str]
    status: str
    created_at: datetime
    requester_name: Optional[str] = None
    house: Optional[str] = None


def _request_out(rq: AccessRequest) -> RequestOut:
    return RequestOut(id=rq.id, plate=rq.plate, direction=rq.direction.value, note=rq.note,
                      status=rq.status.value, created_at=rq.created_at,
                      requester_name=(rq.requester.full_name if rq.requester else None),
                      house=_house_label(rq.resident))


@router.get("/my/requests", response_model=List[RequestOut])
def my_requests(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(AccessRequest).filter(AccessRequest.requester_user_id == user.id).order_by(AccessRequest.id.desc())
    return [_request_out(x) for x in q.all()]


@router.post("/my/requests", response_model=RequestOut, status_code=201)
def create_request(body: RequestIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    plate = normalize_plate(body.plate)
    if not plate:
        raise HTTPException(400, "Некорректный номер")
    unit_ids = _resident_unit_ids(db, user)
    resident_id = body.resident_id if body.resident_id in unit_ids else (unit_ids[0] if unit_ids else None)
    rq = AccessRequest(requester_user_id=user.id, resident_id=resident_id, plate=plate,
                       direction=body.direction, note=body.note, status=RequestStatus.PENDING)
    db.add(rq); db.commit(); db.refresh(rq)
    return _request_out(rq)


# ───────────────────────── guard: check ─────────────────────────

class CheckIn(BaseModel):
    plate: str
    direction: AccessDirection

class CheckOut(BaseModel):
    result: str               # ALLOW | UNKNOWN | BLACKLIST
    plate: str
    vehicle_id: Optional[int] = None
    type: Optional[str] = None
    label: Optional[str] = None
    resident_id: Optional[int] = None
    resident_name: Optional[str] = None
    house: Optional[str] = None


def _lookup_vehicle(db: Session, plate: str, now: datetime) -> Optional[AccessVehicle]:
    """Best match for a plate: blacklist wins (alert), else a currently-valid one, else any."""
    rows = db.query(AccessVehicle).filter(AccessVehicle.plate == plate).all()
    if not rows:
        return None
    for v in rows:
        if v.status == VehicleStatus.BLACKLIST:
            return v
    for v in rows:
        if decide_access(v, now).result == "ALLOW":
            return v
    return rows[0]


@router.post("/check", response_model=CheckOut)
def guard_check(body: CheckIn, user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    plate = normalize_plate(body.plate)
    now = _now()
    v = _lookup_vehicle(db, plate, now)
    decision = decide_access(v, now)
    out = CheckOut(result=decision.result, plate=plate)
    if v:
        out.vehicle_id = v.id
        out.type = v.type.value
        out.label = v.label
        if v.resident:
            out.resident_id = v.resident.id
            out.resident_name = v.resident.owner_full_name
            out.house = _house_label(v.resident)
    return out


# ───────────────────────── guard: open / deny ─────────────────────────

class OpenIn(BaseModel):
    plate: Optional[str] = None
    direction: AccessDirection
    source: EventSource = EventSource.MANUAL
    resident_id: Optional[int] = None
    visitor_name: Optional[str] = None
    visitor_doc_id: Optional[str] = None
    purpose: Optional[str] = None

class OpenOut(BaseModel):
    event_id: int
    decision: str
    barrier: dict


@router.post("/open", response_model=OpenOut)
def guard_open(body: OpenIn, user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    plate = normalize_plate(body.plate) if body.plate else ""
    now = _now()
    v = _lookup_vehicle(db, plate, now) if plate else None
    if v and v.status == VehicleStatus.BLACKLIST:
        raise HTTPException(409, "Номер в чёрном списке — открытие запрещено")
    ev = AccessEvent(
        plate=plate or "—", direction=body.direction,
        decision=EventDecision.MANUAL_OPEN, source=body.source,
        matched_vehicle_id=(v.id if v else None),
        resident_id=(body.resident_id if body.resident_id else (v.resident_id if v else None)),
        guard_id=user.id, visitor_name=body.visitor_name,
        visitor_doc_id=body.visitor_doc_id, purpose=body.purpose,
    )
    db.add(ev); db.commit(); db.refresh(ev)
    barrier = open_barrier(gate="MAIN", direction=body.direction.value)
    return OpenOut(event_id=ev.id, decision=ev.decision.value, barrier=barrier)


class DenyIn(BaseModel):
    plate: Optional[str] = None
    direction: AccessDirection
    resident_id: Optional[int] = None
    visitor_name: Optional[str] = None
    visitor_doc_id: Optional[str] = None
    purpose: Optional[str] = None


@router.post("/deny", status_code=201)
def guard_deny(body: DenyIn, user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    plate = normalize_plate(body.plate) if body.plate else "—"
    ev = AccessEvent(plate=plate, direction=body.direction, decision=EventDecision.DENIED,
                     source=EventSource.MANUAL, resident_id=body.resident_id, guard_id=user.id,
                     visitor_name=body.visitor_name, visitor_doc_id=body.visitor_doc_id,
                     purpose=body.purpose)
    db.add(ev); db.commit(); db.refresh(ev)
    return {"event_id": ev.id, "decision": ev.decision.value}


# ───────────────────────── guard: requests inbox ─────────────────────────

@router.get("/requests", response_model=List[RequestOut])
def guard_requests(status: Optional[RequestStatus] = Query(None),
                   user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    q = db.query(AccessRequest)
    if status:
        q = q.filter(AccessRequest.status == status)
    q = q.order_by(AccessRequest.id.desc())
    return [_request_out(x) for x in q.all()]


def _notify_resident(db: Session, rq: AccessRequest, text: str):
    db.add(Notification(user_id=rq.requester_user_id, message=text,
                        status=NotificationStatus.UNREAD, notification_type="ACCESS",
                        related_id=rq.id))


@router.post("/requests/{request_id}/handle", response_model=OpenOut)
def handle_request(request_id: int, user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    rq = db.get(AccessRequest, request_id)
    if not rq:
        raise HTTPException(404, "Заявка не найдена")
    rq.status = RequestStatus.HANDLED
    rq.handled_by_guard_id = user.id
    rq.handled_at = _now()
    ev = AccessEvent(plate=rq.plate, direction=rq.direction, decision=EventDecision.MANUAL_OPEN,
                     source=EventSource.REQUEST, resident_id=rq.resident_id, guard_id=user.id)
    db.add(ev)
    _notify_resident(db, rq, f"Охрана открыла шлагбаум по вашей заявке ({rq.plate}).")
    db.commit(); db.refresh(ev)
    barrier = open_barrier(gate="MAIN", direction=rq.direction.value)
    return OpenOut(event_id=ev.id, decision=ev.decision.value, barrier=barrier)


@router.post("/requests/{request_id}/reject")
def reject_request(request_id: int, user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    rq = db.get(AccessRequest, request_id)
    if not rq:
        raise HTTPException(404, "Заявка не найдена")
    rq.status = RequestStatus.REJECTED
    rq.handled_by_guard_id = user.id
    rq.handled_at = _now()
    _notify_resident(db, rq, f"Охрана отклонила вашу заявку ({rq.plate}).")
    db.commit()
    return {"ok": True}


# ───────────────────────── guard: vehicle registry ─────────────────────────

class GuardVehicleIn(BaseModel):
    plate: str
    type: VehicleType = VehicleType.GUEST
    resident_id: Optional[int] = None
    label: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None


@router.get("/vehicles", response_model=List[VehicleOut])
def guard_vehicles(q: Optional[str] = Query(None), user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    query = db.query(AccessVehicle)
    if q:
        like = f"%{normalize_plate(q)}%"
        query = query.filter(AccessVehicle.plate.like(like))
    return [_vehicle_out(v) for v in query.order_by(AccessVehicle.id.desc()).limit(200).all()]


@router.post("/vehicles", response_model=VehicleOut, status_code=201)
def guard_add_vehicle(body: GuardVehicleIn, user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    plate = normalize_plate(body.plate)
    if not plate:
        raise HTTPException(400, "Некорректный номер")
    v = AccessVehicle(plate=plate, type=body.type, status=VehicleStatus.ACTIVE,
                      resident_id=body.resident_id, created_by_user_id=user.id, label=body.label,
                      valid_from=body.valid_from, valid_to=body.valid_to)
    db.add(v); db.commit(); db.refresh(v)
    return _vehicle_out(v)


@router.post("/vehicles/{vehicle_id}/blacklist", response_model=VehicleOut)
def guard_blacklist(vehicle_id: int, user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    v = db.get(AccessVehicle, vehicle_id)
    if not v:
        raise HTTPException(404, "Не найдено")
    v.status = VehicleStatus.BLACKLIST
    db.commit(); db.refresh(v)
    return _vehicle_out(v)


class VehicleStatusIn(BaseModel):
    status: str


@router.post("/vehicles/{vehicle_id}/status", response_model=VehicleOut)
def guard_set_vehicle_status(vehicle_id: int, body: VehicleStatusIn,
                             user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    """Edit a vehicle's status (e.g. lift a block: BLACKLIST → ACTIVE)."""
    v = db.get(AccessVehicle, vehicle_id)
    if not v:
        raise HTTPException(404, "Не найдено")
    try:
        v.status = VehicleStatus(body.status)
    except ValueError:
        raise HTTPException(400, "Некорректный статус")
    db.commit(); db.refresh(v)
    return _vehicle_out(v)


# ───────────────────────── guard: residents search + events journal ─────────────────────────

class ResidentPick(BaseModel):
    resident_id: int
    owner_name: Optional[str]
    house: str

class EventOut(BaseModel):
    id: int
    plate: str
    direction: str
    decision: str
    source: str
    matched_vehicle_id: Optional[int]
    resident_id: Optional[int]
    visitor_name: Optional[str]
    visitor_doc_id: Optional[str]
    purpose: Optional[str]
    house: Optional[str]
    created_at: datetime
    exit_at: Optional[datetime]
    vehicle_type: Optional[str] = None      # RESIDENT/GUEST of the matched vehicle (status under which it entered)
    vehicle_status: Optional[str] = None    # ACTIVE/BLACKLIST/EXPIRED of the matched vehicle


def _vehicle_type_map(db: Session, events) -> dict:
    """matched_vehicle_id -> (type, status) for the given events (one query, no N+1)."""
    ids = {e.matched_vehicle_id for e in events if e.matched_vehicle_id}
    if not ids:
        return {}
    rows = db.query(AccessVehicle).filter(AccessVehicle.id.in_(ids)).all()
    return {v.id: (v.type.value, v.status.value) for v in rows}


def _event_out(e: AccessEvent, vmap: dict) -> EventOut:
    vt, vs = vmap.get(e.matched_vehicle_id, (None, None))
    return EventOut(id=e.id, plate=e.plate, direction=e.direction.value, decision=e.decision.value,
                    source=e.source.value, matched_vehicle_id=e.matched_vehicle_id, resident_id=e.resident_id,
                    visitor_name=e.visitor_name, visitor_doc_id=e.visitor_doc_id, purpose=e.purpose,
                    house=_house_label(e.resident), created_at=e.created_at, exit_at=e.exit_at,
                    vehicle_type=vt, vehicle_status=vs)


@router.get("/residents/search", response_model=List[ResidentPick])
def residents_search(q: str = Query(..., min_length=1), user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    # Guards pick a destination by BLOCK + VILLA NUMBER only — never by resident
    # name (privacy). Typing "X" must list X-block villas, not people named X.
    like = f"%{q}%"
    rows = (db.query(Resident).join(Block, Resident.block_id == Block.id)
            .filter(or_(Resident.unit_number.ilike(like),
                        Block.name.ilike(like)))
            .order_by(Block.name.asc(), Resident.unit_number.asc())
            .limit(30).all())
    return [ResidentPick(resident_id=r.id, owner_name=None, house=_house_label(r)) for r in rows]


@router.get("/events", response_model=List[EventOut])
def events_journal(direction: Optional[AccessDirection] = Query(None),
                   plate: Optional[str] = Query(None),
                   date: Optional[str] = Query(None),   # YYYY-MM-DD — events of that day (admin journal)
                   limit: int = Query(300, le=2000),
                   user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    q = db.query(AccessEvent)
    if direction:
        q = q.filter(AccessEvent.direction == direction)
    if plate:
        q = q.filter(AccessEvent.plate.like(f"%{normalize_plate(plate)}%"))
    if date:
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Дата должна быть в формате ГГГГ-ММ-ДД")
        start, end = _baku_day_bounds_utc(day)
        q = q.filter(AccessEvent.created_at >= start, AccessEvent.created_at < end)
    rows = q.order_by(AccessEvent.id.desc()).limit(limit).all()
    vmap = _vehicle_type_map(db, rows)
    return [_event_out(e, vmap) for e in rows]


class AccessStatsOut(BaseModel):
    date: str
    entries: int
    exits: int
    guests: int
    denied: int
    blacklist: int
    total: int


@router.get("/stats", response_model=AccessStatsOut)
def access_stats(date: Optional[str] = Query(None), user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    """Daily access stats (defaults to today). Used by the guard board and the admin КПП page."""
    day = None
    if date:
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Дата должна быть в формате ГГГГ-ММ-ДД")
    else:
        day = _now() + BAKU_UTC_OFFSET  # "today" must be today in Baku, not UTC
    start, end = _baku_day_bounds_utc(day)
    rows = db.query(AccessEvent).filter(AccessEvent.created_at >= start, AccessEvent.created_at < end).all()
    s = {"entries": 0, "exits": 0, "guests": 0, "denied": 0, "blacklist": 0}
    for e in rows:
        if e.decision == EventDecision.DENIED:
            s["denied"] += 1
        elif e.decision == EventDecision.BLACKLIST_ALERT:
            s["blacklist"] += 1
        if e.direction == AccessDirection.IN:
            s["entries"] += 1
        else:
            s["exits"] += 1
        if e.visitor_name:
            s["guests"] += 1
    # start is UTC; the displayed date must be the Baku calendar day
    return AccessStatsOut(date=(start + BAKU_UTC_OFFSET).strftime("%Y-%m-%d"), total=len(rows), **s)


class ExitIn(BaseModel):
    exit_at: Optional[datetime] = None   # default = server now


@router.post("/events/{event_id}/exit", response_model=EventOut)
def mark_event_exit(event_id: int, body: ExitIn, user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    """Record the exit time for a logged entry (camera or guard). Toggle off if exit_at sent as null explicitly is not supported — always sets a time."""
    e = db.get(AccessEvent, event_id)
    if not e:
        raise HTTPException(404, "Не найдено")
    e.exit_at = body.exit_at or _now()
    db.commit(); db.refresh(e)
    return _event_out(e, {})


def _autoclose_exit(db: Session, plate: str, now: datetime) -> bool:
    """On an OUT detection, stamp the exit time on the most recent open IN entry for this plate."""
    if not plate:
        return False
    e = (db.query(AccessEvent)
         .filter(AccessEvent.plate == plate,
                 AccessEvent.direction == AccessDirection.IN,
                 AccessEvent.exit_at.is_(None))
         .order_by(AccessEvent.id.desc()).first())
    if not e:
        return False
    e.exit_at = now
    db.commit()
    return True


# ───────────────────────── ANPR camera (Phase 2) ─────────────────────────

def require_camera_key(x_camera_key: Optional[str] = Header(default=None)):
    expected = settings.ACCESS_CAMERA_KEY or ""
    if not expected:
        raise HTTPException(status_code=503, detail="Camera key not configured")
    if not x_camera_key or not secrets.compare_digest(x_camera_key, expected):
        raise HTTPException(status_code=401, detail="Invalid camera key")
    return True


def _save_gate_snapshot(file: Optional[UploadFile], gate_event_id: int) -> Optional[str]:
    """Persist the camera frame to uploads/gate/<id>.jpg and return its /uploads URL."""
    if file is None:
        return None
    try:
        # audit F-14 applied here too: cap the read and verify real image magic
        # bytes, so a (mis)using camera key can't disk-fill or store arbitrary files.
        data = file.file.read(IMAGE_MAX_UPLOAD_BYTES + 1)
    except Exception:
        return None
    if not data or len(data) > IMAGE_MAX_UPLOAD_BYTES or not looks_like_image(data):
        return None
    base = pathlib.Path("uploads/gate")
    base.mkdir(parents=True, exist_ok=True)
    with open(base / f"{gate_event_id}.jpg", "wb") as f:
        f.write(data)
    return f"/uploads/gate/{gate_event_id}.jpg"


_camera_last_seen: dict = {}  # (plate, direction) -> last datetime (in-memory debounce)


@router.post("/camera/detect")
def camera_detect(
    direction: AccessDirection = Form(...),
    plate: Optional[str] = Form(None),
    vehicle_type: Optional[str] = Form(None),
    camera_id: Optional[str] = Form(None),
    snapshot: Optional[UploadFile] = File(None),
    _ok: bool = Depends(require_camera_key),
    db: Session = Depends(get_db),
):
    now = _now()
    norm = normalize_plate(plate) if plate else ""

    if norm:
        key = (norm, direction.value)
        last = _camera_last_seen.get(key)
        if last and (now - last).total_seconds() < settings.ACCESS_CAMERA_DEBOUNCE_SEC:
            return {"action": "debounced"}
        _camera_last_seen[key] = now

    # Exit detection: a car leaving auto-stamps the exit time on its open entry log.
    if norm and direction == AccessDirection.OUT:
        _autoclose_exit(db, norm, now)

    v = _lookup_vehicle(db, norm, now) if norm else None
    decision = decide_access(v, now).result if norm else "UNKNOWN"

    if decision == "ALLOW":
        ev = AccessEvent(plate=norm, direction=direction, decision=EventDecision.AUTO_OPEN,
                         source=EventSource.CAMERA, matched_vehicle_id=(v.id if v else None),
                         resident_id=(v.resident_id if v else None))
        db.add(ev); db.commit()
        barrier = open_barrier(gate=(camera_id or "MAIN"), direction=direction.value)
        return {"action": "opened", "barrier": barrier}

    hint = (GateHint.BLACKLIST if decision == "BLACKLIST"
            else GateHint.NO_PLATE if not norm
            else GateHint.UNKNOWN)
    ge = AccessGateEvent(plate=(norm or None), vehicle_type=vehicle_type, hint=hint,
                         direction=direction, status=GateStatus.PENDING, camera_id=camera_id)
    db.add(ge); db.commit(); db.refresh(ge)
    ge.snapshot_url = _save_gate_snapshot(snapshot, ge.id)
    db.commit()
    return {"action": "queued", "id": ge.id, "hint": hint.value}


# ---- guard: gate-events feed + resolve ----

class GateEventOut(BaseModel):
    id: int
    plate: Optional[str]
    vehicle_type: Optional[str]
    hint: str
    direction: str
    snapshot_url: Optional[str]
    status: str
    camera_id: Optional[str]
    created_at: datetime

def _gate_event_out(g: AccessGateEvent) -> GateEventOut:
    return GateEventOut(id=g.id, plate=g.plate, vehicle_type=g.vehicle_type, hint=g.hint.value,
                        direction=g.direction.value, snapshot_url=g.snapshot_url, status=g.status.value,
                        camera_id=g.camera_id, created_at=g.created_at)


# A PENDING camera event nobody resolved within this window is stale (the car
# is long gone) — auto-dismiss it so the КПП queue/badge only shows live work.
GATE_EVENT_TTL_HOURS = 24


def _expire_stale_gate_events(db: Session) -> None:
    cutoff = _now() - timedelta(hours=GATE_EVENT_TTL_HOURS)
    stale = (db.query(AccessGateEvent)
             .filter(AccessGateEvent.status == GateStatus.PENDING,
                     AccessGateEvent.created_at < cutoff)
             .update({AccessGateEvent.status: GateStatus.DISMISSED,
                      AccessGateEvent.handled_at: _now()},  # handled_by_guard_id stays NULL = auto
                     synchronize_session=False))
    if stale:
        db.commit()


@router.get("/gate-events", response_model=List[GateEventOut])
def gate_events(status: Optional[GateStatus] = Query(None), user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    _expire_stale_gate_events(db)
    q = db.query(AccessGateEvent)
    if status:
        q = q.filter(AccessGateEvent.status == status)
    return [_gate_event_out(g) for g in q.order_by(AccessGateEvent.id.desc()).limit(100).all()]


class GateResolveIn(BaseModel):
    action: str                      # "open" | "deny"
    resident_id: Optional[int] = None
    visitor_name: Optional[str] = None
    visitor_doc_id: Optional[str] = None
    purpose: Optional[str] = None


@router.post("/gate-events/{event_id}/resolve")
def resolve_gate_event(event_id: int, body: GateResolveIn, user: User = Depends(guard_dep), db: Session = Depends(get_db)):
    ge = db.get(AccessGateEvent, event_id)
    if not ge:
        raise HTTPException(404, "Не найдено")
    decision = EventDecision.MANUAL_OPEN if body.action == "open" else EventDecision.DENIED
    ev = AccessEvent(plate=(ge.plate or "—"), direction=ge.direction, decision=decision,
                     source=EventSource.CAMERA, resident_id=body.resident_id, guard_id=user.id,
                     visitor_name=body.visitor_name, visitor_doc_id=body.visitor_doc_id,
                     purpose=body.purpose, snapshot_url=ge.snapshot_url)
    db.add(ev)
    ge.status = GateStatus.HANDLED
    ge.handled_by_guard_id = user.id
    ge.handled_at = _now()
    db.commit()
    if body.action == "open":
        barrier = open_barrier(gate=(ge.camera_id or "MAIN"), direction=ge.direction.value)
        return {"ok": True, "barrier": barrier}
    return {"ok": True}
