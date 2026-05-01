from typing import List, Optional
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from sqlalchemy import select, func

from ..database import get_db
from ..models import (
    User, RoleEnum, Block, Resident, ResidentMeter,
    ResidentType, ResidentStatus, CustomerType, MeterType, Tariff, MeterReading,
    Invoice, InvoiceLine, InvoiceStatus, PaymentApplication
)
from ..deps import get_current_user


router = APIRouter(prefix="/api/residents", tags=["residents-api"])
DISABLED_MANUAL_METER_TYPES = {MeterType.SEWERAGE.value}


# Pydantic models
class MeterIn(BaseModel):
    id: Optional[int] = None  # existing ResidentMeter.id (optional; helps avoid losing history)
    meter_type: str  # MeterType value
    serial: str
    used: bool  # True если initial_reading > 0
    initial: float  # initial_reading
    tariff_id: int


class ResidentOut(BaseModel):
    id: int
    block_id: int
    block_name: Optional[str] = None
    unit_number: str
    resident_type: str
    customer_type: str
    status: str
    debt: Optional[float] = 0.0  # начальный долг (если задан)
    debt_utility: Optional[float] = 0.0
    debt_service: Optional[float] = 0.0
    debt_rent: Optional[float] = 0.0
    owner_full_name: Optional[str] = None
    owner_phone: Optional[str] = None
    owner_email: Optional[str] = None
    comment: Optional[str] = None
    meters: List[dict] = []
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ResidentCreate(BaseModel):
    block_id: int
    unit_number: str
    resident_type: str  # ResidentType value
    customer_type: str  # CustomerType value
    status: str  # ResidentStatus value
    owner_full_name: Optional[str] = None
    owner_phone: Optional[str] = None
    owner_email: Optional[EmailStr] = None
    comment: Optional[str] = None
    debt: Optional[float] = 0.0  # начальный долг (до внедрения системы)
    debt_utility: Optional[float] = None
    debt_service: Optional[float] = None
    debt_rent: Optional[float] = None
    meters: List[MeterIn] = []


class ResidentUpdate(BaseModel):
    block_id: Optional[int] = None
    unit_number: Optional[str] = None
    resident_type: Optional[str] = None
    customer_type: Optional[str] = None
    status: Optional[str] = None
    owner_full_name: Optional[str] = None
    owner_phone: Optional[str] = None
    owner_email: Optional[EmailStr] = None
    comment: Optional[str] = None
    debt: Optional[float] = None  # обновление начального долга (opening invoice)
    debt_utility: Optional[float] = None
    debt_service: Optional[float] = None
    debt_rent: Optional[float] = None
    meters: Optional[List[MeterIn]] = None


def _opening_invoice_number(resident_id: int) -> str:
    return f"OPEN/{resident_id:06d}"


def _get_opening_invoice(db: Session, resident_id: int) -> Invoice | None:
    """Ищем инвойс начального долга по стабильному номеру OPEN/{resident_id}."""
    return (
        db.execute(
            select(Invoice)
            .where(
                Invoice.resident_id == resident_id,
                Invoice.number == _opening_invoice_number(resident_id),
            )
            .order_by(Invoice.id.desc())
        )
        .scalars()
        .first()
    )


def _get_opening_debt(db: Session, resident_id: int) -> Decimal:
    """Возвращает сумму начального долга (0, если его нет или он отменён)."""
    inv = _get_opening_invoice(db, resident_id)
    if not inv or inv.status == InvoiceStatus.CANCELED:
        return Decimal("0")
    return Decimal(str(inv.amount_total or 0))


OPENING_DEBT_DESCRIPTIONS = {
    "utility": "Начальный долг (Utility)",
    "service": "Начальный долг (Service)",
    "rent": "Начальный долг (Rent)",
}


def _opening_debt_zero_breakdown() -> dict[str, Decimal]:
    return {
        "utility": Decimal("0"),
        "service": Decimal("0"),
        "rent": Decimal("0"),
    }


def _extract_opening_debt_breakdown(payload: object) -> dict[str, Decimal]:
    utility_raw = getattr(payload, "debt_utility", None)
    service_raw = getattr(payload, "debt_service", None)
    rent_raw = getattr(payload, "debt_rent", None)
    total_raw = getattr(payload, "debt", None)

    has_split = utility_raw is not None or service_raw is not None or rent_raw is not None
    if has_split:
        breakdown = {
            "utility": Decimal(str(utility_raw or 0)),
            "service": Decimal(str(service_raw or 0)),
            "rent": Decimal(str(rent_raw or 0)),
        }
    else:
        breakdown = _opening_debt_zero_breakdown()
        breakdown["utility"] = Decimal(str(total_raw or 0))

    for value in breakdown.values():
        if value < 0:
            raise HTTPException(status_code=400, detail="Debt cannot be negative")
    return breakdown


def _has_opening_debt_payload(payload: object) -> bool:
    return any(
        getattr(payload, field, None) is not None
        for field in ("debt", "debt_utility", "debt_service", "debt_rent")
    )


def _opening_debt_total(breakdown: dict[str, Decimal]) -> Decimal:
    return breakdown["utility"] + breakdown["service"] + breakdown["rent"]


def _next_opening_invoice_period(db: Session, resident_id: int) -> tuple[int, int]:
    y, m = 1900, 1
    while True:
        exists_id = db.execute(
            select(Invoice.id).where(
                Invoice.resident_id == resident_id,
                Invoice.period_year == y,
                Invoice.period_month == m,
            )
        ).scalar_one_or_none()
        if not exists_id:
            return y, m
        m += 1
        if m > 12:
            y += 1
            m = 1


def _write_opening_invoice_lines(db: Session, invoice_id: int, breakdown: dict[str, Decimal]) -> None:
    existing = db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id)
    ).scalars().all()
    for line in existing:
        db.delete(line)

    for category in ("utility", "service", "rent"):
        amount = breakdown[category]
        if amount <= 0:
            continue
        db.add(InvoiceLine(
            invoice_id=invoice_id,
            meter_reading_id=None,
            description=OPENING_DEBT_DESCRIPTIONS[category],
            amount_net=amount,
            amount_vat=Decimal("0"),
            amount_total=amount,
        ))


def _upsert_opening_invoice(
    db: Session,
    resident_id: int,
    breakdown: dict[str, Decimal],
    created_by_id: Optional[int],
) -> None:
    total = _opening_debt_total(breakdown)
    opening_inv = _get_opening_invoice(db, resident_id)

    if opening_inv is None:
        if total <= 0:
            return
        period_year, period_month = _next_opening_invoice_period(db, resident_id)
        opening_inv = Invoice(
            resident_id=resident_id,
            number=_opening_invoice_number(resident_id),
            status=InvoiceStatus.ISSUED,
            due_date=None,
            notes="Начальный долг (до внедрения системы)",
            period_year=period_year,
            period_month=period_month,
            amount_net=total,
            amount_vat=Decimal("0"),
            amount_total=total,
            created_by_id=created_by_id,
        )
        db.add(opening_inv)
        db.flush()
        _write_opening_invoice_lines(db, opening_inv.id, breakdown)
        return

    applied = (
        db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
        .filter(PaymentApplication.invoice_id == opening_inv.id)
        .scalar()
        or 0
    )
    applied_dec = Decimal(str(applied))
    if total < applied_dec:
        raise HTTPException(status_code=400, detail="Debt cannot be less than already paid amount")

    if total == 0 and applied_dec == 0:
        opening_inv.status = InvoiceStatus.CANCELED
        opening_inv.amount_net = Decimal("0")
        opening_inv.amount_vat = Decimal("0")
        opening_inv.amount_total = Decimal("0")
    else:
        opening_inv.status = (
            InvoiceStatus.PAID if (total == applied_dec and total > 0)
            else (InvoiceStatus.ISSUED if applied_dec == 0 else InvoiceStatus.PARTIAL)
        )
        opening_inv.notes = "Начальный долг (до внедрения системы)"
        opening_inv.amount_net = total
        opening_inv.amount_vat = Decimal("0")
        opening_inv.amount_total = total

    _write_opening_invoice_lines(db, opening_inv.id, breakdown if total > 0 else _opening_debt_zero_breakdown())


def _get_opening_debt_breakdown(db: Session, resident_id: int) -> dict[str, Decimal]:
    inv = _get_opening_invoice(db, resident_id)
    breakdown = _opening_debt_zero_breakdown()
    if not inv or inv.status == InvoiceStatus.CANCELED:
        return breakdown

    lines = db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
    ).scalars().all()
    if not lines:
        breakdown["utility"] = Decimal(str(inv.amount_total or 0))
        return breakdown

    unclassified = Decimal("0")
    for ln in lines:
        amount = Decimal(str(ln.amount_total or 0))
        desc = str(ln.description or "").lower()
        if "(utility)" in desc:
            breakdown["utility"] += amount
        elif "(service)" in desc:
            breakdown["service"] += amount
        elif "(rent)" in desc:
            breakdown["rent"] += amount
        else:
            unclassified += amount

    if unclassified:
        breakdown["utility"] += unclassified
    return breakdown


# ---------------------------------------------------------------------------
# Helpers used by admin HTML routes too.
# Source-of-truth should live in api_*.py (even if duplicated elsewhere).
# ---------------------------------------------------------------------------
def _to_decimal(value: object) -> Decimal:
    """Safe conversion to Decimal via str (avoids float artifacts)."""
    return Decimal(str(value))


def _parse_meters_json(raw: str):
    """
    Parse meters JSON payload from legacy admin forms.
    Returns list of tuples: (meter_type: MeterType, serial: str, used: bool, initial: Optional[Decimal], tariff_id: int)
    """
    try:
        data = json.loads(raw or "[]")
        assert isinstance(data, list)
    except Exception:
        raise ValueError("meters_invalid_json")

    result = []
    if not data:
        raise ValueError("meters_empty")

    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"meters_item_{i}_not_object")

        mt = item.get("meter_type")
        serial = (item.get("serial") or "").strip()
        used = bool(item.get("used", False))
        initial_raw = item.get("initial", None)
        tariff_id = item.get("tariff_id")

        if mt not in {m.value for m in MeterType}:
            raise ValueError(f"meters_item_{i}_bad_type")

        mtype = MeterType(mt)

        initial = None
        if used:
            try:
                initial = _to_decimal(initial_raw)
            except (InvalidOperation, Exception):
                raise ValueError(f"meters_item_{i}_bad_initial")
            if initial < 0:
                raise ValueError(f"meters_item_{i}_negative_initial")

        try:
            tariff_id = int(tariff_id)
        except Exception:
            raise ValueError(f"meters_item_{i}_bad_tariff")

        result.append((mtype, serial, used, initial, tariff_id))

    return result


def _parse_meters(meters_data: List[MeterIn]) -> List[dict]:
    """Валидация и преобразование счётчиков."""
    result = []
    fixed_price_types = {MeterType.SERVICE, MeterType.RENT, MeterType.CONSTRUCTION}
    
    for m in meters_data:
        if m.meter_type not in {t.value for t in MeterType}:
            raise ValueError(f"Invalid meter_type: {m.meter_type}")
        if m.meter_type in DISABLED_MANUAL_METER_TYPES:
            raise ValueError("Канализация рассчитывается автоматически от воды и не назначается вручную")
        
        meter_type_enum = MeterType(m.meter_type)
        is_fixed_price = meter_type_enum in fixed_price_types
        
        # Для фикс-услуг серийный номер не обязателен (может быть "-" или пустым)
        # Для обычных счётчиков серийный номер обязателен
        serial = m.serial.strip()
        if not is_fixed_price and not serial:
            raise ValueError("Serial number is required for non-fixed-price meters")
        
        # Если серийный номер пустой для фикс-услуги, используем "-"
        if is_fixed_price and not serial:
            serial = "-"
        
        if m.initial < 0:
            raise ValueError("Initial reading cannot be negative")
        
        result.append({
            "id": m.id,
            "meter_type": meter_type_enum,
            "serial": serial,
            "initial_reading": Decimal(str(m.initial)),
            "tariff_id": m.tariff_id,
        })
    return result


def _list_residents_internal(
    db: Session,
    block_id: Optional[int] = None,
    status: Optional[str] = None,
    rtype: Optional[str] = None,
    q: Optional[str] = None,
    unit_number: Optional[str] = None,
    page: int = 1,
    per_page: int = 25,
):
    """Внутренняя функция для получения списка резидентов (без авторизации)."""
    stmt = select(Resident)
    
    if block_id:
        stmt = stmt.where(Resident.block_id == block_id)
    if status and status in {s.value for s in ResidentStatus}:
        stmt = stmt.where(Resident.status == ResidentStatus(status))
    if rtype and rtype in {t.value for t in ResidentType}:
        stmt = stmt.where(Resident.resident_type == ResidentType(rtype))
    if unit_number:
        unit_number = unit_number.strip()
        if "-" in unit_number:
            parts = unit_number.split("-")
            if len(parts) == 2:
                try:
                    start = int(parts[0].strip())
                    end = int(parts[1].strip())
                    # Для диапазонов пытаемся привести unit_number к числу в БД
                    # Используем CAST если возможно, или просто сравниваем строки если нет
                    # Но лучше всего для "101-105" просто искать по диапазону чисел
                    # Мы будем использовать cast к INTEGER для unit_number если это возможно
                    stmt = stmt.where(
                        func.cast(Resident.unit_number, func.Integer).between(start, end)
                    )
                except (ValueError, Exception):
                    # Если не удалось распарсить как числа, используем LIKE
                    stmt = stmt.where(Resident.unit_number.ilike(f"%{unit_number}%"))
            else:
                stmt = stmt.where(Resident.unit_number.ilike(f"%{unit_number}%"))
        else:
            # Для одиночного номера используем или точное совпадение или префикс
            stmt = stmt.where(Resident.unit_number.ilike(f"%{unit_number}%"))
            
    if q:
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            func.lower(Resident.unit_number).like(needle) |
            func.lower(Resident.owner_full_name).like(needle) |
            func.lower(Resident.owner_phone).like(needle) |
            func.lower(Resident.owner_email).like(needle)
        )
    
    # Подсчет общего количества
    count_stmt = select(func.count(Resident.id))
    if block_id:
        count_stmt = count_stmt.where(Resident.block_id == block_id)
    if status and status in {s.value for s in ResidentStatus}:
        count_stmt = count_stmt.where(Resident.status == ResidentStatus(status))
    if rtype and rtype in {t.value for t in ResidentType}:
        count_stmt = count_stmt.where(Resident.resident_type == ResidentType(rtype))
    
    # Дублируем логику фильтрации для count_stmt
    if unit_number:
        unit_number = unit_number.strip()
        if "-" in unit_number:
            parts = unit_number.split("-")
            if len(parts) == 2:
                try:
                    start = int(parts[0].strip())
                    end = int(parts[1].strip())
                    count_stmt = count_stmt.where(
                        func.cast(Resident.unit_number, func.Integer).between(start, end)
                    )
                except (ValueError, Exception):
                    count_stmt = count_stmt.where(Resident.unit_number.ilike(f"%{unit_number}%"))
            else:
                count_stmt = count_stmt.where(Resident.unit_number.ilike(f"%{unit_number}%"))
        else:
            count_stmt = count_stmt.where(Resident.unit_number.ilike(f"%{unit_number}%"))

    if q:
        needle = f"%{q.strip().lower()}%"
        count_stmt = count_stmt.where(
            func.lower(Resident.unit_number).like(needle) |
            func.lower(Resident.owner_full_name).like(needle) |
            func.lower(Resident.owner_phone).like(needle) |
            func.lower(Resident.owner_email).like(needle)
        )
    
    total = db.execute(count_stmt).scalar() or 0
    last_page = max(1, (total + per_page - 1) // per_page)
    if page > last_page:
        page = last_page
    
    # Применяем пагинацию. Новые резиденты должны идти первыми,
    # поэтому сортируем по id по убыванию.
    residents = db.execute(
        stmt.order_by(Resident.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).scalars().all()
    
    result = []
    for r in residents:
        meters_data = []
        for m in r.meters:
            # В списке резидентов показываем только активные счётчики
            if m.is_active:
                meters_data.append({
                    "id": m.id,
                    "meter_type": m.meter_type.value,
                    "serial_number": m.serial_number,
                    "initial_reading": float(m.initial_reading),
                    "tariff_id": m.tariff_id,
                    "tariff_name": m.tariff.name if m.tariff else None,
                    "is_active": m.is_active,
                })
        
        result.append({
            "id": r.id,
            "block_id": r.block_id,
            "block_name": r.block.name if r.block else None,
            "unit_number": r.unit_number,
            "resident_type": r.resident_type.value,
            "customer_type": r.customer_type.value,
            "status": r.status.value,
            "owner_full_name": r.owner_full_name,
            "owner_phone": r.owner_phone,
            "owner_email": r.owner_email,
            "comment": r.comment,
            "meters": meters_data,
            "created_at": r.created_at,
        })
    
    return {
        "items": result,
        "total": total,
        "page": page,
        "per_page": per_page,
        "last_page": last_page,
    }


@router.get("/")
def list_residents_api(
    db: Session = Depends(get_db),
    block_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    rtype: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    unit_number: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    actor: User = Depends(get_current_user),
):
    """
    JSON-список резидентов с фильтрами и пагинацией.
    """
    return _list_residents_internal(
        db=db,
        block_id=block_id,
        status=status,
        rtype=rtype,
        q=q,
        unit_number=unit_number,
        page=page,
        per_page=per_page,
    )


# ВРЕМЕННО: endpoint без авторизации для теста
@router.get("/public")
def list_residents_public(
    db: Session = Depends(get_db),
    block_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    rtype: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    unit_number: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
):
    """
    ВРЕМЕННЫЙ endpoint без авторизации для теста SPA.
    TODO: удалить после настройки нормальной авторизации.
    """
    return _list_residents_internal(
        db=db,
        block_id=block_id,
        status=status,
        rtype=rtype,
        q=q,
        unit_number=unit_number,
        page=page,
        per_page=per_page,
    )


@router.post("/", response_model=ResidentOut, status_code=status.HTTP_201_CREATED)
def create_resident_api(
    payload: ResidentCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """
    Создание нового резидента.
    """
    # Валидация блока
    block = db.get(Block, payload.block_id)
    if not block:
        raise HTTPException(status_code=404, detail="Block not found")
    
    # Валидация типов
    if payload.resident_type not in {t.value for t in ResidentType}:
        raise HTTPException(status_code=400, detail=f"Invalid resident_type: {payload.resident_type}")
    if payload.customer_type not in {c.value for c in CustomerType}:
        raise HTTPException(status_code=400, detail=f"Invalid customer_type: {payload.customer_type}")
    if payload.status not in {s.value for s in ResidentStatus}:
        raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")
    
    # Проверка уникальности unit_number в блоке
    existing = db.execute(
        select(Resident.id).where(
            Resident.block_id == payload.block_id,
            Resident.unit_number == payload.unit_number.strip()
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Unit number already exists in this block")
    
    # Разбор счётчиков
    try:
        meters_data = _parse_meters(payload.meters) if payload.meters else []
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    # Проверка тарифов для счётчиков
    for m_data in meters_data:
        tariff = db.get(Tariff, m_data["tariff_id"])
        if not tariff:
            raise HTTPException(status_code=404, detail=f"Tariff {m_data['tariff_id']} not found")
        if tariff.meter_type != m_data["meter_type"]:
            raise HTTPException(
                status_code=400,
                detail=f"Tariff {m_data['tariff_id']} meter_type mismatch"
            )
    
    # Создание резидента
    resident = Resident(
        block_id=payload.block_id,
        unit_number=payload.unit_number.strip(),
        resident_type=ResidentType(payload.resident_type),
        customer_type=CustomerType(payload.customer_type),
        status=ResidentStatus(payload.status),
        owner_full_name=payload.owner_full_name.strip() if payload.owner_full_name else None,
        owner_phone=payload.owner_phone.strip() if payload.owner_phone else None,
        owner_email=payload.owner_email.strip() if payload.owner_email else None,
        comment=payload.comment.strip() if payload.comment else None,
        created_by_id=actor.id,
    )
    db.add(resident)
    db.flush()

    # Начальный долг: создаём/обновляем opening invoice c разбивкой по категориям.
    opening_breakdown = _extract_opening_debt_breakdown(payload)
    _upsert_opening_invoice(
        db=db,
        resident_id=resident.id,
        breakdown=opening_breakdown,
        created_by_id=actor.id,
    )
    
    # Создание счётчиков
    for m_data in meters_data:
        meter = ResidentMeter(
            resident_id=resident.id,
            meter_type=m_data["meter_type"],
            serial_number=m_data["serial"],
            initial_reading=m_data["initial_reading"],
            tariff_id=m_data["tariff_id"],
            is_active=True,
        )
        db.add(meter)
    
    db.commit()
    db.refresh(resident)
    
    # Формирование ответа - показываем только активные счетчики
    meters_data = []
    seen_meter_ids = set()
    for m in resident.meters:
        # Показываем только активные счетчики (чтобы избежать дубликатов)
        if not m.is_active:
            continue
        # Пропускаем дубликаты по ID
        if m.id in seen_meter_ids:
            continue
        seen_meter_ids.add(m.id)
        
        meters_data.append({
            "id": m.id,
            "meter_type": m.meter_type.value,
            "serial_number": m.serial_number,
            "initial_reading": float(m.initial_reading),
            "tariff_id": m.tariff_id,
            "tariff_name": m.tariff.name if m.tariff else None,
            "is_active": m.is_active,
        })
    
    opening_breakdown = _get_opening_debt_breakdown(db, resident.id)
    opening_debt = _opening_debt_total(opening_breakdown)

    return {
        "id": resident.id,
        "block_id": resident.block_id,
        "block_name": resident.block.name if resident.block else None,
        "unit_number": resident.unit_number,
        "resident_type": resident.resident_type.value,
        "customer_type": resident.customer_type.value,
        "status": resident.status.value,
        "debt": float(opening_debt),
        "debt_utility": float(opening_breakdown["utility"]),
        "debt_service": float(opening_breakdown["service"]),
        "debt_rent": float(opening_breakdown["rent"]),
        "owner_full_name": resident.owner_full_name,
        "owner_phone": resident.owner_phone,
        "owner_email": resident.owner_email,
        "comment": resident.comment,
        "meters": meters_data,
        "created_at": resident.created_at,
    }


# ВРЕМЕННО: endpoint без авторизации для теста
@router.post("/public", response_model=ResidentOut, status_code=status.HTTP_201_CREATED)
def create_resident_public(
    payload: ResidentCreate,
    db: Session = Depends(get_db),
):
    """
    ВРЕМЕННЫЙ endpoint без авторизации для теста SPA.
    TODO: удалить после настройки нормальной авторизации.
    """
    # Валидация блока
    block = db.get(Block, payload.block_id)
    if not block:
        raise HTTPException(status_code=404, detail="Block not found")
    
    # Валидация типов
    if payload.resident_type not in {t.value for t in ResidentType}:
        raise HTTPException(status_code=400, detail=f"Invalid resident_type: {payload.resident_type}")
    if payload.customer_type not in {c.value for c in CustomerType}:
        raise HTTPException(status_code=400, detail=f"Invalid customer_type: {payload.customer_type}")
    if payload.status not in {s.value for s in ResidentStatus}:
        raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")
    
    # Проверка уникальности unit_number в блоке
    existing = db.execute(
        select(Resident.id).where(
            Resident.block_id == payload.block_id,
            Resident.unit_number == payload.unit_number.strip()
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Unit number already exists in this block")
    
    # Разбор счётчиков
    try:
        meters_data = _parse_meters(payload.meters) if payload.meters else []
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    # Проверка тарифов для счётчиков
    for m_data in meters_data:
        tariff = db.get(Tariff, m_data["tariff_id"])
        if not tariff:
            raise HTTPException(status_code=404, detail=f"Tariff {m_data['tariff_id']} not found")
        if tariff.meter_type != m_data["meter_type"]:
            raise HTTPException(
                status_code=400,
                detail=f"Tariff {m_data['tariff_id']} meter_type mismatch"
            )
    
    # Создание резидента
    resident = Resident(
        block_id=payload.block_id,
        unit_number=payload.unit_number.strip(),
        resident_type=ResidentType(payload.resident_type),
        customer_type=CustomerType(payload.customer_type),
        status=ResidentStatus(payload.status),
        owner_full_name=payload.owner_full_name.strip() if payload.owner_full_name else None,
        owner_phone=payload.owner_phone.strip() if payload.owner_phone else None,
        owner_email=payload.owner_email.strip() if payload.owner_email else None,
        comment=payload.comment.strip() if payload.comment else None,
        created_by_id=None,
    )
    db.add(resident)
    db.flush()

    opening_breakdown = _extract_opening_debt_breakdown(payload)
    _upsert_opening_invoice(
        db=db,
        resident_id=resident.id,
        breakdown=opening_breakdown,
        created_by_id=None,
    )
    
    # Создание счётчиков
    for m_data in meters_data:
        meter = ResidentMeter(
            resident_id=resident.id,
            meter_type=m_data["meter_type"],
            serial_number=m_data["serial"],
            initial_reading=m_data["initial_reading"],
            tariff_id=m_data["tariff_id"],
            is_active=True,
        )
        db.add(meter)
    
    db.commit()
    db.refresh(resident)
    
    # Формирование ответа - показываем только активные счетчики
    meters_data = []
    seen_meter_ids = set()
    for m in resident.meters:
        # Показываем только активные счетчики (чтобы избежать дубликатов)
        if not m.is_active:
            continue
        # Пропускаем дубликаты по ID
        if m.id in seen_meter_ids:
            continue
        seen_meter_ids.add(m.id)
        
        meters_data.append({
            "id": m.id,
            "meter_type": m.meter_type.value,
            "serial_number": m.serial_number,
            "initial_reading": float(m.initial_reading),
            "tariff_id": m.tariff_id,
            "tariff_name": m.tariff.name if m.tariff else None,
            "is_active": m.is_active,
        })
    opening_breakdown = _get_opening_debt_breakdown(db, resident.id)
    opening_debt = _opening_debt_total(opening_breakdown)

    return {
        "id": resident.id,
        "block_id": resident.block_id,
        "block_name": resident.block.name if resident.block else None,
        "unit_number": resident.unit_number,
        "resident_type": resident.resident_type.value,
        "customer_type": resident.customer_type.value,
        "status": resident.status.value,
        "debt": float(opening_debt),
        "debt_utility": float(opening_breakdown["utility"]),
        "debt_service": float(opening_breakdown["service"]),
        "debt_rent": float(opening_breakdown["rent"]),
        "owner_full_name": resident.owner_full_name,
        "owner_phone": resident.owner_phone,
        "owner_email": resident.owner_email,
        "comment": resident.comment,
        "meters": meters_data,
        "created_at": resident.created_at,
    }


@router.get("/{resident_id}", response_model=ResidentOut)
def get_resident_api(
    resident_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """
    Получение данных резидента для редактирования.
    """
    from sqlalchemy.orm import selectinload
    
    # Загружаем резидента - используем selectinload для избежания дубликатов
    resident = db.query(Resident).options(selectinload(Resident.meters)).filter(Resident.id == resident_id).first()
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")
    
    # Теперь resident.meters не должен содержать дубликатов благодаря selectinload
    # Но оставляем проверку на всякий случай
    seen_meter_ids = set()
    meters_data = []
    
    for m in resident.meters:
        # Показываем только активные счетчики (чтобы избежать дубликатов от старых неактивных)
        if not m.is_active:
            continue
        # Пропускаем дубликаты по ID (на случай, если они все еще есть)
        if m.id in seen_meter_ids:
            continue
        seen_meter_ids.add(m.id)
        
        meters_data.append({
            "id": m.id,
            "meter_type": m.meter_type.value,
            "serial_number": m.serial_number,
            "initial_reading": float(m.initial_reading),
            "tariff_id": m.tariff_id,
            "tariff_name": m.tariff.name if m.tariff else None,
            "is_active": m.is_active,
        })

    opening_breakdown = _get_opening_debt_breakdown(db, resident.id)
    opening_debt = _opening_debt_total(opening_breakdown)

    return {
        "id": resident.id,
        "block_id": resident.block_id,
        "block_name": resident.block.name if resident.block else None,
        "unit_number": resident.unit_number,
        "resident_type": resident.resident_type.value,
        "customer_type": resident.customer_type.value,
        "status": resident.status.value,
        "debt": float(opening_debt),
        "debt_utility": float(opening_breakdown["utility"]),
        "debt_service": float(opening_breakdown["service"]),
        "debt_rent": float(opening_breakdown["rent"]),
        "owner_full_name": resident.owner_full_name,
        "owner_phone": resident.owner_phone,
        "owner_email": resident.owner_email,
        "comment": resident.comment,
        "meters": meters_data,
        "created_at": resident.created_at,
    }


# ВРЕМЕННО: endpoint без авторизации для теста
@router.get("/{resident_id}/public", response_model=ResidentOut)
def get_resident_public(
    resident_id: int,
    db: Session = Depends(get_db),
):
    """
    ВРЕМЕННЫЙ endpoint без авторизации для теста SPA.
    TODO: удалить после настройки нормальной авторизации.
    """
    from sqlalchemy.orm import selectinload
    
    # Загружаем резидента - используем selectinload для избежания дубликатов
    resident = db.query(Resident).options(selectinload(Resident.meters)).filter(Resident.id == resident_id).first()
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")
    
    # Теперь resident.meters не должен содержать дубликатов благодаря selectinload
    # Но оставляем проверку на всякий случай
    seen_meter_ids = set()
    meters_data = []
    
    for m in resident.meters:
        # Показываем только активные счетчики (чтобы избежать дубликатов от старых неактивных)
        if not m.is_active:
            continue
        # Пропускаем дубликаты по ID (на случай, если они все еще есть)
        if m.id in seen_meter_ids:
            continue
        seen_meter_ids.add(m.id)
        
        meters_data.append({
            "id": m.id,
            "meter_type": m.meter_type.value,
            "serial_number": m.serial_number,
            "initial_reading": float(m.initial_reading),
            "tariff_id": m.tariff_id,
            "tariff_name": m.tariff.name if m.tariff else None,
            "is_active": m.is_active,
        })

    opening_breakdown = _get_opening_debt_breakdown(db, resident.id)
    opening_debt = _opening_debt_total(opening_breakdown)

    return {
        "id": resident.id,
        "block_id": resident.block_id,
        "block_name": resident.block.name if resident.block else None,
        "unit_number": resident.unit_number,
        "resident_type": resident.resident_type.value,
        "customer_type": resident.customer_type.value,
        "status": resident.status.value,
        "debt": float(opening_debt),
        "debt_utility": float(opening_breakdown["utility"]),
        "debt_service": float(opening_breakdown["service"]),
        "debt_rent": float(opening_breakdown["rent"]),
        "owner_full_name": resident.owner_full_name,
        "owner_phone": resident.owner_phone,
        "owner_email": resident.owner_email,
        "comment": resident.comment,
        "meters": meters_data,
        "created_at": resident.created_at,
    }


@router.put("/{resident_id}", response_model=ResidentOut)
def update_resident_api(
    resident_id: int,
    payload: ResidentUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """
    Обновление резидента (полная замена счётчиков).
    """
    resident = db.get(Resident, resident_id)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")
    
    # Обновление полей
    if payload.block_id is not None:
        block = db.get(Block, payload.block_id)
        if not block:
            raise HTTPException(status_code=404, detail="Block not found")
        resident.block_id = payload.block_id
    
    if payload.unit_number is not None:
        unit_number = payload.unit_number.strip()
        if not unit_number:
            raise HTTPException(status_code=400, detail="Unit number cannot be empty")
        # Проверка уникальности (исключая текущую запись)
        existing = db.execute(
            select(Resident.id).where(
                Resident.id != resident.id,
                Resident.block_id == resident.block_id,
                Resident.unit_number == unit_number
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=400, detail="Unit number already exists in this block")
        resident.unit_number = unit_number
    
    if payload.resident_type is not None:
        if payload.resident_type not in {t.value for t in ResidentType}:
            raise HTTPException(status_code=400, detail=f"Invalid resident_type: {payload.resident_type}")
        resident.resident_type = ResidentType(payload.resident_type)
    
    if payload.customer_type is not None:
        if payload.customer_type not in {c.value for c in CustomerType}:
            raise HTTPException(status_code=400, detail=f"Invalid customer_type: {payload.customer_type}")
        resident.customer_type = CustomerType(payload.customer_type)
    
    if payload.status is not None:
        if payload.status not in {s.value for s in ResidentStatus}:
            raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")
        resident.status = ResidentStatus(payload.status)
    
    if payload.owner_full_name is not None:
        resident.owner_full_name = payload.owner_full_name.strip() if payload.owner_full_name else None
    if payload.owner_phone is not None:
        resident.owner_phone = payload.owner_phone.strip() if payload.owner_phone else None
    if payload.owner_email is not None:
        resident.owner_email = payload.owner_email.strip() if payload.owner_email else None
    if payload.comment is not None:
        resident.comment = payload.comment.strip() if payload.comment else None

    # ---- Начальный долг (opening invoice) ----
    if _has_opening_debt_payload(payload):
        opening_breakdown = _extract_opening_debt_breakdown(payload)
        _upsert_opening_invoice(
            db=db,
            resident_id=resident.id,
            breakdown=opening_breakdown,
            created_by_id=actor.id,
        )
    
    # Обновление/синхронизация счётчиков (БЕЗ физического удаления тех, у кого есть показания)
    # Это предотвращает "пропадание показаний" из-за FK resident_meter_id ON DELETE CASCADE.
    if payload.meters is not None:
        try:
            incoming = _parse_meters(payload.meters)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        existing_meters: list[ResidentMeter] = (
            db.query(ResidentMeter)
            .filter(ResidentMeter.resident_id == resident.id)
            .all()
        )
        existing_by_id = {m.id: m for m in existing_meters}

        if existing_by_id:
            meters_with_readings = {
                mid for (mid,) in db.query(MeterReading.resident_meter_id)
                .filter(MeterReading.resident_meter_id.in_(list(existing_by_id.keys())))
                .distinct()
                .all()
            }
        else:
            meters_with_readings = set()

        seen_existing_ids: set[int] = set()

        def validate_tariff(tariff_id: int, meter_type_enum: MeterType, existing_meter: ResidentMeter | None):
            tariff = db.get(Tariff, tariff_id)
            if not tariff:
                raise HTTPException(status_code=404, detail=f"Tariff {tariff_id} not found")
            if tariff.meter_type != meter_type_enum:
                raise HTTPException(status_code=400, detail=f"Tariff {tariff_id} meter_type mismatch")
            # Не даём назначать неактивные тарифы на новые/изменённые счётчики.
            # Но разрешаем сохранить старую привязку (если она уже была).
            if not tariff.is_active:
                if not (existing_meter and existing_meter.tariff_id == tariff_id):
                    raise HTTPException(status_code=400, detail=f"Tariff {tariff_id} is inactive")
            return tariff

        # 1) Upsert incoming meters
        for m_data in incoming:
            meter_id = m_data.get("id")
            meter_type = m_data["meter_type"]
            serial = m_data["serial"]
            initial_reading = m_data["initial_reading"]
            tariff_id = m_data["tariff_id"]

            existing_meter: ResidentMeter | None = None
            if meter_id is not None:
                existing_meter = existing_by_id.get(int(meter_id))
                if not existing_meter or existing_meter.resident_id != resident.id:
                    raise HTTPException(status_code=400, detail=f"Meter {meter_id} not found for this resident")
            else:
                # Best-effort match for backwards compatibility (older SPA payload without meter id)
                # Prefer active meters first to avoid accidentally reactivating historical ones.
                candidates = [
                    em for em in existing_meters
                    if em.meter_type == meter_type
                    and (em.serial_number or "") == (serial or "")
                    and em.tariff_id == tariff_id
                ]
                candidates = sorted(candidates, key=lambda x: (not x.is_active, x.id))
                existing_meter = candidates[0] if candidates else None

            validate_tariff(tariff_id, meter_type, existing_meter)

            if existing_meter:
                has_readings = existing_meter.id in meters_with_readings
                meter_type_changed = existing_meter.meter_type != meter_type

                # Если у счётчика есть показания и меняется тип, сохраняем историю:
                # старый деактивируем, новый создаём как отдельный.
                if has_readings and meter_type_changed:
                    existing_meter.is_active = False
                    db.add(ResidentMeter(
                        resident_id=resident.id,
                        meter_type=meter_type,
                        serial_number=serial,
                        initial_reading=initial_reading,
                        tariff_id=tariff_id,
                        is_active=True,
                    ))
                    seen_existing_ids.add(existing_meter.id)
                    continue

                # Обновляем существующий счётчик (включая смену типа, если показаний ещё нет)
                existing_meter.is_active = True
                existing_meter.meter_type = meter_type
                existing_meter.serial_number = serial
                existing_meter.tariff_id = tariff_id

                # Не меняем initial_reading, если есть показания — иначе можем сломать базовую логику "опорного"
                if not has_readings:
                    existing_meter.initial_reading = initial_reading
                seen_existing_ids.add(existing_meter.id)
            else:
                # Создаём новый счётчик
                db.add(ResidentMeter(
                    resident_id=resident.id,
                    meter_type=meter_type,
                    serial_number=serial,
                    initial_reading=initial_reading,
                    tariff_id=tariff_id,
                    is_active=True,
                ))

        # 2) Deactivate (or delete) meters that are missing in incoming payload
        for em in existing_meters:
            if em.id in seen_existing_ids:
                continue
            if em.id in meters_with_readings:
                em.is_active = False
            else:
                db.delete(em)
    
    db.commit()
    db.refresh(resident)
    
    # Формирование ответа - показываем только активные счетчики
    meters_data = []
    seen_meter_ids = set()
    for m in resident.meters:
        # Показываем только активные счетчики (чтобы избежать дубликатов)
        if not m.is_active:
            continue
        # Пропускаем дубликаты по ID
        if m.id in seen_meter_ids:
            continue
        seen_meter_ids.add(m.id)
        
        meters_data.append({
            "id": m.id,
            "meter_type": m.meter_type.value,
            "serial_number": m.serial_number,
            "initial_reading": float(m.initial_reading),
            "tariff_id": m.tariff_id,
            "tariff_name": m.tariff.name if m.tariff else None,
            "is_active": m.is_active,
        })
    opening_breakdown = _get_opening_debt_breakdown(db, resident.id)
    opening_debt = _opening_debt_total(opening_breakdown)

    return {
        "id": resident.id,
        "block_id": resident.block_id,
        "block_name": resident.block.name if resident.block else None,
        "unit_number": resident.unit_number,
        "resident_type": resident.resident_type.value,
        "customer_type": resident.customer_type.value,
        "status": resident.status.value,
        "debt": float(opening_debt),
        "debt_utility": float(opening_breakdown["utility"]),
        "debt_service": float(opening_breakdown["service"]),
        "debt_rent": float(opening_breakdown["rent"]),
        "owner_full_name": resident.owner_full_name,
        "owner_phone": resident.owner_phone,
        "owner_email": resident.owner_email,
        "comment": resident.comment,
        "meters": meters_data,
        "created_at": resident.created_at,
    }


# ВРЕМЕННО: endpoint без авторизации для теста
@router.put("/{resident_id}/public", response_model=ResidentOut)
def update_resident_public(
    resident_id: int,
    payload: ResidentUpdate,
    db: Session = Depends(get_db),
):
    """
    ВРЕМЕННЫЙ endpoint без авторизации для теста SPA.
    TODO: удалить после настройки нормальной авторизации.
    """
    resident = db.get(Resident, resident_id)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")
    
    # Обновление полей
    if payload.block_id is not None:
        block = db.get(Block, payload.block_id)
        if not block:
            raise HTTPException(status_code=404, detail="Block not found")
        resident.block_id = payload.block_id
    
    if payload.unit_number is not None:
        unit_number = payload.unit_number.strip()
        if not unit_number:
            raise HTTPException(status_code=400, detail="Unit number cannot be empty")
        # Проверка уникальности (исключая текущую запись)
        existing = db.execute(
            select(Resident.id).where(
                Resident.id != resident.id,
                Resident.block_id == resident.block_id,
                Resident.unit_number == unit_number
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=400, detail="Unit number already exists in this block")
        resident.unit_number = unit_number
    
    if payload.resident_type is not None:
        if payload.resident_type not in {t.value for t in ResidentType}:
            raise HTTPException(status_code=400, detail=f"Invalid resident_type: {payload.resident_type}")
        resident.resident_type = ResidentType(payload.resident_type)
    
    if payload.customer_type is not None:
        if payload.customer_type not in {c.value for c in CustomerType}:
            raise HTTPException(status_code=400, detail=f"Invalid customer_type: {payload.customer_type}")
        resident.customer_type = CustomerType(payload.customer_type)
    
    if payload.status is not None:
        if payload.status not in {s.value for s in ResidentStatus}:
            raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")
        resident.status = ResidentStatus(payload.status)
    
    if payload.owner_full_name is not None:
        resident.owner_full_name = payload.owner_full_name.strip() if payload.owner_full_name else None
    if payload.owner_phone is not None:
        resident.owner_phone = payload.owner_phone.strip() if payload.owner_phone else None
    if payload.owner_email is not None:
        resident.owner_email = payload.owner_email.strip() if payload.owner_email else None
    if payload.comment is not None:
        resident.comment = payload.comment.strip() if payload.comment else None

    # ---- Начальный долг (opening invoice) ----
    if _has_opening_debt_payload(payload):
        opening_breakdown = _extract_opening_debt_breakdown(payload)
        _upsert_opening_invoice(
            db=db,
            resident_id=resident.id,
            breakdown=opening_breakdown,
            created_by_id=None,
        )
    
    # Обновление/синхронизация счётчиков (БЕЗ физического удаления тех, у кого есть показания)
    if payload.meters is not None:
        try:
            incoming = _parse_meters(payload.meters)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        existing_meters: list[ResidentMeter] = (
            db.query(ResidentMeter)
            .filter(ResidentMeter.resident_id == resident.id)
            .all()
        )
        existing_by_id = {m.id: m for m in existing_meters}

        if existing_by_id:
            meters_with_readings = {
                mid for (mid,) in db.query(MeterReading.resident_meter_id)
                .filter(MeterReading.resident_meter_id.in_(list(existing_by_id.keys())))
                .distinct()
                .all()
            }
        else:
            meters_with_readings = set()

        seen_existing_ids: set[int] = set()

        def validate_tariff(tariff_id: int, meter_type_enum: MeterType, existing_meter: ResidentMeter | None):
            tariff = db.get(Tariff, tariff_id)
            if not tariff:
                raise HTTPException(status_code=404, detail=f"Tariff {tariff_id} not found")
            if tariff.meter_type != meter_type_enum:
                raise HTTPException(status_code=400, detail=f"Tariff {tariff_id} meter_type mismatch")
            if not tariff.is_active:
                if not (existing_meter and existing_meter.tariff_id == tariff_id):
                    raise HTTPException(status_code=400, detail=f"Tariff {tariff_id} is inactive")
            return tariff

        for m_data in incoming:
            meter_id = m_data.get("id")
            meter_type = m_data["meter_type"]
            serial = m_data["serial"]
            initial_reading = m_data["initial_reading"]
            tariff_id = m_data["tariff_id"]

            existing_meter: ResidentMeter | None = None
            if meter_id is not None:
                existing_meter = existing_by_id.get(int(meter_id))
                if not existing_meter or existing_meter.resident_id != resident.id:
                    raise HTTPException(status_code=400, detail=f"Meter {meter_id} not found for this resident")
            else:
                candidates = [
                    em for em in existing_meters
                    if em.meter_type == meter_type
                    and (em.serial_number or "") == (serial or "")
                    and em.tariff_id == tariff_id
                ]
                candidates = sorted(candidates, key=lambda x: (not x.is_active, x.id))
                existing_meter = candidates[0] if candidates else None

            validate_tariff(tariff_id, meter_type, existing_meter)

            if existing_meter:
                has_readings = existing_meter.id in meters_with_readings
                meter_type_changed = existing_meter.meter_type != meter_type

                # Если у счётчика есть показания и меняется тип, сохраняем историю:
                # старый деактивируем, новый создаём как отдельный.
                if has_readings and meter_type_changed:
                    existing_meter.is_active = False
                    db.add(ResidentMeter(
                        resident_id=resident.id,
                        meter_type=meter_type,
                        serial_number=serial,
                        initial_reading=initial_reading,
                        tariff_id=tariff_id,
                        is_active=True,
                    ))
                    seen_existing_ids.add(existing_meter.id)
                    continue

                existing_meter.is_active = True
                existing_meter.meter_type = meter_type
                existing_meter.serial_number = serial
                existing_meter.tariff_id = tariff_id
                if not has_readings:
                    existing_meter.initial_reading = initial_reading
                seen_existing_ids.add(existing_meter.id)
            else:
                db.add(ResidentMeter(
                    resident_id=resident.id,
                    meter_type=meter_type,
                    serial_number=serial,
                    initial_reading=initial_reading,
                    tariff_id=tariff_id,
                    is_active=True,
                ))

        for em in existing_meters:
            if em.id in seen_existing_ids:
                continue
            if em.id in meters_with_readings:
                em.is_active = False
            else:
                db.delete(em)
    
    db.commit()
    db.refresh(resident)
    
    # Формирование ответа - показываем только активные счетчики
    meters_data = []
    seen_meter_ids = set()
    for m in resident.meters:
        # Показываем только активные счетчики (чтобы избежать дубликатов)
        if not m.is_active:
            continue
        # Пропускаем дубликаты по ID
        if m.id in seen_meter_ids:
            continue
        seen_meter_ids.add(m.id)
        
        meters_data.append({
            "id": m.id,
            "meter_type": m.meter_type.value,
            "serial_number": m.serial_number,
            "initial_reading": float(m.initial_reading),
            "tariff_id": m.tariff_id,
            "tariff_name": m.tariff.name if m.tariff else None,
            "is_active": m.is_active,
        })
    
    opening_breakdown = _get_opening_debt_breakdown(db, resident.id)
    opening_debt = _opening_debt_total(opening_breakdown)

    return {
        "id": resident.id,
        "block_id": resident.block_id,
        "block_name": resident.block.name if resident.block else None,
        "unit_number": resident.unit_number,
        "resident_type": resident.resident_type.value,
        "customer_type": resident.customer_type.value,
        "status": resident.status.value,
        "debt": float(opening_debt),
        "debt_utility": float(opening_breakdown["utility"]),
        "debt_service": float(opening_breakdown["service"]),
        "debt_rent": float(opening_breakdown["rent"]),
        "owner_full_name": resident.owner_full_name,
        "owner_phone": resident.owner_phone,
        "owner_email": resident.owner_email,
        "comment": resident.comment,
        "meters": meters_data,
        "created_at": resident.created_at,
    }


@router.delete("/{resident_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_resident_api(
    resident_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """
    Удаление резидента.
    """
    resident = db.get(Resident, resident_id)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")
    
    db.delete(resident)
    db.commit()
    return None


# ВРЕМЕННО: endpoint без авторизации для теста
@router.delete("/{resident_id}/public", status_code=status.HTTP_204_NO_CONTENT)
def delete_resident_public(
    resident_id: int,
    db: Session = Depends(get_db),
):
    """
    ВРЕМЕННЫЙ endpoint без авторизации для теста SPA.
    TODO: удалить после настройки нормальной авторизации.
    """
    resident = db.get(Resident, resident_id)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")
    
    db.delete(resident)
    db.commit()
    return None

