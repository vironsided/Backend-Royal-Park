from typing import List, Optional
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from io import BytesIO
import json
import os
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status, Query, Request, UploadFile, File, Form
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, or_, exists

from ..database import get_db
from ..models import (
    User, RoleEnum, Block, Resident, ResidentMeter,
    MeterType, Tariff, MeterReading, ReadingLog, MeterReadingPhoto,
    Invoice, InvoiceLine, InvoiceStatus, CustomerType, PaymentApplication
)
from ..deps import get_current_user
from .api_payment_logic import auto_apply_advance


router = APIRouter(prefix="/api/readings", tags=["readings-api"])

def money(x: Decimal) -> Decimal:
    """Округление денег до 2 знаков."""
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _effective_sewerage_percent(tariff: Tariff | None) -> Decimal:
    """
    Канализация берётся только из WATER-тарифа.
    Если % не задан (0) — авто-канализация не применяется.
    """
    if not tariff or tariff.meter_type != MeterType.WATER:
        return Decimal("0")
    try:
        percent = Decimal(str(getattr(tariff, "sewerage_percent", 0) or 0))
    except Exception:
        percent = Decimal("0")
    return percent if percent > 0 else Decimal("0")


def _apply_consumption_multiplier(consumption: Decimal, tariff: Tariff | None, meter_type: MeterType | None) -> Decimal:
    """
    Умножает расход на коэффициент тарифа (если включено).
    Применяем только к счётчикам с показаниями (не SERVICE/RENT/CONSTRUCTION).
    """
    base = Decimal(str(consumption or 0))
    if not tariff or meter_type != MeterType.ELECTRIC:
        return base
    if not bool(getattr(tariff, "use_multiplier", False)):
        return base
    try:
        coeff = Decimal(str(getattr(tariff, "consumption_multiplier", 1) or 1))
    except Exception:
        coeff = Decimal("1")
    if coeff <= 0:
        coeff = Decimal("1")
    return base * coeff


def _is_period_paid(db: Session, resident_id: int, year: int, month: int) -> bool:
    inv = (
        db.query(Invoice.id)
        .filter(
            Invoice.resident_id == resident_id,
            Invoice.period_year == year,
            Invoice.period_month == month,
            Invoice.status == InvoiceStatus.PAID,
        )
        .first()
    )
    return bool(inv)


def _is_water_line_desc(desc: str | None) -> bool:
    low = (desc or "").lower()
    return ("вода" in low) or ("water" in low) or ("meter_cold_water" in low)


def _is_sewer_line_desc(desc: str | None) -> bool:
    low = (desc or "").lower()
    return ("канализац" in low) or ("sewerage" in low) or ("meter_sewerage" in low)


def _invoice_line_payment_state_for_period(
    db: Session,
    resident_id: int,
    year: int,
    month: int,
) -> dict[int, dict]:
    """
    Детализация оплаты по строкам инвойса за период.
    Возвращает карту: line_id -> {meter_reading_id, description, line_total, paid_amount, remaining_amount}.
    """
    inv = (
        db.query(Invoice)
        .filter(
            Invoice.resident_id == resident_id,
            Invoice.period_year == year,
            Invoice.period_month == month,
            Invoice.status != InvoiceStatus.CANCELED,
        )
        .first()
    )
    if not inv:
        return {}

    apps = (
        db.query(PaymentApplication)
        .filter(PaymentApplication.invoice_id == inv.id)
        .order_by(PaymentApplication.created_at.asc(), PaymentApplication.id.asc())
        .all()
    )
    all_lines = (
        db.query(InvoiceLine)
        .filter(InvoiceLine.invoice_id == inv.id)
        .order_by(InvoiceLine.id.asc())
        .all()
    )
    if not all_lines:
        return {}

    line_totals: dict[int, Decimal] = {
        int(line.id): money(Decimal(str(line.amount_total or 0)))
        for line in all_lines
        if line.id is not None
    }
    paid_by_line: dict[int, Decimal] = {lid: Decimal("0") for lid in line_totals}
    line_desc_by_id: dict[int, str] = {
        int(line.id): (line.description or "")
        for line in all_lines
        if line.id is not None
    }
    line_reading_id_by_id: dict[int, int | None] = {
        int(line.id): (int(line.meter_reading_id) if line.meter_reading_id is not None else None)
        for line in all_lines
        if line.id is not None
    }

    def _parse_selected_line_ids(reference: str | None) -> list[int]:
        if not reference:
            return []
        marker = "LINESEL:"
        idx = reference.find(marker)
        if idx < 0:
            return []
        raw = reference[idx + len(marker):].split("|")[0]
        out: list[int] = []
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                out.append(int(token))
            except Exception:
                continue
        return out

    def _is_water_line_desc(desc: str | None) -> bool:
        low = (desc or "").lower()
        return ("вода" in low) or ("water" in low) or ("meter_cold_water" in low)

    def _is_sewer_line_desc(desc: str | None) -> bool:
        low = (desc or "").lower()
        return ("канализац" in low) or ("sewerage" in low) or ("meter_sewerage" in low)

    def _normalize_selected_ids(selected_ids: list[int]) -> list[int]:
        ids = [int(x) for x in (selected_ids or []) if int(x) > 0]
        if not ids:
            return []
        unique_ids = list(dict.fromkeys(ids))
        selected_has_bundle = any(
            _is_water_line_desc(line_desc_by_id.get(lid)) or _is_sewer_line_desc(line_desc_by_id.get(lid))
            for lid in unique_ids
        )
        if not selected_has_bundle:
            return unique_ids

        sewer_ids = [lid for lid, desc in line_desc_by_id.items() if _is_sewer_line_desc(desc)]
        water_ids = [lid for lid, desc in line_desc_by_id.items() if _is_water_line_desc(desc)]
        if not sewer_ids or not water_ids:
            return unique_ids

        for lid in sewer_ids + water_ids:
            if lid not in unique_ids:
                unique_ids.append(lid)

        def _key(lid: int) -> tuple[int, int]:
            if lid in sewer_ids:
                return (0, unique_ids.index(lid))
            if lid in water_ids:
                return (1, unique_ids.index(lid))
            return (2, unique_ids.index(lid))

        return [lid for lid in sorted(unique_ids, key=_key)]

    def _allocate(amount: Decimal, line_ids: list[int]) -> None:
        remaining_amt = Decimal(str(amount or 0))
        for lid in line_ids:
            if remaining_amt <= 0:
                break
            if lid not in line_totals:
                continue
            cap = max(line_totals[lid] - paid_by_line[lid], Decimal("0"))
            if cap <= 0:
                continue
            take = min(cap, remaining_amt)
            paid_by_line[lid] += take
            remaining_amt -= take

    default_order = sorted(line_totals.keys())
    for app in apps:
        amt = Decimal(str(app.amount_applied or 0))
        if amt <= 0:
            continue
        selected_ids = _parse_selected_line_ids(getattr(app, "reference", None))
        if selected_ids:
            _allocate(amt, _normalize_selected_ids(selected_ids))
        else:
            _allocate(amt, default_order)

    result: dict[int, dict] = {}
    for lid, total in line_totals.items():
        paid = money(paid_by_line.get(lid, Decimal("0")))
        remaining = max(total - paid, Decimal("0"))
        result[lid] = {
            "meter_reading_id": line_reading_id_by_id.get(lid),
            "description": line_desc_by_id.get(lid) or "",
            "line_total": float(total),
            "paid_amount": float(paid),
            "remaining_amount": float(money(remaining)),
        }
    return result


def _meter_reading_payment_lock_map(
    db: Session,
    resident_id: int,
    year: int,
    month: int,
) -> dict[int, dict]:
    """
    Возвращает карту блокировок по meter_reading_id для периода инвойса.
    Логика распределения оплаты:
    - берём фактически оплаченный total по инвойсу;
    - последовательно покрываем строки InvoiceLine (FIFO по id);
    - строка считается заблокированной, если покрыта полностью.
    - Авто-канализация (meter_reading_id = None) учитывается первой и блокирует WATER показания.
    """
    line_state = _invoice_line_payment_state_for_period(db, resident_id, year, month)
    if not line_state:
        return {}

    lock_map: dict[int, dict] = {}
    for _, state in line_state.items():
        meter_reading_id = state.get("meter_reading_id")
        if meter_reading_id is None:
            continue
        remaining = Decimal(str(state.get("remaining_amount", 0) or 0))
        lock_map[int(meter_reading_id)] = {
            "locked": remaining <= Decimal("0.0001"),
            "line_total": float(state.get("line_total", 0) or 0),
            "paid_amount": float(state.get("paid_amount", 0) or 0),
            "remaining_amount": float(state.get("remaining_amount", 0) or 0),
        }

    return lock_map


def _upsert_auto_sewerage_line_for_invoice(
    db: Session,
    inv: Invoice,
    all_period_readings: list[MeterReading],
) -> None:
    """
    Авто-канализация как % от воды:
    - если есть реальные SEWERAGE-показания, авто-строку удаляем;
    - иначе считаем по WATER-тарифам (Tariff.sewerage_percent).
    """
    if not inv or not inv.id:
        return

    readings = all_period_readings or []
    has_real_sewerage = any(
        (rd.resident_meter and rd.resident_meter.meter_type == MeterType.SEWERAGE)
        for rd in readings
    )

    auto_line = (
        db.query(InvoiceLine)
        .filter(
            InvoiceLine.invoice_id == inv.id,
            InvoiceLine.meter_reading_id.is_(None),
            InvoiceLine.description.ilike("Канализация%"),
        )
        .first()
    )

    if has_real_sewerage:
        # Восстанавливаем WATER-строки как исходные начисления из MeterReading.
        for rd in readings:
            if rd.resident_meter and rd.resident_meter.meter_type == MeterType.WATER:
                line = (
                    db.query(InvoiceLine)
                    .filter(InvoiceLine.invoice_id == inv.id, InvoiceLine.meter_reading_id == rd.id)
                    .first()
                )
                if line:
                    line.amount_net = money(Decimal(str(rd.amount_net or 0)))
                    line.amount_vat = money(Decimal(str(rd.amount_vat or 0)))
                    line.amount_total = money(Decimal(str(rd.amount_total or 0)))
                    line.description = f"Вода {float(Decimal(str(rd.consumption or 0)))} м³"
        if auto_line:
            db.delete(auto_line)
        return

    water_readings = [
        rd for rd in readings
        if rd.resident_meter and rd.resident_meter.meter_type == MeterType.WATER
    ]
    if not water_readings:
        if auto_line:
            db.delete(auto_line)
        return

    sew_net = Decimal("0")
    sew_vat = Decimal("0")
    sew_total = Decimal("0")
    sewer_cons = Decimal("0")

    for rd in water_readings:
        line = (
            db.query(InvoiceLine)
            .filter(InvoiceLine.invoice_id == inv.id, InvoiceLine.meter_reading_id == rd.id)
            .first()
        )
        if not line:
            continue

        base_net = money(Decimal(str(rd.amount_net or 0)))
        base_vat = money(Decimal(str(rd.amount_vat or 0)))
        base_total = money(Decimal(str(rd.amount_total or 0)))

        t = db.get(Tariff, rd.tariff_id) if rd.tariff_id else None
        percent = _effective_sewerage_percent(t)
        if percent <= 0:
            line.amount_net = base_net
            line.amount_vat = base_vat
            line.amount_total = base_total
            line.description = f"Вода {float(Decimal(str(rd.consumption or 0)))} м³"
            continue

        k = percent / Decimal("100")
        consumption = Decimal(str(rd.consumption or 0))

        if getattr(t, "customer_type", None) == CustomerType.INDIVIDUAL:
            # Для физлиц делим WATER-строку на воду и канализацию (без роста total).
            new_net = money(base_net * (Decimal("1") - k))
            new_vat = money(base_vat * (Decimal("1") - k))
            new_total = money(base_total * (Decimal("1") - k))

            line.amount_net = new_net
            line.amount_vat = new_vat
            line.amount_total = new_total

            sew_net += (base_net - new_net)
            sew_vat += (base_vat - new_vat)
            sew_total += (base_total - new_total)

            water_display_cons = consumption * (Decimal("1") - k)
            sewer_display_cons = consumption * k
            line.description = f"Вода {float(water_display_cons)} м³"
            sewer_cons += sewer_display_cons
        else:
            # Для юрлиц WATER остаётся как есть, канализация добавляется сверху.
            line.amount_net = base_net
            line.amount_vat = base_vat
            line.amount_total = base_total

            sew_net += money(base_net * k)
            sew_vat += money(base_vat * k)
            sew_total += money(base_total * k)
            line.description = f"Вода {float(consumption)} м³"
            sewer_cons += consumption * k

    if sew_total <= 0:
        if auto_line:
            db.delete(auto_line)
        return

    # Для инвойса канализация выводится без объёма (м3).
    desc = "Канализация"
    if auto_line:
        auto_line.description = desc
        auto_line.amount_net = money(sew_net)
        auto_line.amount_vat = money(sew_vat)
        auto_line.amount_total = money(sew_total)
    else:
        db.add(InvoiceLine(
            invoice_id=inv.id,
            meter_reading_id=None,
            description=desc,
            amount_net=money(sew_net),
            amount_vat=money(sew_vat),
            amount_total=money(sew_total),
        ))


def get_gas_annual_prev(db: Session, meter_id: int, period_start: datetime) -> Decimal:
    """Газ: годовой объём ДО начала периода (для накопительного тарифа)."""
    year_start = datetime(period_start.year, 1, 1)
    total = db.query(func.coalesce(func.sum(MeterReading.consumption), 0)).filter(
        MeterReading.resident_meter_id == meter_id,
        MeterReading.reading_date >= year_start,
        MeterReading.reading_date < period_start,
    ).scalar()
    return Decimal(str(total or 0))


def compute_amount_components(
    consumption: Decimal,
    tariff: Tariff,
    annual_prev: Decimal | None = None,
) -> dict:
    """
    Возвращает (amount_net, amount_vat, amount_total, breakdown[])
    Для CONSTRUCTION тарифов (date-based) используем первую ступень с ценой.
    """
    left = Decimal(consumption)
    total_net = Decimal("0")
    breakdown = []

    # Для тарифов с date-based steps (CONSTRUCTION) - используем первую ступень
    if tariff.meter_type == MeterType.CONSTRUCTION:
        if tariff.steps:
            first_step = tariff.steps[0]
            price = Decimal(first_step.price)
            part = left * price
            total_net = part
            breakdown.append({
                "from": None,
                "to": None,
                "qty": float(left),
                "price": float(price),
                "sum": float(money(part)),
            })
    elif tariff.meter_type == MeterType.GAS and annual_prev is not None:
        # Газ: ступени считаются по годовому объёму (накопительно)
        cursor = Decimal(annual_prev)
        remaining = Decimal(consumption)
        for st in tariff.steps:
            if remaining <= 0:
                break
            st_from = Decimal(st.from_value) if st.from_value is not None else Decimal("0")
            st_to = Decimal(st.to_value) if st.to_value is not None else None
            price = Decimal(st.price)

            if st_to is not None and cursor >= st_to:
                continue

            step_start = max(cursor, st_from)
            if st_to is None:
                capacity = remaining
            else:
                capacity = max(Decimal("0"), st_to - step_start)

            chunk = remaining if remaining <= capacity else capacity
            if chunk > 0:
                part = chunk * price
                total_net += part
                breakdown.append({
                    "from": float(st_from),
                    "to": (None if st_to is None else float(st_to)),
                    "qty": float(chunk),
                    "price": float(price),
                    "sum": float(money(part)),
                })
                remaining -= chunk
                cursor += chunk
    else:
        # Обычные тарифы с value-based steps
        for st in tariff.steps:
            if left <= 0:
                break
            st_from = Decimal("0") if st.from_value is None else Decimal(st.from_value)
            st_to = Decimal(st.to_value) if st.to_value is not None else None
            price = Decimal(st.price)

            if st_to is None:
                chunk = left
            else:
                width = st_to - st_from
                chunk = left if left <= width else width

            if chunk > 0:
                part = chunk * price
                total_net += part
                breakdown.append({
                    "from": float(st_from),
                    "to": (None if st_to is None else float(st_to)),
                    "qty": float(chunk),
                    "price": float(price),
                    "sum": float(money(part)),
                })
                left -= chunk

    # Фиксированная часть (stable_tariff):
    # stable_tariff в тарифе хранится как ИТОГОВАЯ сумма (gross).
    # Поэтому при ненулевом НДС раскладываем её на net+vat так,
    # чтобы total всегда оставался равным stable_tariff.
    try:
        stable_fee = Decimal(str(getattr(tariff, "stable_tariff", 0) or 0))
    except Exception:
        stable_fee = Decimal("0")
    if stable_fee < 0:
        stable_fee = Decimal("0")

    try:
        vat_percent = Decimal(str(getattr(tariff, "vat_percent", 0) or 0))
    except Exception:
        vat_percent = Decimal("0")
    if vat_percent < 0:
        vat_percent = Decimal("0")

    variable_vat = money(total_net * vat_percent / Decimal(100))
    variable_total = money(total_net + variable_vat)

    # stable_total is entered by accounting (e.g. 1.00) and must stay unchanged.
    stable_total = money(stable_fee)
    if stable_total > 0 and vat_percent > 0:
        stable_net = money(stable_total * Decimal(100) / (Decimal(100) + vat_percent))
        stable_vat = money(stable_total - stable_net)
    else:
        stable_net = stable_total
        stable_vat = Decimal("0")

    amount_net = money(total_net + stable_net)
    amount_vat = money(variable_vat + stable_vat)
    amount_total = money(variable_total + stable_total)
    return {
        "amount_net": amount_net,
        "amount_vat": amount_vat,
        "amount_total": amount_total,
        "breakdown": breakdown,
        "stable_net": stable_net,
        "stable_vat": stable_vat,
        "stable_total": stable_total,
    }


def compute_amount(
    consumption: Decimal,
    tariff: Tariff,
    annual_prev: Decimal | None = None,
) -> tuple[Decimal, Decimal, Decimal, list[dict]]:
    comp = compute_amount_components(consumption, tariff, annual_prev=annual_prev)
    return (
        comp["amount_net"],
        comp["amount_vat"],
        comp["amount_total"],
        comp["breakdown"],
    )


# Pydantic models
class MeterReadingOut(BaseModel):
    id: int
    resident_meter_id: int
    reading_date: datetime
    value: float
    consumption: float
    tariff_id: int
    tariff_name: Optional[str] = None
    amount_net: float
    vat_percent: int
    amount_vat: float
    amount_total: float
    note: Optional[str] = None

    class Config:
        from_attributes = True


class ResidentReadingRow(BaseModel):
    resident_id: int
    resident_code: str  # "A / 41"
    resident_info: str  # "Блок A, №41"
    block_name: str
    unit_number: str
    meters: List[dict]  # [{type, consumption, unit, total}]
    total_amount: float
    is_paid: bool


class ReadingCreateItem(BaseModel):
    meter_id: int
    new_value: Optional[float] = None  # None для фикс-услуг означает удаление


class ReadingCreate(BaseModel):
    resident_id: int
    date_str: str  # "YYYY-MM-DD"
    items: List[ReadingCreateItem]
    note: Optional[str] = None


PHOTO_TTL_DAYS = 90
PHOTO_COMPRESS_THRESHOLD_BYTES = int(os.getenv("METER_PHOTO_COMPRESS_THRESHOLD_BYTES", str(3 * 1024 * 1024)))
PHOTO_TARGET_MAX_BYTES = int(os.getenv("METER_PHOTO_TARGET_MAX_BYTES", str(1 * 1024 * 1024)))
PHOTO_JPEG_QUALITY_START = int(os.getenv("METER_PHOTO_JPEG_QUALITY_START", "85"))
PHOTO_JPEG_QUALITY_MIN = int(os.getenv("METER_PHOTO_JPEG_QUALITY_MIN", "45"))
PHOTO_JPEG_QUALITY_STEP = int(os.getenv("METER_PHOTO_JPEG_QUALITY_STEP", "10"))


def _compress_meter_photo_if_needed(raw_bytes: bytes, original_ext: str) -> tuple[bytes, str]:
    """
    Сжимает тяжёлые фото счётчиков.
    Если файл маленький или сжать не удалось — возвращаем исходный файл как есть.
    """
    if len(raw_bytes) <= PHOTO_COMPRESS_THRESHOLD_BYTES:
        return raw_bytes, original_ext

    try:
        with Image.open(BytesIO(raw_bytes)) as img:
            normalized = ImageOps.exif_transpose(img)
            if normalized.mode not in {"RGB", "L"}:
                normalized = normalized.convert("RGB")
            elif normalized.mode == "L":
                normalized = normalized.convert("RGB")

            best_bytes = raw_bytes
            quality = max(PHOTO_JPEG_QUALITY_MIN, PHOTO_JPEG_QUALITY_START)

            while quality >= PHOTO_JPEG_QUALITY_MIN:
                out = BytesIO()
                normalized.save(out, format="JPEG", quality=quality, optimize=True)
                candidate = out.getvalue()

                if len(candidate) < len(best_bytes):
                    best_bytes = candidate
                if len(candidate) <= PHOTO_TARGET_MAX_BYTES:
                    return candidate, ".jpg"

                quality -= max(1, PHOTO_JPEG_QUALITY_STEP)

            if len(best_bytes) < len(raw_bytes):
                return best_bytes, ".jpg"
            return raw_bytes, original_ext
    except (UnidentifiedImageError, OSError, ValueError):
        return raw_bytes, original_ext


def _photo_disk_path(photo: MeterReadingPhoto) -> str:
    return os.path.join("uploads", photo.file_path)


def cleanup_expired_meter_photos(db: Session) -> None:
    now = datetime.utcnow()
    expired = db.query(MeterReadingPhoto).filter(MeterReadingPhoto.expires_at <= now).all()
    for photo in expired:
        try:
            path = _photo_disk_path(photo)
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        db.delete(photo)


def delete_meter_photo_for_reading(db: Session, reading_id: int) -> None:
    photo = db.query(MeterReadingPhoto).filter(MeterReadingPhoto.meter_reading_id == reading_id).first()
    if not photo:
        return
    try:
        path = _photo_disk_path(photo)
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    db.delete(photo)


# ====== List readings ======
@router.get("/")
def list_readings(
    db: Session = Depends(get_db),
    block_id: Optional[int] = Query(None),
    resident_id: Optional[int] = Query(None),
    meter_type: Optional[List[str]] = Query(None),
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
):
    """
    Список показаний за выбранный месяц (агрегировано по резидентам).
    """
    blocks = db.query(Block).order_by(Block.name.asc()).all()

    # Выпадашка резидентов (по блоку/поиску)
    residents_q = db.query(Resident).order_by(Resident.unit_number.asc())
    if block_id:
        residents_q = residents_q.filter(Resident.block_id == block_id)
    if q:
        like = f"%{q.strip()}%"
        residents_q = residents_q.filter(
            (Resident.unit_number.ilike(like)) |
            (Resident.owner_full_name.ilike(like)) |
            (Resident.owner_phone.ilike(like)) |
            (Resident.owner_email.ilike(like))
        )
    residents = residents_q.all()

    now = datetime.utcnow()
    year = year or now.year
    month = month or now.month

    from_dt = datetime(year, month, 1)
    to_dt = datetime(year + (1 if month == 12 else 0), (1 if month == 12 else month + 1), 1)

    # Показания за период
    # ВАЖНО: Показываем показания для всех счётчиков (активных и неактивных),
    # так как показания уже были записаны и должны отображаться в истории
    readings_q = db.query(MeterReading).join(ResidentMeter).join(Resident)
    # УБРАЛИ фильтр is_active == True, чтобы показывать все показания за период
    if block_id:
        readings_q = readings_q.filter(Resident.block_id == block_id)
    if resident_id:
        readings_q = readings_q.filter(Resident.id == resident_id)
    allowed_types = {"ELECTRIC", "GAS", "WATER", "SEWERAGE", "SERVICE", "RENT", "CONSTRUCTION"}
    selected_types = [t for t in (meter_type or []) if t in allowed_types]
    selected_types_set = set(selected_types)
    filter_by_meter_type = bool(selected_types_set)
    include_water = (not filter_by_meter_type) or ("WATER" in selected_types_set)
    include_sewerage = (not filter_by_meter_type) or ("SEWERAGE" in selected_types_set)

    # Если выбрана только канализация, всё равно нужно подтянуть WATER как источник
    # для расчёта авто-канализации (у резидентов без реального SEWERAGE-счётчика).
    query_types = set(selected_types_set)
    if filter_by_meter_type and include_sewerage and not include_water:
        query_types.add("WATER")
    if query_types:
        readings_q = readings_q.filter(ResidentMeter.meter_type.in_([MeterType(t) for t in query_types]))
    if q:
        like = f"%{q.strip()}%"
        readings_q = readings_q.filter(
            (Resident.unit_number.ilike(like)) |
            (Resident.owner_full_name.ilike(like)) |
            (Resident.owner_phone.ilike(like)) |
            (Resident.owner_email.ilike(like))
        )

    readings_q = readings_q.filter(
        MeterReading.reading_date >= from_dt,
        MeterReading.reading_date < to_dt,
    )
    period_readings = readings_q.all()
    
    # Сначала определяем, есть ли реальная канализация за период у резидента.
    real_sewerage_resident_ids: set[int] = set()
    for rd in period_readings:
        meter = rd.resident_meter
        if meter and meter.meter_type == MeterType.SEWERAGE:
            real_sewerage_resident_ids.add(meter.resident_id)

    # Агрегат по счётчикам
    rows: dict[int, dict] = {}
    for rd in period_readings:
        meter = rd.resident_meter
        res_id = meter.resident_id

        # Единицы: ELECTRIC -> кВт·ч; GAS/WATER/SEWERAGE -> м³; SERVICE/RENT/CONSTRUCTION -> мес.
        if meter.meter_type == MeterType.ELECTRIC:
            unit = "кВт·ч"
        elif meter.meter_type in {MeterType.GAS, MeterType.WATER, MeterType.SEWERAGE}:
            unit = "м³"
        else:
            unit = "мес."

        entry = rows.setdefault(res_id, {"resident": meter.resident, "meters": {}, "max_reading_date": None})
        cons = Decimal(rd.consumption or 0)
        total = Decimal(rd.amount_total or 0)

        def ensure_mrow():
            return entry["meters"].setdefault(meter.id, {
                "meter": meter,
                "unit": unit,
                "consumption": Decimal("0"),
                "total": Decimal("0"),
                "reading_ids": [],
            })

        # Если у резидента нет реального SEWERAGE-счётчика — показываем авто-канализацию как отдельный тариф.
        if meter.meter_type == MeterType.WATER and res_id not in real_sewerage_resident_ids:
            t = rd.tariff
            percent = _effective_sewerage_percent(t)
            if percent > 0:
                k = percent / Decimal("100")
                if getattr(t, "customer_type", None) == CustomerType.INDIVIDUAL:
                    water_cons = cons * (Decimal("1") - k)
                    water_total = money(total * (Decimal("1") - k))
                    sewer_cons = cons * k
                    sewer_total = money(total - water_total)
                else:
                    water_cons = cons
                    water_total = total
                    sewer_cons = cons * k
                    sewer_total = money(total * k)

                if include_water:
                    mrow = ensure_mrow()
                    mrow["consumption"] += water_cons
                    mrow["total"] += water_total
                    mrow["reading_ids"].append(int(rd.id))

                if include_sewerage:
                    auto_sewer = entry.setdefault("auto_sewer", {
                        "consumption": Decimal("0"),
                        "total": Decimal("0"),
                        "unit": "м³",
                    })
                    auto_sewer["consumption"] += sewer_cons
                    auto_sewer["total"] += sewer_total
            else:
                if include_water:
                    mrow = ensure_mrow()
                    mrow["consumption"] += cons
                    mrow["total"] += total
                    mrow["reading_ids"].append(int(rd.id))
        else:
            meter_type_value = meter.meter_type.value
            if (not filter_by_meter_type) or (meter_type_value in selected_types_set):
                mrow = ensure_mrow()
                mrow["consumption"] += cons
                mrow["total"] += total
                mrow["reading_ids"].append(int(rd.id))

        # Нужна "последняя" запись за период для сортировки (новые сверху)
        cur_max = entry.get("max_reading_date")
        if cur_max is None or rd.reading_date > cur_max:
            entry["max_reading_date"] = rd.reading_date

    paid_resident_ids = set(
        rid for (rid,) in db.query(Resident.id)
        .join(Invoice, Invoice.resident_id == Resident.id)
        .filter(
            Invoice.period_year == year,
            Invoice.period_month == month,
            Invoice.status == InvoiceStatus.PAID
        ).all()
    )

    payment_lock_maps: dict[int, dict[int, dict]] = {}
    line_payment_states: dict[int, dict[int, dict]] = {}
    for res_id in rows.keys():
        payment_lock_maps[int(res_id)] = _meter_reading_payment_lock_map(
            db=db,
            resident_id=int(res_id),
            year=year,
            month=month,
        )
        line_payment_states[int(res_id)] = _invoice_line_payment_state_for_period(
            db=db,
            resident_id=int(res_id),
            year=year,
            month=month,
        )

    # Формируем ответ
    result_rows = []
    for res_id, data in rows.items():
        resident = data["resident"]
        meters_list = []
        total_amount = Decimal("0")
        reading_date = data.get("max_reading_date")  # last reading date for sorting/display
        has_editable_meters = False
        
        for meter_id, mdata in data["meters"].items():
            meter = mdata["meter"]
            consumption = float(mdata["consumption"])
            total = float(mdata["total"])
            total_amount += Decimal(total)
            
            # Определяем тип для отображения
            if meter.meter_type == MeterType.ELECTRIC:
                display_type = "Электричество"
            elif meter.meter_type == MeterType.GAS:
                display_type = "Газ"
            elif meter.meter_type == MeterType.WATER:
                display_type = "Вода"
            elif meter.meter_type == MeterType.SEWERAGE:
                display_type = "Канализация"
            elif meter.meter_type == MeterType.SERVICE:
                display_type = "Сервис"
            elif meter.meter_type == MeterType.RENT:
                display_type = "Аренда"
            elif meter.meter_type == MeterType.CONSTRUCTION:
                display_type = "Строительство"
            else:
                display_type = "Неизвестно"

            meter_lock_map = payment_lock_maps.get(int(res_id), {})
            reading_ids = [int(x) for x in (mdata.get("reading_ids") or []) if x is not None]

            line_total_amount = Decimal("0")
            paid_amount = Decimal("0")
            remaining_amount = Decimal("0")
            has_payment_meta = False

            for rid in reading_ids:
                meta = meter_lock_map.get(rid)
                if not meta:
                    continue
                has_payment_meta = True
                line_total_amount += Decimal(str(meta.get("line_total", 0) or 0))
                paid_amount += Decimal(str(meta.get("paid_amount", 0) or 0))
                remaining_amount += Decimal(str(meta.get("remaining_amount", 0) or 0))

            if not has_payment_meta:
                line_total_amount = Decimal(str(total))
                paid_amount = Decimal("0")
                remaining_amount = Decimal(str(total))

            line_total_amount = money(line_total_amount)
            paid_amount = money(paid_amount)
            remaining_amount = money(remaining_amount)

            meter_is_paid = line_total_amount > Decimal("0.0001") and remaining_amount <= Decimal("0.0001")
            meter_is_partial = paid_amount > Decimal("0.0001") and remaining_amount > Decimal("0.0001")
            payment_status = "Оплачено" if meter_is_paid else ("Частично" if meter_is_partial else "Не оплачено")
            meter_is_editable = paid_amount <= Decimal("0.0001")
            if meter_is_editable:
                has_editable_meters = True
            
            meters_list.append({
                "type": display_type,
                "consumption": consumption,
                "unit": mdata["unit"],
                "total": total,
                "is_paid": meter_is_paid,
                "is_partial": meter_is_partial,
                "is_editable": meter_is_editable,
                "payment_status": payment_status,
                "paid_amount": float(paid_amount),
                "remaining_amount": float(remaining_amount),
                "line_total_amount": float(line_total_amount),
            })

        auto_sewer = data.get("auto_sewer")
        if auto_sewer and auto_sewer["total"] > 0:
            auto_cons = float(auto_sewer["consumption"])
            auto_total = float(auto_sewer["total"])
            total_amount += Decimal(auto_total)

            resident_line_state = line_payment_states.get(int(res_id), {})
            sewer_line_states = [
                state
                for state in resident_line_state.values()
                if _is_sewer_line_desc((state.get("description") or ""))
            ]

            sewer_line_total = money(
                sum((Decimal(str(s.get("line_total", 0) or 0)) for s in sewer_line_states), Decimal("0"))
            )
            sewer_paid = money(
                sum((Decimal(str(s.get("paid_amount", 0) or 0)) for s in sewer_line_states), Decimal("0"))
            )
            sewer_remaining = money(
                sum((Decimal(str(s.get("remaining_amount", 0) or 0)) for s in sewer_line_states), Decimal("0"))
            )
            if not sewer_line_states:
                sewer_line_total = money(Decimal(str(auto_total)))
                sewer_paid = Decimal("0")
                sewer_remaining = sewer_line_total

            sewer_is_paid = sewer_line_total > Decimal("0.0001") and sewer_remaining <= Decimal("0.0001")
            sewer_is_partial = sewer_paid > Decimal("0.0001") and sewer_remaining > Decimal("0.0001")
            sewer_payment_status = "Оплачено" if sewer_is_paid else ("Частично" if sewer_is_partial else "Не оплачено")

            meters_list.append({
                "type": "Канализация",
                "consumption": auto_cons,
                "unit": auto_sewer["unit"],
                "total": auto_total,
                "is_paid": sewer_is_paid,
                "is_partial": sewer_is_partial,
                "is_editable": sewer_paid <= Decimal("0.0001"),
                "payment_status": sewer_payment_status,
                "paid_amount": float(sewer_paid),
                "remaining_amount": float(sewer_remaining),
                "line_total_amount": float(sewer_line_total),
            })

        if not meters_list:
            continue
        
        result_rows.append({
            "resident_id": res_id,
            "resident_code": f"{resident.block.name if resident.block else ''} / {resident.unit_number}",
            "resident_info": f"Блок {resident.block.name if resident.block else ''}, №{resident.unit_number}",
            "block_name": resident.block.name if resident.block else "",
            "unit_number": resident.unit_number,
            "meters": meters_list,
            "total_amount": float(total_amount),
            "is_paid": res_id in paid_resident_ids,
            "has_editable_meters": has_editable_meters,
            "reading_date": reading_date.strftime("%Y-%m-%d") if reading_date else None,
            "_reading_date_dt": reading_date,
        })

    # Сортировка: последние записи сверху.
    # В качестве основного ключа используем ID резидента (новые ID первыми),
    # а дату последнего показания оставляем вторичным критерием.
    result_rows.sort(
        key=lambda r: (
            int(r.get("resident_id") or 0),
            r.get("_reading_date_dt") or datetime.min,
        ),
        reverse=True,
    )
    for r in result_rows:
        r.pop("_reading_date_dt", None)

    # Apply pagination
    total = len(result_rows)
    last_page = max(1, (total + per_page - 1) // per_page)
    if page > last_page:
        page = last_page
    
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_rows = result_rows[start_idx:end_idx]

    return {
        "blocks": [{"id": b.id, "name": b.name} for b in blocks],
        "residents": [{"id": r.id, "unit_number": r.unit_number, "block_name": r.block.name if r.block else ""} for r in residents],
        "rows": paginated_rows,
        "year": year,
        "month": month,
        "total_amount": float(sum(r["total_amount"] for r in result_rows)),
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "last_page": last_page,
        },
    }


# ====== Get resident meters (reuse existing endpoint) ======
@router.get("/resident/{resident_id}/meters")
def get_resident_meters(
    resident_id: int,
    db: Session = Depends(get_db),
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD для режима редактирования"),
):
    """
    Данные для модалки: список счётчиков резидента.
    """
    cleanup_expired_meter_photos(db)
    r = db.get(Resident, resident_id)
    if not r:
        raise HTTPException(status_code=404, detail="Resident not found")

    period_start = period_end = None
    period_year = period_month = None
    if date:
        try:
            d = datetime.strptime(date, "%Y-%m-%d")
            period_year, period_month = d.year, d.month
            period_start = datetime(d.year, d.month, 1)
            period_end = datetime(d.year + (1 if d.month == 12 else 0), (1 if d.month == 12 else d.month + 1), 1)
        except Exception as e:
            period_start = period_end = None
            period_year = period_month = None

    # ВАЖНО: В режиме редактирования месяца нужно показывать:
    # - активные счётчики
    # - И неактивные тоже, если в выбранном месяце по ним есть показания
    # иначе при “удалении услуги у резидента” (deactivate meter) пропадёт возможность
    # открыть/увидеть исторический тариф и редактировать показания.
    meters_q = db.query(ResidentMeter).filter(ResidentMeter.resident_id == resident_id)
    if period_start and period_end:
        in_period = exists().where(
            MeterReading.resident_meter_id == ResidentMeter.id,
            MeterReading.reading_date >= period_start,
            MeterReading.reading_date < period_end,
        )
        meters_q = meters_q.filter(or_(ResidentMeter.is_active == True, in_period))  # noqa: E712
    else:
        meters_q = meters_q.filter(ResidentMeter.is_active == True)  # noqa: E712

    meters_list = meters_q.options(
        joinedload(ResidentMeter.tariff).joinedload(Tariff.steps)
    ).all()

    payment_lock_map = {}
    if period_year and period_month:
        payment_lock_map = _meter_reading_payment_lock_map(db, resident_id, period_year, period_month)
    
    meters = []
    for m in meters_list:
        # prev_value: ПОСЛЕДНЯЯ запись ДО начала месяца (или initial)
        if period_start:
            prev_rd = (
                db.query(MeterReading)
                .filter(MeterReading.resident_meter_id == m.id, MeterReading.reading_date < period_start)
                .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
                .first()
            )
            if prev_rd:
                prev_value = Decimal(prev_rd.value)
            else:
                prev_value = Decimal(m.initial_reading or 0)
        else:
            # без даты — просто последнее показание
            last_any = (
                db.query(MeterReading)
                .filter(MeterReading.resident_meter_id == m.id)
                .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
                .first()
            )
            if last_any:
                prev_value = Decimal(last_any.value)
            else:
                prev_value = Decimal(m.initial_reading or 0)

        existing = None
        if period_start and period_end:
            existing = (
                db.query(MeterReading)
                .filter(
                    MeterReading.resident_meter_id == m.id,
                    MeterReading.reading_date >= period_start,
                    MeterReading.reading_date < period_end,
                )
                .options(joinedload(MeterReading.tariff).joinedload(Tariff.steps))
                .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
                .first()
            )
        last_any = (
            db.query(MeterReading)
            .filter(MeterReading.resident_meter_id == m.id)
            .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
            .first()
        )

        # ВАЖНО:
        # Для режима редактирования месяца (когда existing=True) нужно показывать тариф,
        # по которому было рассчитано ИМЕННО ЭТО показание (existing.tariff),
        # а не текущий тариф счётчика (m.tariff), т.к. тариф могли "удалить" (архивировать)
        # или поменять у счётчика позже.
        tariff_obj = None
        if existing and getattr(existing, "tariff", None) is not None:
            tariff_obj = existing.tariff
        else:
            tariff_obj = m.tariff

        if m.meter_type == MeterType.ELECTRIC:
            display_type = "Электричество"; unit = "кВт·ч"; is_fixed = False
        elif m.meter_type == MeterType.GAS:
            display_type = "Газ"; unit = "м³"; is_fixed = False
        elif m.meter_type == MeterType.WATER:
            display_type = "Вода"; unit = "м³"; is_fixed = False
        elif m.meter_type == MeterType.SEWERAGE:
            display_type = "Канализация"; unit = "м³"; is_fixed = False
        elif m.meter_type == MeterType.SERVICE:
            display_type = "Сервис (фикс)"; unit = "мес."; is_fixed = True
        elif m.meter_type == MeterType.RENT:
            display_type = "Аренда (фикс)"; unit = "мес."; is_fixed = True
        elif m.meter_type == MeterType.CONSTRUCTION:
            display_type = "Строительство (фикс)"; unit = "мес."; is_fixed = True
        else:
            display_type = "Неизвестно"; unit = "—"; is_fixed = False

        # Для CONSTRUCTION тарифов получаем диапазон дат
        date_range = None
        if m.meter_type == MeterType.CONSTRUCTION and tariff_obj and tariff_obj.steps:
            first_step = tariff_obj.steps[0]
            if first_step.from_date and first_step.to_date:
                date_range = {
                    "from_date": first_step.from_date.isoformat(),
                    "to_date": first_step.to_date.isoformat(),
                }

        # Подготовка данных о тарифе для расчета суммы на frontend
        tariff_steps = []
        if tariff_obj and tariff_obj.steps:
            for step in sorted(tariff_obj.steps, key=lambda s: s.from_value or 0):
                tariff_steps.append({
                    "from_value": float(step.from_value) if step.from_value is not None else None,
                    "to_value": float(step.to_value) if step.to_value is not None else None,
                    "from_date": step.from_date.isoformat() if step.from_date else None,
                    "to_date": step.to_date.isoformat() if step.to_date else None,
                    "price": float(step.price),
                })
        
        prev_value_float = float(prev_value)
        existing_value_float = float(existing.value) if existing else None

        existing_photo_url = None
        if existing:
            photo = db.query(MeterReadingPhoto).filter(MeterReadingPhoto.meter_reading_id == existing.id).first()
            if photo:
                normalized_photo_path = str(photo.file_path).replace("\\", "/")
                existing_photo_url = f"/uploads/{normalized_photo_path}"

        payment_meta = None
        if existing:
            payment_meta = payment_lock_map.get(int(existing.id))

        paid_amount = Decimal(str(payment_meta.get("paid_amount", 0))) if payment_meta else Decimal("0")
        full_locked = bool(payment_meta and payment_meta.get("locked"))
        # По бизнес-правилу редактирование запрещено, если по строке уже есть любая оплата
        # (частичная или полная).
        payment_locked = full_locked or (paid_amount > Decimal("0.0001"))
        
        meters.append({
            "meter_id": m.id,
            "type": m.meter_type.value,
            "display_type": display_type,
            "serial": m.serial_number,
            "tariff_id": (existing.tariff_id if existing else m.tariff_id),
            "tariff_name": (tariff_obj.name if tariff_obj else None),
            "vat_percent": (tariff_obj.vat_percent if tariff_obj else 0),
            "tariff_steps": tariff_steps,
            "prev_value": prev_value_float,
            "unit": unit,
            "is_fixed": is_fixed,
            "date_range": date_range,
            "existing": bool(existing),
            "existing_value": existing_value_float,
            "existing_note": (existing.note if existing else None),
            "existing_reading_id": (existing.id if existing else None),
            "last_reading_id": (last_any.id if last_any else None),
            "existing_photo_url": existing_photo_url,
            "payment_locked": payment_locked,
            "paid_amount": float(payment_meta.get("paid_amount", 0)) if payment_meta else 0.0,
            "remaining_amount": float(payment_meta.get("remaining_amount", 0)) if payment_meta else 0.0,
            "line_total_amount": float(payment_meta.get("line_total", 0)) if payment_meta else 0.0,
            "sewerage_percent": float(_effective_sewerage_percent(tariff_obj)),
            "stable_tariff": float(Decimal(str(getattr(tariff_obj, "stable_tariff", 0) or 0))) if tariff_obj else 0.0,
            "use_multiplier": bool(getattr(tariff_obj, "use_multiplier", False)) if tariff_obj else False,
            "consumption_multiplier": float(Decimal(str(getattr(tariff_obj, "consumption_multiplier", 1) or 1))) if tariff_obj else 1.0,
            "tariff_customer_type": (tariff_obj.customer_type.value if getattr(tariff_obj, "customer_type", None) else None),
        })

    return {"meters": meters}


# ====== Public endpoints (must be before dynamic routes) ======
@router.post("/public")
def create_readings_public(
    data: ReadingCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    """Public endpoint for testing."""
    try:
        # Получаем пользователя из сессии
        from ..models import User
        from ..security import get_user_id_from_session
        
        user_id = get_user_id_from_session(request)
        user = None
        if user_id:
            user = db.get(User, user_id)
            if user and user.is_active:
                pass  # Используем пользователя из сессии
            else:
                user = None
        
        # Fallback: если нет сессии, используем первого пользователя для теста
        if not user:
            user = db.query(User).first()
            if not user:
                raise HTTPException(status_code=500, detail="No user found in database")
        
        return create_readings_internal(data, user, db)
    except Exception as e:
        import traceback
        print(f"Error in create_readings_public: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


# ====== Create/Update readings ======
@router.post("/")
def create_readings(
    data: ReadingCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Upsert показаний за месяц.
    """
    return create_readings_internal(data, user, db)


@router.post("/meter/{meter_id}/photo")
def upload_meter_photo(
    meter_id: int,
    date_str: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cleanup_expired_meter_photos(db)
    meter = db.get(ResidentMeter, meter_id)
    if not meter:
        raise HTTPException(status_code=404, detail="Meter not found")

    if not file or not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Invalid image file")

    try:
        reading_date = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format")

    period_start = datetime(reading_date.year, reading_date.month, 1)
    period_end = datetime(
        reading_date.year + (1 if reading_date.month == 12 else 0),
        (1 if reading_date.month == 12 else reading_date.month + 1),
        1,
    )
    if _is_period_paid(db, meter.resident_id, reading_date.year, reading_date.month):
        raise HTTPException(status_code=409, detail="Editing is disabled for paid periods")

    reading = (
        db.query(MeterReading)
        .filter(
            MeterReading.resident_meter_id == meter_id,
            MeterReading.reading_date >= period_start,
            MeterReading.reading_date < period_end,
        )
        .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
        .first()
    )
    if not reading:
        raise HTTPException(status_code=404, detail="Reading not found for this month")
    lock_map = _meter_reading_payment_lock_map(db, meter.resident_id, reading_date.year, reading_date.month)
    if lock_map.get(int(reading.id), {}).get("locked"):
        raise HTTPException(status_code=409, detail="This line is paid and cannot be edited")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}:
        ext = ".jpg"

    raw_bytes = file.file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty image file")

    processed_bytes, processed_ext = _compress_meter_photo_if_needed(raw_bytes, ext)
    filename = f"{meter_id}_{reading.id}_{uuid4().hex}{processed_ext}"
    relative_path = os.path.join("meter_readings", filename)
    os.makedirs(os.path.join("uploads", "meter_readings"), exist_ok=True)
    full_path = os.path.join("uploads", relative_path)

    with open(full_path, "wb") as buffer:
        buffer.write(processed_bytes)

    expires_at = datetime.utcnow() + timedelta(days=PHOTO_TTL_DAYS)
    existing = db.query(MeterReadingPhoto).filter(MeterReadingPhoto.meter_reading_id == reading.id).first()
    if existing:
        try:
            old_path = _photo_disk_path(existing)
            if os.path.exists(old_path):
                os.remove(old_path)
        except Exception:
            pass
        existing.file_path = relative_path.replace("\\", "/")
        existing.created_at = datetime.utcnow()
        existing.expires_at = expires_at
        existing.created_by_id = user.id
    else:
        db.add(MeterReadingPhoto(
            meter_reading_id=reading.id,
            file_path=relative_path.replace("\\", "/"),
            created_at=datetime.utcnow(),
            expires_at=expires_at,
            created_by_id=user.id,
        ))

    db.commit()
    normalized_relative_path = relative_path.replace("\\", "/")
    return {"ok": True, "photo_url": f"/uploads/{normalized_relative_path}"}


@router.delete("/meter/{meter_id}/photo")
def delete_meter_photo(
    meter_id: int,
    date_str: str = Query(..., description="YYYY-MM-DD"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cleanup_expired_meter_photos(db)
    meter = db.get(ResidentMeter, meter_id)
    if not meter:
        raise HTTPException(status_code=404, detail="Meter not found")

    try:
        reading_date = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format")

    period_start = datetime(reading_date.year, reading_date.month, 1)
    period_end = datetime(
        reading_date.year + (1 if reading_date.month == 12 else 0),
        (1 if reading_date.month == 12 else reading_date.month + 1),
        1,
    )
    if _is_period_paid(db, meter.resident_id, reading_date.year, reading_date.month):
        raise HTTPException(status_code=409, detail="Editing is disabled for paid periods")

    reading = (
        db.query(MeterReading)
        .filter(
            MeterReading.resident_meter_id == meter_id,
            MeterReading.reading_date >= period_start,
            MeterReading.reading_date < period_end,
        )
        .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
        .first()
    )
    if not reading:
        raise HTTPException(status_code=404, detail="Reading not found for this month")
    lock_map = _meter_reading_payment_lock_map(db, meter.resident_id, reading_date.year, reading_date.month)
    if lock_map.get(int(reading.id), {}).get("locked"):
        raise HTTPException(status_code=409, detail="This line is paid and cannot be edited")

    delete_meter_photo_for_reading(db, reading.id)
    db.commit()
    return {"ok": True}


def create_readings_internal(
    data: ReadingCreate,
    user: User,
    db: Session,
):
    """
    Internal function for creating/updating readings.
    """
    cleanup_expired_meter_photos(db)
    r = db.get(Resident, data.resident_id)
    if not r:
        raise HTTPException(status_code=404, detail="Resident not found")

    try:
        reading_date = datetime.strptime(data.date_str, "%Y-%m-%d") if data.date_str else datetime.utcnow()
    except Exception:
        reading_date = datetime.utcnow()

    period_year = reading_date.year
    period_month = reading_date.month
    period_start = datetime(period_year, period_month, 1)
    period_end = datetime(period_year + (1 if period_month == 12 else 0), (1 if period_month == 12 else period_month + 1), 1)
    if _is_period_paid(db, r.id, period_year, period_month):
        raise HTTPException(status_code=409, detail="Editing is disabled for paid periods")
    lock_map = _meter_reading_payment_lock_map(db, r.id, period_year, period_month)
    
    upserted: list[MeterReading] = []

    for it in data.items:
        meter_id = it.meter_id
        new_value_raw = it.new_value
        m = db.get(ResidentMeter, meter_id)
        if not m or m.resident_id != r.id:
            db.rollback()
            raise HTTPException(status_code=404, detail=f"Meter {meter_id} not found")

        # Для фикс-услуг (SERVICE, RENT, CONSTRUCTION) consumption всегда = 1
        is_fixed = m.meter_type in {MeterType.SERVICE, MeterType.RENT, MeterType.CONSTRUCTION}
        
        # Если new_value is None для фикс-услуги - удаляем существующую запись за месяц
        if is_fixed and new_value_raw is None:
            existing = (
                db.query(MeterReading)
                .filter(
                    MeterReading.resident_meter_id == m.id,
                    MeterReading.reading_date >= period_start,
                    MeterReading.reading_date < period_end,
                )
                .first()
            )
            if existing:
                if lock_map.get(int(existing.id), {}).get("locked"):
                    db.rollback()
                    raise HTTPException(status_code=409, detail="This line is paid and cannot be edited")
                # Удаляем строку инвойса
                db.query(InvoiceLine).filter(InvoiceLine.meter_reading_id == existing.id).delete()
                # Удаляем запись показания
                db.add(ReadingLog(
                    action="DELETE",
                    reading_id=existing.id,
                    resident_meter_id=m.id,
                    user_id=user.id,
                    details=f"deleted by unchecking fixed meter month={period_year}-{period_month:02d}",
                ))
                db.delete(existing)
            continue
        
        new_value = Decimal(str(new_value_raw))
        tariff = db.get(Tariff, m.tariff_id)
        if not tariff:
            db.rollback()
            raise HTTPException(status_code=404, detail=f"Tariff {m.tariff_id} not found")
        
        if is_fixed:
            prev_rd = (
                db.query(MeterReading)
                .filter(MeterReading.resident_meter_id == m.id, MeterReading.reading_date < period_start)
                .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
                .first()
            )
            prev_val = Decimal(prev_rd.value) if prev_rd else Decimal("0")
            consumption = Decimal("1")
        else:
            prev_rd = (
                db.query(MeterReading)
                .filter(MeterReading.resident_meter_id == m.id, MeterReading.reading_date < period_start)
                .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
                .first()
            )
            if prev_rd:
                prev_val = Decimal(prev_rd.value)
            else:
                prev_val = Decimal(m.initial_reading or 0)

            if new_value < prev_val:
                db.rollback()
                raise HTTPException(status_code=400, detail=f"New value {new_value} is less than previous {prev_val}")

            raw_consumption = new_value - prev_val
            consumption = _apply_consumption_multiplier(raw_consumption, tariff, m.meter_type)

        # Ищем существующую запись за месяц
        existing = (
            db.query(MeterReading)
            .filter(
                MeterReading.resident_meter_id == m.id,
                MeterReading.reading_date >= period_start,
                MeterReading.reading_date < period_end,
            )
            .first()
        )

        # Расчёт суммы по тарифу
        annual_prev = get_gas_annual_prev(db, m.id, period_start) if m.meter_type == MeterType.GAS else None
        amount_comp = compute_amount_components(consumption, tariff, annual_prev=annual_prev)
        amount_net = amount_comp["amount_net"]
        amount_vat = amount_comp["amount_vat"]
        amount_total = amount_comp["amount_total"]
        steps_detail = amount_comp["breakdown"]
        stable_fee_net = amount_comp["stable_net"]
        stable_fee_vat = amount_comp["stable_vat"]
        stable_fee_total = amount_comp["stable_total"]

        if existing:
            if lock_map.get(int(existing.id), {}).get("locked"):
                db.rollback()
                raise HTTPException(status_code=409, detail="This line is paid and cannot be edited")
            # Обновляем существующую запись
            existing.value = new_value
            existing.consumption = consumption
            existing.amount_net = amount_net
            existing.amount_vat = amount_vat
            existing.amount_total = amount_total
            existing.stable_fee_net = stable_fee_net
            existing.stable_fee_vat = stable_fee_vat
            existing.stable_fee_total = stable_fee_total
            existing.note = (data.note.strip() if data.note is not None else None)
            db.flush()
            
            # Создаём лог для обновления
            db.add(ReadingLog(
                action="UPDATE",
                reading_id=existing.id,
                resident_meter_id=m.id,
                user_id=user.id,
                details=f"edit month={period_year}-{period_month:02d} new={float(new_value)} prev={float(prev_val)} cons={float(consumption)}",
            ))
            upserted.append(existing)
        else:
            # Создаём новую запись
            new_reading = MeterReading(
                resident_meter_id=m.id,
                reading_date=reading_date,
                value=new_value,
                consumption=consumption,
                tariff_id=tariff.id,
                amount_net=amount_net,
                vat_percent=tariff.vat_percent,
                amount_vat=amount_vat,
                amount_total=amount_total,
                stable_fee_net=stable_fee_net,
                stable_fee_vat=stable_fee_vat,
                stable_fee_total=stable_fee_total,
                note=data.note,
                created_by_id=user.id,
            )
            db.add(new_reading)
            db.flush()
            
            # Создаём лог для создания
            db.add(ReadingLog(
                action="CREATE",
                reading_id=new_reading.id,
                resident_meter_id=m.id,
                user_id=user.id,
                details=f"create month={period_year}-{period_month:02d} new={float(new_value)} prev={float(prev_val)} cons={float(consumption)}",
            ))
            upserted.append(new_reading)

        db.flush()

        # Обновляем/создаём строку инвойса
        invoice = (
            db.query(Invoice)
            .filter(
                Invoice.resident_id == r.id,
                Invoice.period_year == period_year,
                Invoice.period_month == period_month,
            )
            .first()
        )

        if not invoice:
            invoice = Invoice(
                resident_id=r.id,
                period_year=period_year,
                period_month=period_month,
                status=InvoiceStatus.DRAFT,
            )
            db.add(invoice)
            db.flush()

    # ВАЖНО: После обработки всех показаний из запроса, найдем ВСЕ показания за период
    # и убедимся, что для каждого есть строка в счете
    if upserted:
        # Определяем период из первого показания
        first_reading = upserted[0]
        period_year = first_reading.reading_date.year
        period_month = first_reading.reading_date.month
        resident_id = first_reading.resident_meter.resident_id
        
        from_dt = datetime(period_year, period_month, 1)
        to_dt = datetime(period_year + (1 if period_month == 12 else 0), (1 if period_month == 12 else period_month + 1), 1)
        
        # Найдем все показания за период для этого резидента
        all_period_readings = (
            db.query(MeterReading)
            .join(ResidentMeter, ResidentMeter.id == MeterReading.resident_meter_id)
            .filter(
                ResidentMeter.resident_id == resident_id,
                MeterReading.reading_date >= from_dt,
                MeterReading.reading_date < to_dt,
            )
            .all()
        )
        
        # Найдем счет за этот период
        invoice = (
            db.query(Invoice)
            .filter(
                Invoice.resident_id == resident_id,
                Invoice.period_year == period_year,
                Invoice.period_month == period_month,
            )
            .first()
        )
        
        if invoice:
            # Получим все существующие строки счета
            existing_lines = {line.meter_reading_id: line for line in db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all() if line.meter_reading_id}
            
            # Создадим/обновим строки для всех показаний периода
            for rd in all_period_readings:
                if rd.id not in existing_lines:
                    # Создаем новую строку для показания, которого еще нет в счете
                    m = rd.resident_meter
                    meter_type_name = {
                        MeterType.ELECTRIC: "Электричество",
                        MeterType.GAS: "Газ",
                        MeterType.WATER: "Вода",
                        MeterType.SEWERAGE: "Канализация",
                        MeterType.SERVICE: "Сервис",
                        MeterType.RENT: "Аренда",
                        MeterType.CONSTRUCTION: "Строительство",
                    }.get(m.meter_type, "Услуга")
                    unit = ("кВт·ч" if m.meter_type == MeterType.ELECTRIC else
                            "м³" if m.meter_type in {MeterType.GAS, MeterType.WATER, MeterType.SEWERAGE} else "мес.")
                    description = f"{meter_type_name} {float(rd.consumption)} {unit}"
                    
                    db.add(InvoiceLine(
                        invoice_id=invoice.id,
                        meter_reading_id=rd.id,
                        description=description,
                        amount_net=rd.amount_net,
                        amount_vat=rd.amount_vat,
                        amount_total=rd.amount_total,
                    ))
                else:
                    # Обновляем существующую строку
                    line = existing_lines[rd.id]
                    m = rd.resident_meter
                    meter_type_name = {
                        MeterType.ELECTRIC: "Электричество",
                        MeterType.GAS: "Газ",
                        MeterType.WATER: "Вода",
                        MeterType.SEWERAGE: "Канализация",
                        MeterType.SERVICE: "Сервис",
                        MeterType.RENT: "Аренда",
                        MeterType.CONSTRUCTION: "Строительство",
                    }.get(m.meter_type, "Услуга")
                    unit = ("кВт·ч" if m.meter_type == MeterType.ELECTRIC else
                            "м³" if m.meter_type in {MeterType.GAS, MeterType.WATER, MeterType.SEWERAGE} else "мес.")
                    description = f"{meter_type_name} {float(rd.consumption)} {unit}"
                    line.description = description
                    line.amount_net = rd.amount_net
                    line.amount_vat = rd.amount_vat
                    line.amount_total = rd.amount_total

            # Автодобавление/обновление синтетической строки канализации (от воды).
            _upsert_auto_sewerage_line_for_invoice(db, invoice, all_period_readings)

            # Пересчитываем итоги счёта после синхронизации всех строк
            db.flush()  # Важно: сохраняем все изменения перед пересчетом
            totals = db.query(
                func.coalesce(func.sum(InvoiceLine.amount_net), 0).label("net"),
                func.coalesce(func.sum(InvoiceLine.amount_vat), 0).label("vat"),
                func.coalesce(func.sum(InvoiceLine.amount_total), 0).label("total"),
            ).filter(InvoiceLine.invoice_id == invoice.id).first()

            if totals:
                invoice.amount_net = Decimal(str(totals.net or 0))
                invoice.amount_vat = Decimal(str(totals.vat or 0))
                invoice.amount_total = Decimal(str(totals.total or 0))

    db.commit()

    # Автораспределение аванса
    for rd in upserted:
        meter = db.get(ResidentMeter, rd.resident_meter_id)
        if meter:
            auto_apply_advance(db, meter.resident_id)

    return {"success": True, "upserted_count": len(upserted)}


# ====== Public endpoints (temporary for testing) ======
@router.get("/public")
def list_readings_public(
    db: Session = Depends(get_db),
    block_id: Optional[int] = Query(None),
    resident_id: Optional[int] = Query(None),
    meter_type: Optional[List[str]] = Query(None),
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
):
    """Public endpoint for testing."""
    return list_readings(db, block_id, resident_id, meter_type, year, month, q, page, per_page)


@router.get("/resident/{resident_id}/meters/public")
def get_resident_meters_public(
    resident_id: int,
    db: Session = Depends(get_db),
    date: Optional[str] = Query(default=None),
):
    """Public endpoint for testing."""
    return get_resident_meters(resident_id, db, date)




# ====== Get detailed reading history for resident ======
@router.get("/resident/{resident_id}/history")
def get_reading_history(
    resident_id: int,
    db: Session = Depends(get_db),
    from_month: Optional[str] = Query(None, description="Начальный месяц в формате YYYY-MM"),
    to_month: Optional[str] = Query(None, description="Конечный месяц в формате YYYY-MM"),
):
    """
    Получить детальную историю показаний для резидента по всем счётчикам.
    Поддерживает фильтрацию по диапазону месяцев.
    """
    cleanup_expired_meter_photos(db)
    r = db.get(Resident, resident_id)
    if not r:
        raise HTTPException(status_code=404, detail="Resident not found")

    meters = r.meters
    result = []
    meter_entries = []
    has_real_sewerage = False
    
    # Парсим даты фильтрации по месяцам
    start_date = None
    end_date = None
    if from_month:
        try:
            year, month = map(int, from_month.split('-'))
            # Начало первого дня месяца
            start_date = datetime(year, month, 1)
        except (ValueError, AttributeError):
            pass
    
    if to_month:
        try:
            year, month = map(int, to_month.split('-'))
            # Конец последнего дня месяца (начало следующего месяца минус 1 секунда)
            if month == 12:
                end_date = datetime(year + 1, 1, 1) - timedelta(seconds=1)
            else:
                end_date = datetime(year, month + 1, 1) - timedelta(seconds=1)
        except (ValueError, AttributeError):
            pass
    
    for m in meters:
        query = (
            db.query(MeterReading)
            .filter(MeterReading.resident_meter_id == m.id)
        )
        
        # Применяем фильтр по датам, если указан
        if start_date:
            query = query.filter(MeterReading.reading_date >= start_date)
        if end_date:
            query = query.filter(MeterReading.reading_date <= end_date)
        
        readings = (
            query.order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
            .all()
        )
        if m.meter_type == MeterType.SEWERAGE and readings:
            has_real_sewerage = True

        photo_map = {}
        reading_ids = [rd.id for rd in readings]
        if reading_ids:
            photos = db.query(MeterReadingPhoto).filter(MeterReadingPhoto.meter_reading_id.in_(reading_ids)).all()
            photo_map = {
                p.meter_reading_id: "/uploads/" + str(p.file_path).replace("\\", "/")
                for p in photos
            }
        
        meter_entries.append((m, readings, photo_map))

    for m, readings, photo_map in meter_entries:
        # Определяем тип для отображения
        if m.meter_type == MeterType.ELECTRIC:
            display_type = "Электричество"
            unit = "кВт·ч"
        elif m.meter_type == MeterType.GAS:
            display_type = "Газ"
            unit = "м³"
        elif m.meter_type == MeterType.WATER:
            display_type = "Вода"
            unit = "м³"
        elif m.meter_type == MeterType.SEWERAGE:
            display_type = "Канализация"
            unit = "м³"
        elif m.meter_type == MeterType.SERVICE:
            display_type = "Сервис"
            unit = "мес."
        elif m.meter_type == MeterType.RENT:
            display_type = "Аренда"
            unit = "мес."
        elif m.meter_type == MeterType.CONSTRUCTION:
            display_type = "Строительство"
            unit = "мес."
        else:
            display_type = "Неизвестно"
            unit = "—"

        readings_data = []
        sewerage_readings = []

        for rd in readings:
            tariff = db.get(Tariff, rd.tariff_id) if rd.tariff_id else m.tariff
            base_amount_total = Decimal(str(rd.amount_total or 0))
            base_cons = Decimal(str(rd.consumption or 0))
            base_value = Decimal(str(rd.value or 0))

            # Стабильный тариф (фиксированная часть) — только из исторического snapshot чтения.
            # Не читаем текущий tariff.stable_tariff, чтобы старые начисления не "переезжали".
            try:
                stable_fee_total = Decimal(str(getattr(rd, "stable_fee_total", 0) or 0))
            except Exception:
                stable_fee_total = Decimal("0")
            stable_fee_total = money(stable_fee_total)

            base_amount_for_calc = base_amount_total
            if stable_fee_total > 0 and base_amount_for_calc >= stable_fee_total:
                base_amount_for_calc = money(base_amount_for_calc - stable_fee_total)

            # По умолчанию — как есть (без стабильного тарифа)
            amount_out = money(base_amount_for_calc)
            cons_out = base_cons

            if m.meter_type == MeterType.WATER and not has_real_sewerage:
                t = tariff
                percent = _effective_sewerage_percent(t)
                if t and t.meter_type == MeterType.WATER and percent > 0:
                    k = percent / Decimal("100")
                    if getattr(t, "customer_type", None) == CustomerType.INDIVIDUAL:
                        cons_out = base_cons * (Decimal("1") - k)
                        amount_out = money(base_amount_for_calc * (Decimal("1") - k))
                        sewer_cons = base_cons * k
                        sewer_amount = money(base_amount_for_calc - amount_out)
                    else:
                        cons_out = base_cons
                        amount_out = money(base_amount_for_calc)
                        sewer_cons = base_cons * k
                        sewer_amount = money(base_amount_for_calc * k)

                    sewerage_readings.append({
                        "date": rd.reading_date.strftime("%Y-%m-%d"),
                        "value": float(sewer_cons),
                        "consumption": float(sewer_cons),
                        "amount": float(sewer_amount),
                        "vat_percent": rd.vat_percent,
                        "comment": "Авто (от воды)",
                        "photo_url": None,
                    })

            readings_data.append({
                "date": rd.reading_date.strftime("%Y-%m-%d"),
                "value": float(base_value),
                "consumption": float(cons_out),
                "amount": float(amount_out),
                "vat_percent": rd.vat_percent,
                "comment": rd.note or "—",
                "photo_url": photo_map.get(rd.id),
            })

            if stable_fee_total > 0:
                readings_data.append({
                    "date": rd.reading_date.strftime("%Y-%m-%d"),
                    "value": 0.0,
                    "consumption": 0.0,
                    "amount": float(stable_fee_total),
                    "vat_percent": 0,
                    "comment": "Стабильный тариф",
                    "photo_url": None,
                })

        result.append({
            "meter_id": m.id,
            "type": display_type,
            "tariff_name": m.tariff.name if m.tariff else None,
            "serial_number": m.serial_number,
            "unit": unit,
            "readings": readings_data,
        })

        if m.meter_type == MeterType.WATER and sewerage_readings:
            result.append({
                "meter_id": None,
                "type": "Канализация",
                "tariff_name": (m.tariff.name if m.tariff else None),
                "serial_number": None,
                "unit": "м³",
                "readings": sewerage_readings,
            })
    
    return {"meters": result}


@router.get("/resident/{resident_id}/history/public")
def get_reading_history_public(
    resident_id: int,
    db: Session = Depends(get_db),
):
    """Public endpoint for testing."""
    return get_reading_history(resident_id, db)


# ====== Delete last reading (public endpoint must be before main) ======
@router.delete("/meter/{meter_id}/last/public")
def delete_last_reading_public(
    meter_id: int,
    request: Request,
    expected_reading_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Public endpoint for testing."""
    from ..models import User, ReadingLog, InvoiceLine, Invoice
    from ..security import get_user_id_from_session
    from sqlalchemy import func
    
    # Получаем пользователя из сессии
    user_id = get_user_id_from_session(request)
    user = None
    if user_id:
        user = db.get(User, user_id)
        if user and user.is_active:
            pass  # Используем пользователя из сессии
        else:
            user = None
    
    # Fallback: если нет сессии, используем первого пользователя для теста
    if not user:
        user = db.query(User).first()
        if not user:
            raise HTTPException(status_code=500, detail="No user found in database")
    
    m = db.get(ResidentMeter, meter_id)
    if not m:
        raise HTTPException(status_code=404, detail="Meter not found")

    last = (
        db.query(MeterReading)
        .filter(MeterReading.resident_meter_id == m.id)
        .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
        .first()
    )
    if not last:
        return {"ok": True, "message": "Nothing to delete"}
    if _is_period_paid(db, m.resident_id, last.reading_date.year, last.reading_date.month):
        raise HTTPException(status_code=409, detail="Editing is disabled for paid periods")
    lock_map = _meter_reading_payment_lock_map(db, m.resident_id, last.reading_date.year, last.reading_date.month)
    if lock_map.get(int(last.id), {}).get("locked"):
        raise HTTPException(status_code=409, detail="This line is paid and cannot be edited")
    if expected_reading_id is not None and last.id != expected_reading_id:
        raise HTTPException(
            status_code=409,
            detail="Only the originally selected latest reading can be deleted once",
        )

    db.query(InvoiceLine).filter(InvoiceLine.meter_reading_id == last.id).delete()

    db.add(ReadingLog(
        action="DELETE",
        reading_id=last.id,
        resident_meter_id=m.id,
        user_id=user.id,
        details="deleted"
    ))
    delete_meter_photo_for_reading(db, last.id)
    db.delete(last)

    inv = db.query(Invoice).filter(
        Invoice.resident_id == m.resident_id,
        Invoice.period_year == last.reading_date.year,
        Invoice.period_month == last.reading_date.month,
    ).first()
    
    if inv:
        db.flush()
        sums = db.query(
            func.coalesce(func.sum(InvoiceLine.amount_net), 0),
            func.coalesce(func.sum(InvoiceLine.amount_vat), 0),
            func.coalesce(func.sum(InvoiceLine.amount_total), 0),
        ).filter(InvoiceLine.invoice_id == inv.id).one()

        inv.total_amount_net = Decimal(sums[0])
        inv.total_amount_vat = Decimal(sums[1])
        inv.total_amount_total = Decimal(sums[2])
        
        if inv.total_amount_total == 0:
            line_count = db.query(func.count(InvoiceLine.id)).filter(InvoiceLine.invoice_id == inv.id).scalar()
            if line_count == 0:
                db.delete(inv)

    db.commit()
    return {"ok": True, "message": "Last reading deleted successfully"}


@router.delete("/meter/{meter_id}/last")
def delete_last_reading(
    meter_id: int,
    expected_reading_id: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Удаляет последнее показание по счётчику.
    """
    from ..models import ReadingLog, InvoiceLine, Invoice
    from sqlalchemy import func
    
    m = db.get(ResidentMeter, meter_id)
    if not m:
        raise HTTPException(status_code=404, detail="Meter not found")

    last = (
        db.query(MeterReading)
        .filter(MeterReading.resident_meter_id == m.id)
        .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
        .first()
    )
    if not last:
        return {"ok": True, "message": "Nothing to delete"}
    if _is_period_paid(db, m.resident_id, last.reading_date.year, last.reading_date.month):
        raise HTTPException(status_code=409, detail="Editing is disabled for paid periods")
    lock_map = _meter_reading_payment_lock_map(db, m.resident_id, last.reading_date.year, last.reading_date.month)
    if lock_map.get(int(last.id), {}).get("locked"):
        raise HTTPException(status_code=409, detail="This line is paid and cannot be edited")
    if expected_reading_id is not None and last.id != expected_reading_id:
        raise HTTPException(
            status_code=409,
            detail="Only the originally selected latest reading can be deleted once",
        )

    # Удаляем строку инвойса
    db.query(InvoiceLine).filter(InvoiceLine.meter_reading_id == last.id).delete()

    # Логируем удаление
    db.add(ReadingLog(
        action="DELETE",
        reading_id=last.id,
        resident_meter_id=m.id,
        user_id=user.id,
        details="deleted"
    ))
    delete_meter_photo_for_reading(db, last.id)
    db.delete(last)

    # Пересчитываем/удаляем инвойс периода
    inv = db.query(Invoice).filter(
        Invoice.resident_id == m.resident_id,
        Invoice.period_year == last.reading_date.year,
        Invoice.period_month == last.reading_date.month,
    ).first()
    
    if inv:
        db.flush()
        sums = db.query(
            func.coalesce(func.sum(InvoiceLine.amount_net), 0),
            func.coalesce(func.sum(InvoiceLine.amount_vat), 0),
            func.coalesce(func.sum(InvoiceLine.amount_total), 0),
        ).filter(InvoiceLine.invoice_id == inv.id).one()

        inv.total_amount_net = Decimal(sums[0])
        inv.total_amount_vat = Decimal(sums[1])
        inv.total_amount_total = Decimal(sums[2])
        
        if inv.total_amount_total == 0:
            # Проверяем, есть ли еще строки
            line_count = db.query(func.count(InvoiceLine.id)).filter(InvoiceLine.invoice_id == inv.id).scalar()
            if line_count == 0:
                db.delete(inv)

    db.commit()
    return {"ok": True, "message": "Last reading deleted successfully"}

