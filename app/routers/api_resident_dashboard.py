"""
API endpoint for resident dashboard data
Returns JSON data for the resident's personal dashboard
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Form
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, or_, and_
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import List, Optional
from pydantic import BaseModel

from ..database import get_db
from ..models import (
    User, RoleEnum, Resident, ResidentMeter,
    Invoice, InvoiceStatus, InvoiceLine,
    Payment, PaymentApplication, PaymentApplicationLine, PaymentMethod,
    Notification, NotificationStatus, MeterReading,
    user_residents, PaymentLog,
    Tariff, MeterType, CustomerType
    , OnlineTransaction
)
from ..security import get_user_id_from_session
from ..utils import now_baku, to_baku_datetime


router = APIRouter(prefix="/api/resident", tags=["resident-api"])


def _money2(x: Decimal) -> Decimal:
    return (x or Decimal("0")).quantize(Decimal("0.01"))


def _effective_sewerage_percent(tariff: Tariff | None) -> Decimal:
    if not tariff or tariff.meter_type != MeterType.WATER:
        return Decimal("0")
    try:
        percent = Decimal(str(getattr(tariff, "sewerage_percent", 0) or 0))
    except Exception:
        percent = Decimal("0")
    return percent if percent > 0 else Decimal("0")


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


def _is_water_line_description(desc: str | None) -> bool:
    low = (desc or "").lower()
    return ("вода" in low) or ("water" in low) or ("meter_cold_water" in low)


def _is_sewerage_line_description(desc: str | None) -> bool:
    low = (desc or "").lower()
    return ("канализац" in low) or ("sewerage" in low) or ("meter_sewerage" in low)


def _normalize_selected_line_ids_for_water_sewer(
    selected_ids: list[int],
    line_desc_by_id: dict[int, str],
) -> list[int]:
    ids = [int(x) for x in (selected_ids or []) if int(x) > 0]
    if not ids:
        return []

    unique_ids = list(dict.fromkeys(ids))
    selected_has_bundle = any(
        _is_water_line_description(line_desc_by_id.get(lid))
        or _is_sewerage_line_description(line_desc_by_id.get(lid))
        for lid in unique_ids
    )
    if not selected_has_bundle:
        return unique_ids

    sewer_ids = [
        lid for lid, desc in line_desc_by_id.items()
        if _is_sewerage_line_description(desc)
    ]
    water_ids = [
        lid for lid, desc in line_desc_by_id.items()
        if _is_water_line_description(desc)
    ]

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


def _build_invoice_line_payment_map(
    lines: list[InvoiceLine],
    apps: list[PaymentApplication],
) -> dict[int, dict]:
    sorted_lines = sorted(lines, key=lambda l: l.id or 0)
    line_desc_by_id = {
        int(l.id): (l.description or "")
        for l in sorted_lines
        if l.id is not None
    }
    line_totals = {int(l.id): Decimal(str(l.amount_total or 0)) for l in sorted_lines if l.id is not None}
    paid_by_line = {lid: Decimal("0") for lid in line_totals}

    def allocate(amount: Decimal, line_ids: list[int]) -> Decimal:
        remaining_amt = Decimal(str(amount or 0))
        for lid in line_ids:
            if remaining_amt <= 0:
                break
            if lid not in line_totals:
                continue
            capacity = max(line_totals[lid] - paid_by_line[lid], Decimal("0"))
            if capacity <= 0:
                continue
            take = min(capacity, remaining_amt)
            paid_by_line[lid] += take
            remaining_amt -= take
        return remaining_amt

    ordered_apps = sorted(
        apps or [],
        key=lambda a: (
            a.created_at or datetime.min,
            a.id or 0,
        ),
    )
    default_order = list(line_totals.keys())
    for app in ordered_apps:
        app_amt = Decimal(str(getattr(app, "amount_applied", 0) or 0))
        if app_amt <= 0:
            continue
        selected_ids = _parse_selected_line_ids(getattr(app, "reference", None))
        if selected_ids:
            normalized_selected_ids = _normalize_selected_line_ids_for_water_sewer(selected_ids, line_desc_by_id)
            allocate(app_amt, normalized_selected_ids)
        else:
            allocate(app_amt, default_order)

    result: dict[int, dict] = {}
    for lid, total in line_totals.items():
        paid = _money2(paid_by_line.get(lid, Decimal("0")))
        remaining = _money2(max(total - paid, Decimal("0")))
        if remaining <= Decimal("0.0001"):
            status_text = "Оплачена"
        elif paid > Decimal("0.0001"):
            status_text = "Частично"
        else:
            status_text = "Не оплачена"
        result[lid] = {"paid": paid, "remaining": remaining, "status": status_text}
    return result


def _ensure_auto_sewerage_line(db: Session, inv: Invoice) -> None:
    if not inv or not inv.id:
        return

    rows = (
        db.query(InvoiceLine, MeterReading, ResidentMeter.meter_type)
        .join(InvoiceLine, InvoiceLine.meter_reading_id == MeterReading.id)
        .join(ResidentMeter, ResidentMeter.id == MeterReading.resident_meter_id)
        .filter(InvoiceLine.invoice_id == inv.id)
        .all()
    )

    has_real_sewerage = any(mt == MeterType.SEWERAGE for _ln, _rd, mt in rows)
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
        for ln, rd, mt in rows:
            if mt == MeterType.WATER:
                ln.amount_net = _money2(Decimal(str(rd.amount_net or 0)))
                ln.amount_vat = _money2(Decimal(str(rd.amount_vat or 0)))
                ln.amount_total = _money2(Decimal(str(rd.amount_total or 0)))
                ln.description = f"Вода {float(Decimal(str(rd.consumption or 0)))} м³"
        if auto_line:
            db.delete(auto_line)
        return

    water_rows = [(ln, rd) for ln, rd, mt in rows if mt == MeterType.WATER]
    if not water_rows:
        if auto_line:
            db.delete(auto_line)
        return

    sew_net = Decimal("0")
    sew_vat = Decimal("0")
    sew_total = Decimal("0")
    water_cons = Decimal("0")
    sewer_cons = Decimal("0")

    for ln, rd in water_rows:
        water_cons += Decimal(str(rd.consumption or 0))

        base_net = _money2(Decimal(str(rd.amount_net or 0)))
        base_vat = _money2(Decimal(str(rd.amount_vat or 0)))
        base_total = _money2(Decimal(str(rd.amount_total or 0)))

        t = db.get(Tariff, rd.tariff_id) if rd.tariff_id else None
        percent = _effective_sewerage_percent(t)
        if percent <= 0:
            ln.amount_net = base_net
            ln.amount_vat = base_vat
            ln.amount_total = base_total
            ln.description = f"Вода {float(Decimal(str(rd.consumption or 0)))} м³"
            continue

        k = percent / Decimal("100")

        consumption = Decimal(str(rd.consumption or 0))
        if getattr(t, "customer_type", None) == CustomerType.INDIVIDUAL:
            new_net = _money2(base_net * (Decimal("1") - k))
            new_vat = _money2(base_vat * (Decimal("1") - k))
            new_total = _money2(base_total * (Decimal("1") - k))

            ln.amount_net = new_net
            ln.amount_vat = new_vat
            ln.amount_total = new_total

            sew_net += (base_net - new_net)
            sew_vat += (base_vat - new_vat)
            sew_total += (base_total - new_total)
            water_display_cons = consumption * (Decimal("1") - k)
            sewer_display_cons = consumption * k
            ln.description = f"Вода {float(water_display_cons)} м³"
            sewer_cons += sewer_display_cons
        else:
            ln.amount_net = base_net
            ln.amount_vat = base_vat
            ln.amount_total = base_total

            sew_net += _money2(base_net * k)
            sew_vat += _money2(base_vat * k)
            sew_total += _money2(base_total * k)
            ln.description = f"Вода {float(consumption)} м³"
            sewer_cons += consumption * k

    if sew_total <= 0:
        if auto_line:
            db.delete(auto_line)
        return

    # Для инвойса канализация выводится только как начисление (без объёма).
    desc = "Канализация"

    if auto_line:
        auto_line.description = desc
        auto_line.amount_net = _money2(sew_net)
        auto_line.amount_vat = _money2(sew_vat)
        auto_line.amount_total = _money2(sew_total)
    else:
        db.add(InvoiceLine(
            invoice_id=inv.id,
            meter_reading_id=None,
            description=desc,
            amount_net=_money2(sew_net),
            amount_vat=_money2(sew_vat),
            amount_total=_money2(sew_total),
        ))


@router.post("/apply-advance")
def api_resident_apply_advance(
    request: Request,
    db: Session = Depends(get_db),
    resident_id: int = Form(...),
):
    """
    API версия применения аванса. 
    Берет аванс из общего пула и применяет к конкретному дому.
    """
    from .api_payment_logic import auto_apply_advance
    user = _get_resident_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Проверка прав (дом должен принадлежать пользователю)
    resident_ids = [r.id for r in (user.resident_links or [])]
    if resident_id not in resident_ids:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        # БЛОКИРОВКА: предотвращаем одновременные списания
        # Используем простой select без JOIN-ов для блокировки
        from sqlalchemy import select
        db.execute(select(Resident.id).where(Resident.id.in_(resident_ids)).with_for_update()).all()
        
        # 1) Применяем аванс к счетам (функция вернет количество и сумму)
        affected, applied_amount = auto_apply_advance(db, resident_id)
        
        if affected == 0 or applied_amount <= 0:
            return {
                "ok": True,
                "affected_count": 0,
                "message": "Нет открытых счетов для применения аванса"
            }
        
        # 2) Создаем техническую запись Payment для отображения в админ-панели
        history_payment = Payment(
            resident_id=resident_id,
            received_at=now_baku(),
            amount_total=applied_amount,
            method=PaymentMethod.ADVANCE,
            reference="AUTO_APPLY",
            comment=f"Автоматическое применение аванса к {affected} счетам ({applied_amount} ₼)",
            created_by_id=user.id,
        )
        db.add(history_payment)
        db.flush()
        
        # 3) ЛОГИРОВАНИЕ
        db.add(PaymentLog(
            payment_id=history_payment.id,
            resident_id=resident_id,
            user_id=user.id,
            action="APPLY_AUTO",
            amount=float(applied_amount),
            details=f"Автоматическое применение аванса к {affected} счетам"
        ))
            
        db.commit()
        return {
            "ok": True, 
            "affected_count": affected,
            "applied_amount": float(applied_amount),
            "message": f"Аванс успешно применен к {affected} счет(ам) на сумму {applied_amount} ₼"
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/test")
def test_endpoint():
    """Test endpoint to verify router is working."""
    return {"message": "API resident router is working!", "status": "ok"}


# Pydantic models for invoices
class InvoiceListItem(BaseModel):
    id: int
    resident_id: int
    resident_code: str  # "E / 444"
    number: Optional[str] = None
    status: str
    due_date: Optional[date] = None
    period_year: int
    period_month: int
    amount_total: float
    paid_amount: float
    remaining_amount: float
    period_dates: Optional[dict] = None  # {"from": "05.12.2025", "to": "21.04.2026"}


class InvoiceListResponse(BaseModel):
    invoices: List[InvoiceListItem]


@router.get("/invoices", response_model=InvoiceListResponse)
def get_resident_invoices(
    request: Request,
    db: Session = Depends(get_db),
    status: Optional[str] = None,
    resident_id: Optional[int] = None,
):
    """
    Get list of invoices for the current resident user.
    Supports filtering by status and resident_id.
    """
    user = _get_resident_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Load user with residents relationship
    user_with_residents = (
        db.query(User)
        .options(joinedload(User.resident_links).joinedload(Resident.block))
        .filter(User.id == user.id)
        .first()
    )
    
    if not user_with_residents:
        raise HTTPException(status_code=401, detail="Unauthorized")

    resident_ids = [r.id for r in (user_with_residents.resident_links or [])]
    if not resident_ids:
        return {"invoices": []}

    # Filter by resident_id if specified and belongs to user
    if resident_id:
        if resident_id not in resident_ids:
            raise HTTPException(status_code=403, detail="Forbidden")
        resident_ids = [resident_id]

    # Query invoices
    query = db.query(Invoice).filter(Invoice.resident_id.in_(resident_ids))
    
    if status and status in {s.value for s in InvoiceStatus}:
        query = query.filter(Invoice.status == InvoiceStatus(status))
    
    items = query.order_by(
        Invoice.period_year.desc(),
        Invoice.period_month.desc(),
        Invoice.id.desc()
    ).all()

    # Get paid amounts
    paid_map = {}
    if items:
        ids = [inv.id for inv in items]
        rows = (
            db.query(
                PaymentApplication.invoice_id,
                func.coalesce(func.sum(PaymentApplication.amount_applied), 0)
            )
            .filter(PaymentApplication.invoice_id.in_(ids))
            .group_by(PaymentApplication.invoice_id)
            .all()
        )
        paid_map = {inv_id: float(paid) for inv_id, paid in rows}

    # Get period dates from meter readings (similar to resident_portal.py)
    period_map: dict[int, dict[str, Optional[str]]] = {}
    if items:
        ids = [inv.id for inv in items]
        from ..models import MeterReading
        
        mr_with_prev = (
            db.query(
                MeterReading.id.label("mr_id"),
                MeterReading.resident_meter_id.label("meter_id"),
                MeterReading.reading_date.label("curr_dt"),
                func.lag(MeterReading.reading_date)
                    .over(
                        partition_by=MeterReading.resident_meter_id,
                        order_by=MeterReading.reading_date
                    )
                    .label("prev_dt"),
            )
            .subquery("mr_with_prev")
        )

        rows = (
            db.query(
                Invoice.id.label("inv_id"),
                func.min(func.coalesce(mr_with_prev.c.prev_dt, mr_with_prev.c.curr_dt)).label("min_prev"),
                func.max(mr_with_prev.c.curr_dt).label("max_curr"),
            )
            .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
            .join(mr_with_prev, mr_with_prev.c.mr_id == InvoiceLine.meter_reading_id)
            .filter(Invoice.id.in_(ids))
            .group_by(Invoice.id)
            .all()
        )

        for r in rows:
            end_dt = r.max_curr
            if not end_dt:
                continue
            end_str = end_dt.date().strftime('%d.%m.%Y')
            start_dt = r.min_prev
            start_str = start_dt.date().strftime('%d.%m.%Y') if start_dt else None
            period_map[r.inv_id] = {
                "from": start_str,
                "to": end_str,
            }

    # Chain period ranges per resident so that each next invoice
    # starts from the end date of the previous invoice for this resident.
    chained_period_map: dict[int, dict[str, Optional[str]]] = {}
    if items and period_map:
        from datetime import datetime as _dt

        # Group invoices by resident
        invoices_by_resident: dict[int, list[Invoice]] = {}
        for inv in items:
            if inv.resident_id is None:
                continue
            invoices_by_resident.setdefault(int(inv.resident_id), []).append(inv)

        def _parse_date(date_str: Optional[str]):
            if not date_str:
                return None
            try:
                return _dt.strptime(date_str, "%d.%m.%Y").date()
            except Exception:
                return None

        for resident_id, inv_list in invoices_by_resident.items():
            # Oldest first for chaining, по году/месяцу и id
            sorted_invs = sorted(
                inv_list,
                key=lambda inv: (inv.period_year, inv.period_month, inv.id),
            )
            prev_end_date = None
            for inv in sorted_invs:
                raw = period_map.get(inv.id)
                if not raw:
                    continue
                raw_start = _parse_date(raw.get("from"))
                raw_end = _parse_date(raw.get("to"))
                if not raw_end:
                    continue

                if prev_end_date:
                    start_date = prev_end_date
                else:
                    start_date = raw_start or raw_end

                prev_end_date = raw_end
                chained_period_map[inv.id] = {
                    "from": start_date.strftime("%d.%m.%Y") if start_date else None,
                    "to": raw_end.strftime("%d.%m.%Y"),
                }

    # Format response
    result = []
    for inv in items:
        resident = inv.resident
        block = resident.block if resident else None
        resident_code = f"{block.name if block else ''} / {resident.unit_number}" if block else resident.unit_number
        
        paid = paid_map.get(inv.id, 0.0)
        remaining = float(inv.amount_total or 0) - paid
        
        result.append({
            "id": inv.id,
            "resident_id": inv.resident_id,
            "resident_code": resident_code,
            "number": inv.number,
            "status": inv.status.value,
            "due_date": inv.due_date,
            "period_year": inv.period_year,
            "period_month": inv.period_month,
            "amount_total": float(inv.amount_total or 0),
            "paid_amount": paid,
            "remaining_amount": remaining,
            "period_dates": chained_period_map.get(inv.id) or period_map.get(inv.id),
        })

    return {"invoices": result}


@router.get("/invoice/{invoice_id}")
def get_resident_invoice_detail(
    invoice_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Get detailed information about a specific invoice for the current resident user.
    """
    user = _get_resident_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Load user with residents relationship
    user_with_residents = (
        db.query(User)
        .options(joinedload(User.resident_links))
        .filter(User.id == user.id)
        .first()
    )
    
    if not user_with_residents:
        raise HTTPException(status_code=401, detail="Unauthorized")

    resident_ids = [r.id for r in (user_with_residents.resident_links or [])]
    if not resident_ids:
        raise HTTPException(status_code=403, detail="Forbidden")

    # Get invoice
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    
    # Check if invoice belongs to user's residents
    if inv.resident_id not in resident_ids:
        raise HTTPException(status_code=403, detail="Forbidden")

    resident = inv.resident
    block = resident.block if resident else None
    
    # Get invoice lines
    _ensure_auto_sewerage_line(db, inv)
    db.flush()
    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == inv.id).all()
    
    # Get payments for this invoice
    apps = (
        db.query(PaymentApplication)
        .join(Payment, Payment.id == PaymentApplication.payment_id)
        .filter(PaymentApplication.invoice_id == inv.id)
        .order_by(PaymentApplication.created_at.asc(), PaymentApplication.id.asc())
        .all()
    )
    
    # Recalculate totals from lines
    lines_sum = db.query(
        func.coalesce(func.sum(InvoiceLine.amount_total), 0)
    ).filter(InvoiceLine.invoice_id == inv.id).scalar() or 0
    
    if abs(float(inv.amount_total or 0) - float(lines_sum)) > 0.01:
        inv.amount_total = Decimal(str(lines_sum))
        net_sum = db.query(func.coalesce(func.sum(InvoiceLine.amount_net), 0)).filter(InvoiceLine.invoice_id == inv.id).scalar() or 0
        vat_sum = db.query(func.coalesce(func.sum(InvoiceLine.amount_vat), 0)).filter(InvoiceLine.invoice_id == inv.id).scalar() or 0
        inv.amount_net = Decimal(str(net_sum))
        inv.amount_vat = Decimal(str(vat_sum))
        db.commit()
    
    paid_total = db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))\
                   .filter(PaymentApplication.invoice_id == inv.id).scalar() or 0
    remaining = float(inv.amount_total or 0) - float(paid_total)
    
    payments = []
    for app in apps:
        p = app.payment
        # ВАЖНО: Если app.reference указывает на списание аванса, показываем "ADVANCE"
        # Показываем "ADVANCE" вместо реального метода платежа (CARD/TRANSFER)
        if app.reference == "ADVANCE" or (app.reference and app.reference.startswith(("ADVANCE:", "AUTOADV:"))):
            display_method = "ADVANCE"
            display_comment = "Royal Park Pass"
        else:
            display_method = p.method.value
            display_comment = p.comment or "—"

        payments.append({
            "id": app.id,
            "date": to_baku_datetime(app.created_at or p.created_at or p.received_at),
            "method": display_method,
            "amount": float(app.amount_applied),
            "payment_id": p.id,
            "reference": p.reference,
            "comment": display_comment,
        })
    
    # Format period dates from meter readings if available
    period_dates = None
    if lines:
        from ..models import MeterReading
        
        mr_with_prev = (
            db.query(
                MeterReading.id.label("mr_id"),
                MeterReading.reading_date.label("curr_dt"),
                func.lag(MeterReading.reading_date)
                    .over(
                        partition_by=MeterReading.resident_meter_id,
                        order_by=MeterReading.reading_date
                    )
                    .label("prev_dt"),
            )
            .subquery("mr_with_prev")
        )

        row = (
            db.query(
                func.min(func.coalesce(mr_with_prev.c.prev_dt, mr_with_prev.c.curr_dt)).label("min_prev"),
                func.max(mr_with_prev.c.curr_dt).label("max_curr"),
            )
            .join(InvoiceLine, InvoiceLine.meter_reading_id == mr_with_prev.c.mr_id)
            .filter(InvoiceLine.invoice_id == inv.id)
            .first()
        )
        
        if row and row.max_curr:
            end_str = row.max_curr.date().strftime('%d.%m.%Y')
            start_str = row.min_prev.date().strftime('%d.%m.%Y') if row.min_prev else None
            period_dates = {
                "from": start_str,
                "to": end_str,
            }

    # Align invoice detail period with bills list chaining:
    # start = end of previous invoice for same resident (if exists), else raw start.
    if period_dates and inv.resident_id:
        prev_inv = (
            db.query(Invoice)
            .filter(
                Invoice.resident_id == inv.resident_id,
                or_(
                    Invoice.period_year < inv.period_year,
                    and_(
                        Invoice.period_year == inv.period_year,
                        or_(
                            Invoice.period_month < inv.period_month,
                            and_(Invoice.period_month == inv.period_month, Invoice.id < inv.id),
                        ),
                    ),
                ),
            )
            .order_by(Invoice.period_year.desc(), Invoice.period_month.desc(), Invoice.id.desc())
            .first()
        )
        if prev_inv:
            prev_end_dt = (
                db.query(func.max(MeterReading.reading_date))
                .join(InvoiceLine, InvoiceLine.meter_reading_id == MeterReading.id)
                .filter(InvoiceLine.invoice_id == prev_inv.id)
                .scalar()
            )
            if prev_end_dt:
                period_dates["from"] = prev_end_dt.date().strftime('%d.%m.%Y')
    
    resident_code = f"{block.name if block else ''} / {resident.unit_number}" if block else resident.unit_number
    
    # Build response lines with stable tariff split (for transparency)
    reading_ids = [line.meter_reading_id for line in lines if line.meter_reading_id]
    reading_map = {}
    if reading_ids:
        readings = (
            db.query(MeterReading)
            .options(joinedload(MeterReading.tariff))
            .filter(MeterReading.id.in_(reading_ids))
            .all()
        )
        reading_map = {rd.id: rd for rd in readings}

    def _stable_service_label(desc: str | None) -> str | None:
        if not desc:
            return None
        low = desc.lower()
        if "электр" in low:
            return "Электричество"
        if "газ" in low:
            return "Газ"
        if "вода" in low:
            return "Вода"
        if "канализац" in low:
            return "Канализация"
        if "сервис" in low:
            return "Сервис"
        if "аренда" in low:
            return "Аренда"
        if "строител" in low:
            return "Строительство"
        return None

    # Распределяем оплату по строкам с учётом явного выбора строк (LINESEL)
    line_payment_map = _build_invoice_line_payment_map(lines, apps)

    lines_out = []
    for line in lines:
        base_line = {
            "id": line.id,
            "description": line.description,
            "amount_net": float(line.amount_net),
            "amount_vat": float(line.amount_vat),
            "amount_total": float(line.amount_total),
        }

        rd = reading_map.get(line.meter_reading_id)
        try:
            stable_fee_total = Decimal(str(getattr(rd, "stable_fee_total", 0) or 0))
        except Exception:
            stable_fee_total = Decimal("0")

        if stable_fee_total > 0 and line.amount_total:
            # stable часть берём из snapshot чтения (историческое значение на момент начисления).
            line_net = Decimal(str(line.amount_net or 0))
            line_vat_total = Decimal(str(line.amount_vat or 0))
            line_total = _money2(Decimal(str(line.amount_total or 0)))
            stable_fee_total = min(_money2(stable_fee_total), line_total)
            try:
                stable_fee_net = _money2(Decimal(str(getattr(rd, "stable_fee_net", 0) or 0)))
                stable_fee_vat = _money2(Decimal(str(getattr(rd, "stable_fee_vat", 0) or 0)))
            except Exception:
                stable_fee_net = stable_fee_total
                stable_fee_vat = Decimal("0")
            if stable_fee_net <= 0 and stable_fee_total > 0:
                stable_fee_net = stable_fee_total
                stable_fee_vat = Decimal("0")

            variable_total = _money2(max(line_total - stable_fee_total, Decimal("0")))
            variable_vat = min(_money2(max(line_vat_total - stable_fee_vat, Decimal("0"))), variable_total)
            variable_net = _money2(max(variable_total - variable_vat, Decimal("0")))

            # Если линия действительно включает стабильный тариф — разделяем
            if line_net >= stable_fee_net:
                line_payment = line_payment_map.get(
                    int(line.id),
                    {"paid": Decimal("0"), "remaining": Decimal("0"), "status": "Не оплачена"},
                )

                split_total = variable_total + stable_fee_total
                if split_total > 0 and line_payment["paid"] > 0:
                    variable_paid = _money2(line_payment["paid"] * variable_total / split_total)
                    stable_paid = _money2(line_payment["paid"] * stable_fee_total / split_total)
                else:
                    variable_paid = Decimal("0")
                    stable_paid = Decimal("0")

                variable_remaining = max(variable_total - variable_paid, Decimal("0"))
                stable_remaining = max(stable_fee_total - stable_paid, Decimal("0"))

                variable_status = (
                    "Оплачена"
                    if variable_remaining <= Decimal("0.0001")
                    else ("Частично" if variable_paid > Decimal("0.0001") else "Не оплачена")
                )
                stable_status = (
                    "Оплачена"
                    if stable_remaining <= Decimal("0.0001")
                    else ("Частично" if stable_paid > Decimal("0.0001") else "Не оплачена")
                )

                base_line = {
                    "id": line.id,
                    "description": line.description,
                    "amount_net": float(variable_net),
                    "amount_vat": float(variable_vat),
                    "amount_total": float(variable_total),
                    "payment_status": variable_status,
                    "paid_amount": float(variable_paid),
                    "remaining_amount": float(_money2(variable_remaining)),
                }

                lines_out.append(base_line)
                service_label = _stable_service_label(line.description)
                stable_label = "Стабильный тариф"
                if service_label:
                    stable_label = f"{stable_label} ({service_label})"

                lines_out.append({
                    "id": line.id,
                    "description": stable_label,
                    "amount_net": float(stable_fee_net),
                    "amount_vat": float(stable_fee_vat),
                    "amount_total": float(stable_fee_total),
                    "payment_status": stable_status,
                    "paid_amount": float(stable_paid),
                    "remaining_amount": float(_money2(stable_remaining)),
                })
                continue

        line_payment = line_payment_map.get(
            int(line.id),
            {"paid": Decimal("0"), "remaining": Decimal(str(line.amount_total or 0)), "status": "Не оплачена"},
        )
        base_line["payment_status"] = line_payment["status"]
        base_line["paid_amount"] = float(_money2(line_payment["paid"]))
        base_line["remaining_amount"] = float(_money2(line_payment["remaining"]))
        lines_out.append(base_line)

    return {
        "id": inv.id,
        "resident_id": inv.resident_id,
        "resident_code": resident_code,
        "resident_owner_full_name": (resident.owner_full_name if resident else None),
        "number": inv.number,
        "status": inv.status.value,
        "due_date": inv.due_date,
        "notes": inv.notes,
        "period_year": inv.period_year,
        "period_month": inv.period_month,
        "period_dates": period_dates,
        "amount_net": float(inv.amount_net),
        "amount_vat": float(inv.amount_vat),
        "amount_total": float(inv.amount_total),
        "paid_amount": float(paid_total),
        "remaining_amount": remaining,
        "lines": lines_out,
        "payments": payments,
    }


@router.get("/detail/{resident_id}")
def get_resident_detail(
    resident_id: int,
    request: Request,
    db: Session = Depends(get_db),
    from_date: Optional[str] = None,  # YYYY-MM-DD
    to_date: Optional[str] = None,     # YYYY-MM-DD
    range_: Optional[str] = None,      # 'month' | '3m' | '6m' | 'year'
):
    """
    Get detailed information about a resident including meters and readings history.
    Supports date filtering.
    """
    try:
        user = _get_resident_user(request, db)
        if not user:
            raise HTTPException(status_code=401, detail="Unauthorized")

        # Load user with residents relationship
        user_with_residents = (
            db.query(User)
            .options(joinedload(User.resident_links).joinedload(Resident.block))
            .filter(User.id == user.id)
            .first()
        )
        
        if not user_with_residents:
            raise HTTPException(status_code=401, detail="Unauthorized")

        # Check if resident belongs to user and load with meters
        resident = (
            db.query(Resident)
            .options(joinedload(Resident.meters).joinedload(ResidentMeter.tariff))
            .options(joinedload(Resident.block))
            .filter(Resident.id == resident_id)
            .first()
        )
        if not resident or resident not in (user_with_residents.resident_links or []):
            raise HTTPException(status_code=403, detail="Forbidden")

        # Calculate date range
        start_dt: Optional[datetime] = None
        end_dt: Optional[datetime] = None
        today = datetime.utcnow().date()

        if range_ in {"month", "3m", "6m", "year"}:
            if range_ == "month":
                start_dt = datetime(today.year, today.month, 1)
                end_dt = datetime(
                    today.year + (1 if today.month == 12 else 0),
                    (1 if today.month == 12 else today.month + 1),
                    1
                )
            elif range_ == "3m":
                m = today.month - 2
                y = today.year
                while m <= 0:
                    m += 12
                    y -= 1
                start_dt = datetime(y, m, 1)
                end_dt = datetime(
                    today.year + (1 if today.month == 12 else 0),
                    (1 if today.month == 12 else today.month + 1),
                    1
                )
            elif range_ == "6m":
                m = today.month - 5
                y = today.year
                while m <= 0:
                    m += 12
                    y -= 1
                start_dt = datetime(y, m, 1)
                end_dt = datetime(
                    today.year + (1 if today.month == 12 else 0),
                    (1 if today.month == 12 else today.month + 1),
                    1
                )
            elif range_ == "year":
                m = today.month - 11
                y = today.year
                while m <= 0:
                    m += 12
                    y -= 1
                start_dt = datetime(y, m, 1)
                end_dt = datetime(
                    today.year + (1 if today.month == 12 else 0),
                    (1 if today.month == 12 else today.month + 1),
                    1
                )

        if start_dt is None and end_dt is None and (from_date or to_date):
            try:
                if from_date:
                    y, m, d = map(int, from_date.split("-"))
                    start_dt = datetime(y, m, d)
                if to_date:
                    y2, m2, d2 = map(int, to_date.split("-"))
                    end_dt = datetime(y2, m2, d2) + timedelta(days=1)
            except Exception:
                start_dt, end_dt = None, None

        # Get resident info
        block_name = resident.block.name if resident.block else ""
        resident_code = f"{block_name} / {resident.unit_number}" if block_name else resident.unit_number

        # Get meters with readings
        from ..models import MeterReading, MeterType
        
        meters_data = []

        # Если есть реальные показания канализации в периоде, авто-канализацию не добавляем.
        real_sewerage_query = (
            db.query(MeterReading.id)
            .join(ResidentMeter, ResidentMeter.id == MeterReading.resident_meter_id)
            .filter(
                ResidentMeter.resident_id == resident.id,
                ResidentMeter.meter_type == MeterType.SEWERAGE,
            )
        )
        if start_dt is not None:
            real_sewerage_query = real_sewerage_query.filter(MeterReading.reading_date >= start_dt)
        if end_dt is not None:
            real_sewerage_query = real_sewerage_query.filter(MeterReading.reading_date < end_dt)
        has_real_sewerage = real_sewerage_query.first() is not None

        # Ensure meters are loaded - they should already be loaded from the query above
        for meter in resident.meters if resident.meters else []:
            # Get readings for this meter with date filter
            readings_query = (
                db.query(MeterReading)
                .filter(MeterReading.resident_meter_id == meter.id)
            )

            if start_dt is not None:
                readings_query = readings_query.filter(MeterReading.reading_date >= start_dt)
            if end_dt is not None:
                readings_query = readings_query.filter(MeterReading.reading_date < end_dt)

            readings = readings_query.order_by(
                MeterReading.reading_date.desc(),
                MeterReading.id.desc()
            ).all()

            # Determine meter type display name and unit
            if meter.meter_type == MeterType.ELECTRIC:
                display_type = "Электричество"
                unit = "кВт⋅ч"
            elif meter.meter_type == MeterType.GAS:
                display_type = "Газ"
                unit = "м³"
            elif meter.meter_type == MeterType.WATER:
                display_type = "Вода"
                unit = "м³"
            elif meter.meter_type == MeterType.SEWERAGE:
                display_type = "Канализация"
                unit = "м³"
            elif meter.meter_type == MeterType.SERVICE:
                display_type = "Сервис"
                unit = "мес."
            elif meter.meter_type == MeterType.RENT:
                display_type = "Аренда"
                unit = "мес."
            elif meter.meter_type == MeterType.CONSTRUCTION:
                display_type = "Строительство"
                unit = "мес."
            else:
                display_type = "Неизвестно"
                unit = "—"

            # Format readings
            readings_data = []
            sewerage_readings = []
            for rd in readings:
                base_consumption = Decimal(str(rd.consumption or 0))
                base_charge = _money2(Decimal(str(rd.amount_total or 0)))
                out_consumption = base_consumption
                out_charge = base_charge

                if meter.meter_type == MeterType.WATER and not has_real_sewerage:
                    tariff_for_reading = db.get(Tariff, rd.tariff_id) if rd.tariff_id else meter.tariff
                    percent = _effective_sewerage_percent(tariff_for_reading)
                    if tariff_for_reading and tariff_for_reading.meter_type == MeterType.WATER and percent > 0:
                        k = percent / Decimal("100")
                        if getattr(tariff_for_reading, "customer_type", None) == CustomerType.INDIVIDUAL:
                            out_consumption = base_consumption * (Decimal("1") - k)
                            out_charge = _money2(base_charge * (Decimal("1") - k))
                            sewer_consumption = base_consumption * k
                            sewer_charge = _money2(base_charge - out_charge)
                        else:
                            out_consumption = base_consumption
                            out_charge = base_charge
                            sewer_consumption = base_consumption * k
                            sewer_charge = _money2(base_charge * k)

                        sewerage_readings.append({
                            "id": f"auto-sewerage-{rd.id}",
                            "date": rd.reading_date.strftime("%d.%m.%Y"),
                            "reading": str(sewer_consumption),
                            "consumption": str(sewer_consumption),
                            "charge": float(sewer_charge),
                            "vat": float(rd.vat_percent) if hasattr(rd, 'vat_percent') and rd.vat_percent else 0,
                            "comment": "Авто (от воды)",
                        })

                readings_data.append({
                    "id": rd.id,
                    "date": rd.reading_date.strftime("%d.%m.%Y"),
                    "reading": str(rd.value),
                    "consumption": str(out_consumption),
                    "charge": float(out_charge),
                    "vat": float(rd.vat_percent) if hasattr(rd, 'vat_percent') and rd.vat_percent else 0,
                    "comment": rd.note or "",
                })

            tariff_name = meter.tariff.name if meter.tariff else "—"
            
            meters_data.append({
                "id": meter.id,
                "type": meter.meter_type.value,
                "display_type": display_type,
                "serial_number": meter.serial_number,
                "tariff_name": tariff_name,
                "unit": unit,
                "readings": readings_data,
            })

            if meter.meter_type == MeterType.WATER and sewerage_readings:
                meters_data.append({
                    "id": f"auto-sewerage-{meter.id}",
                    "type": MeterType.SEWERAGE.value,
                    "display_type": "Канализация",
                    "serial_number": meter.serial_number,
                    "tariff_name": tariff_name,
                    "unit": "м³",
                    "readings": sewerage_readings,
                })

        from_val = start_dt.strftime("%Y-%m-%d") if start_dt else ""
        to_val = (end_dt - timedelta(days=1)).strftime("%Y-%m-%d") if end_dt else ""

        return {
            "resident": {
                "id": resident.id,
                "code": resident_code,
                "block_name": block_name,
                "unit_number": resident.unit_number,
                "status": resident.status.value if resident.status else "unknown",
            },
            "meters": meters_data,
            "filters": {
                "from_date": from_val,
                "to_date": to_val,
                "range": range_ or "",
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"ERROR in get_resident_detail: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


class ResidentDashboardData(BaseModel):
    id: int
    code: str  # "E / 444"
    full_name: Optional[str] = None  # owner_full_name for display on advance card
    month_due: float
    month_total: float
    month_paid: float
    debt_total: float
    advance_total: float
    pay_now: float
    due_date: Optional[date] = None
    due_state: str  # "ok" | "soon" | "over" | "none"
    # Invoice for server "current" calendar month (same period as month_due), if issued
    current_month_invoice_id: Optional[int] = None


class DashboardSummary(BaseModel):
    total_month: float
    total_debt: float
    total_advance: float
    total_pay_now: float
    unpaid_invoices_count: int = 0
    monthly_kwh: float = 0.0
    monthly_water_m3: float = 0.0
    monthly_gas_m3: float = 0.0
    active_notifications_count: int = 0


class ResidentDashboardResponse(BaseModel):
    user: dict
    residents: List[ResidentDashboardData]
    summary: DashboardSummary


# --------- Appeals (resident requests) API, reusing Notification model ---------


class ResidentAppeal(BaseModel):
    id: int
    resident_id: int
    resident_code: str
    message: str
    status: str
    created_at: datetime
    appeal_workflow: Optional[str] = None
    staff_message: Optional[str] = None
    workflow_updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ResidentAppealCreate(BaseModel):
    resident_id: int
    message: str


class ResidentAppealUpdate(BaseModel):
    message: str


def _resident_appeal_from_notif(notif: Notification, resident_code: str) -> ResidentAppeal:
    return ResidentAppeal(
        id=notif.id,
        resident_id=notif.resident_id or 0,
        resident_code=resident_code,
        message=notif.message,
        status=notif.status.value,
        created_at=notif.created_at,
        appeal_workflow=notif.appeal_workflow,
        staff_message=notif.staff_message,
        workflow_updated_at=notif.workflow_updated_at,
    )


def _get_resident_user(request: Request, db: Session) -> Optional[User]:
    """Get current resident user from session."""
    uid = get_user_id_from_session(request)
    if not uid:
        return None
    user = db.get(User, uid)
    if not user or user.role != RoleEnum.RESIDENT or not user.is_active:
        return None
    return user


def _build_portal_dashboard_context(db: Session, user: User) -> dict:
    """
    Build dashboard context in the same structure that `resident_portal.py`
    expects for server-rendered HTML (`resident_dashboard.html`).

    IMPORTANT: This is source-of-truth business logic and should live in api_*.py.
    """
    # Load user with residents relationship (blocks needed for template)
    user_with_residents = (
        db.query(User)
        .options(joinedload(User.resident_links).joinedload(Resident.block))
        .filter(User.id == user.id)
        .first()
    )
    if not user_with_residents:
        raise HTTPException(status_code=401, detail="Unauthorized")

    residents = user_with_residents.resident_links or []
    today = datetime.utcnow().date()
    cur_y, cur_m = today.year, today.month

    month_due: dict[int, Decimal] = {}
    month_total: dict[int, Decimal] = {}
    month_paid: dict[int, Decimal] = {}
    debt_total: dict[int, Decimal] = {}
    advance_total: dict[int, Decimal] = {}
    pay_now: dict[int, Decimal] = {}
    due_info: dict[int, dict] = {}

    for r in residents:
        inv_month = (
            db.query(Invoice)
            .filter(
                Invoice.resident_id == r.id,
                Invoice.period_year == cur_y,
                Invoice.period_month == cur_m,
                Invoice.status != InvoiceStatus.CANCELED,
            )
            .first()
        )
        if inv_month:
            paid_m = (
                db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
                .filter(PaymentApplication.invoice_id == inv_month.id)
                .scalar()
                or 0
            )
            md = Decimal(inv_month.amount_total or 0) - Decimal(paid_m)
            month_due[r.id] = md if md > 0 else Decimal("0")
            month_total[r.id] = Decimal(inv_month.amount_total or 0)
            month_paid[r.id] = Decimal(paid_m)

            due = inv_month.due_date
            state = "none"
            # Не показывать срок оплаты, если счёт за текущий месяц полностью оплачен
            if Decimal(paid_m) >= Decimal(inv_month.amount_total or 0):
                due_info[r.id] = {"due_date": None, "state": "none"}
            elif due:
                days = (due - today).days
                if days < 0:
                    state = "over"
                elif days <= 3:
                    state = "soon"
                else:
                    state = "ok"
                due_info[r.id] = {"due_date": due, "state": state}
            else:
                due_info[r.id] = {"due_date": due, "state": state}
        else:
            month_due[r.id] = Decimal("0")
            month_total[r.id] = Decimal("0")
            month_paid[r.id] = Decimal("0")
            due_info[r.id] = {"due_date": None, "state": "none"}

        inv_total = (
            db.query(func.coalesce(func.sum(Invoice.amount_total), 0))
            .filter(Invoice.resident_id == r.id, Invoice.status != InvoiceStatus.CANCELED)
            .scalar()
            or 0
        )
        paid_total = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .join(Invoice, Invoice.id == PaymentApplication.invoice_id)
            .filter(Invoice.resident_id == r.id, Invoice.status != InvoiceStatus.CANCELED)
            .scalar()
            or 0
        )
        debt = Decimal(inv_total) - Decimal(paid_total)
        debt_total[r.id] = debt if debt > 0 else Decimal("0")

        pay_sum = (
            db.query(func.coalesce(func.sum(Payment.amount_total), 0))
            .filter(Payment.resident_id == r.id)
            .scalar()
            or 0
        )
        appl_sum = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .join(Payment, Payment.id == PaymentApplication.payment_id)
            .filter(Payment.resident_id == r.id)
            .scalar()
            or 0
        )
        adv = Decimal(pay_sum) - Decimal(appl_sum)
        advance_total[r.id] = adv if adv > 0 else Decimal("0")

        pay_now[r.id] = max(debt_total[r.id] - advance_total[r.id], Decimal("0"))

    total_month = sum(month_due.values(), Decimal("0"))
    total_debt = sum(debt_total.values(), Decimal("0"))
    total_adv = sum(advance_total.values(), Decimal("0"))
    total_pay = sum(pay_now.values(), Decimal("0"))

    resident_ids = [r.id for r in residents]
    pending_online = []
    pending_count = 0
    if resident_ids:
        pending_rows = (
            db.query(OnlineTransaction)
            .filter(
                OnlineTransaction.resident_id.in_(resident_ids),
                OnlineTransaction.gateway_status == "INITIATED",
            )
            .order_by(OnlineTransaction.created_at.desc(), OnlineTransaction.id.desc())
            .limit(20)
            .all()
        )
        pending_online = pending_rows
        pending_count = len(pending_rows)

    return {
        "user": user,
        "residents": residents,
        "month_due": month_due,
        "month_total": month_total,
        "month_paid": month_paid,
        "debt_total": debt_total,
        "advance_total": advance_total,
        "pay_now": pay_now,
        "due_info": due_info,
        "total_month": total_month,
        "total_debt": total_debt,
        "total_adv": total_adv,
        "total_pay": total_pay,
        "pending_online": pending_online,
        "pending_online_count": pending_count,
    }


@router.get("/appeals", response_model=List[ResidentAppeal])
def get_resident_appeals(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    List recent appeals (notifications) for current resident user.
    """
    user = _get_resident_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    notifications = (
        db.query(Notification)
        .join(Resident, Resident.id == Notification.resident_id)
        .filter(
            Notification.user_id == user.id,
            or_(
                Notification.notification_type == "APPEAL",
                Notification.notification_type == None
            )
        )
        .order_by(Notification.created_at.desc())
        .limit(50)
        .all()
    )

    result: List[ResidentAppeal] = []
    for notif in notifications:
        block_name = notif.resident.block.name if notif.resident and notif.resident.block else ""
        unit_number = notif.resident.unit_number if notif.resident else ""
        resident_code = f"{block_name} / {unit_number}" if block_name and unit_number else unit_number or block_name or ""
        result.append(_resident_appeal_from_notif(notif, resident_code))

    return result


@router.post("/appeals", response_model=ResidentAppeal)
def create_resident_appeal(
    data: ResidentAppealCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Create new appeal from current resident user.
    """
    user = _get_resident_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    message = (data.message or "").strip()
    if len(message) < 5:
        raise HTTPException(status_code=400, detail="Message is too short")

    resident = db.get(Resident, data.resident_id)
    if not resident or resident not in (user.resident_links or []):
        raise HTTPException(status_code=403, detail="Invalid resident")

    notif = Notification(
        user_id=user.id,
        resident_id=resident.id,
        message=message,
        status=NotificationStatus.UNREAD,
        notification_type="APPEAL",
        created_at=datetime.utcnow()
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)

    block_name = resident.block.name if resident.block else ""
    resident_code = f"{block_name} / {resident.unit_number}" if block_name else resident.unit_number

    return _resident_appeal_from_notif(notif, resident_code)


@router.put("/appeals/{appeal_id}", response_model=ResidentAppeal)
def update_resident_appeal(
    appeal_id: int,
    data: ResidentAppealUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Update appeal text for current resident user (only while UNREAD).
    """
    user = _get_resident_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    notif = db.get(Notification, appeal_id)
    if not notif or notif.user_id != user.id:
        raise HTTPException(status_code=404, detail="Appeal not found")

    if notif.status != NotificationStatus.UNREAD:
        raise HTTPException(status_code=400, detail="Cannot edit read appeal")

    message = (data.message or "").strip()
    if len(message) < 5:
        raise HTTPException(status_code=400, detail="Message is too short")

    notif.message = message
    db.commit()
    db.refresh(notif)

    resident = notif.resident
    block_name = resident.block.name if resident and resident.block else ""
    unit_number = resident.unit_number if resident else ""
    resident_code = f"{block_name} / {unit_number}" if block_name and unit_number else unit_number or block_name or ""

    return _resident_appeal_from_notif(notif, resident_code)


@router.delete("/appeals/{appeal_id}")
def delete_resident_appeal(
    appeal_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Delete appeal for current resident user.
    """
    user = _get_resident_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    notif = db.get(Notification, appeal_id)
    if not notif or notif.user_id != user.id:
        raise HTTPException(status_code=404, detail="Appeal not found")

    db.delete(notif)
    db.commit()

    return {"ok": True}


# --------- Resident Payment API ---------


class ResidentPaymentCreate(BaseModel):
    resident_id: int
    amount: float
    method: str = "CARD"  # CARD, TRANSFER, CASH, ONLINE
    reference: Optional[str] = None
    comment: Optional[str] = None
    scope: Optional[str] = None  # 'month' - только текущий месяц, 'all' - все счета, None - только пополнение аванса
    invoice_id: Optional[int] = None  # оплата конкретного счета
    selected_line_ids: Optional[List[int]] = None  # выбор конкретных строк счета для оплаты


class ResidentPaymentResponse(BaseModel):
    ok: bool
    payment_id: int
    message: str


@router.post("/payment", response_model=ResidentPaymentResponse)
def create_resident_payment(
    data: ResidentPaymentCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Create a payment from the resident portal.
    This endpoint allows authenticated resident users to submit payments.
    The payment will be automatically applied to open invoices based on scope:
    - scope='month': only current month invoices
    - scope='all': all open invoices
    - scope=None: no application (advance top-up only)
    """
    from ..models import Payment, PaymentMethod
    
    user = _get_resident_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Load user with residents relationship
    user_with_residents = (
        db.query(User)
        .options(joinedload(User.resident_links))
        .filter(User.id == user.id)
        .first()
    )
    
    if not user_with_residents:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Validate resident belongs to user
    resident_ids = [r.id for r in (user_with_residents.resident_links or [])]
    if data.resident_id not in resident_ids:
        raise HTTPException(status_code=403, detail="Invalid resident")

    # Validate amount
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    payment_amount_dec = Decimal(str(data.amount))
    selected_remaining_total = Decimal("0")

    # Validate payment method
    method_value = data.method.upper()
    is_advance = (method_value == "ADVANCE")
    
    if not is_advance:
        valid_methods = {m.value for m in PaymentMethod}
        if method_value not in valid_methods:
            # Если это не аванс и не известный метод, то для онлайн-оплат из портала 
            # мы разрешаем только CARD или TRANSFER. 
            # Если пришло что-то странное - принудительно ставим CARD.
            method_value = "CARD"

    from .api_payment_logic import (
        apply_payment_to_invoices,
        apply_advance_with_limit,
        apply_payment_to_invoice,
        apply_advance_to_invoice,
    )

    # Безопасность: в режиме пополнения аванса (scope=None) платеж не должен быть привязан к счёту.
    request_invoice_id = data.invoice_id if data.scope is not None else None
    request_selected_line_ids = data.selected_line_ids if request_invoice_id is not None else None

    if request_selected_line_ids and request_invoice_id is None:
        raise HTTPException(status_code=400, detail="selected_line_ids requires invoice_id")
    
    # Если указан invoice_id, проверяем доступность счета
    target_invoice = None
    selected_line_ids: list[int] = []
    application_reference: Optional[str] = None
    if request_invoice_id is not None:
        target_invoice = db.get(Invoice, request_invoice_id)
        if not target_invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if target_invoice.resident_id not in resident_ids:
            raise HTTPException(status_code=403, detail="Invalid invoice")
        if target_invoice.status in {InvoiceStatus.CANCELED}:
            raise HTTPException(status_code=400, detail="Invoice is canceled")

        if request_selected_line_ids:
            selected_line_ids = []
            for x in request_selected_line_ids:
                try:
                    v = int(x)
                except Exception:
                    continue
                if v > 0:
                    selected_line_ids.append(v)
            selected_line_ids = sorted(set(selected_line_ids))
            if not selected_line_ids:
                raise HTTPException(status_code=400, detail="Выберите хотя бы одну строку счёта")

            all_invoice_lines = (
                db.query(InvoiceLine)
                .filter(InvoiceLine.invoice_id == target_invoice.id)
                .all()
            )
            existing_ids = {int(row.id) for row in all_invoice_lines if row.id is not None}
            # Мягкая нормализация: отбрасываем устаревшие id вместо жесткой 400-ошибки.
            selected_line_ids = [lid for lid in selected_line_ids if lid in existing_ids]
            if not selected_line_ids:
                raise HTTPException(status_code=400, detail="Некорректный выбор строк счёта")

            line_desc_by_id = {
                int(row.id): (row.description or "")
                for row in all_invoice_lines
                if row.id is not None
            }
            selected_line_ids = _normalize_selected_line_ids_for_water_sewer(selected_line_ids, line_desc_by_id)
            selected_line_ids = [lid for lid in selected_line_ids if lid in existing_ids]
            if not selected_line_ids:
                raise HTTPException(status_code=400, detail="Некорректный выбор строк счёта")

            existing_apps = (
                db.query(PaymentApplication)
                .filter(PaymentApplication.invoice_id == target_invoice.id)
                .all()
            )
            line_payment_map = _build_invoice_line_payment_map(all_invoice_lines, existing_apps)
            selected_remaining = sum(
                (line_payment_map.get(lid, {}).get("remaining", Decimal("0")) for lid in selected_line_ids),
                Decimal("0"),
            )
            if selected_remaining <= Decimal("0.0001"):
                raise HTTPException(status_code=400, detail="Выбранные строки уже оплачены")
            selected_remaining_total = _money2(selected_remaining)
            application_reference = "LINESEL:" + ",".join(str(x) for x in selected_line_ids)

    # Если оплата через аванс, создаем историю списания и корректируем общий баланс
    if is_advance:
        # БЛОКИРОВКА: предотвращаем одновременные списания аванса
        # Используем простой select без JOIN-ов для блокировки
        from sqlalchemy import select
        db.execute(select(Resident.id).where(Resident.id.in_(resident_ids)).with_for_update()).all()
        
        # 1) Проверяем наличие средств в авансе перед списанием
        total_advance = Decimal("0")
        if resident_ids:
            from sqlalchemy import cast, String
            all_user_payments = db.query(Payment).filter(
                Payment.resident_id.in_(resident_ids),
                cast(Payment.method, String) != 'ADVANCE'
            ).all()
            for p in all_user_payments:
                applied_p = db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))\
                              .filter(PaymentApplication.payment_id == p.id).scalar() or 0
                total_advance += (Decimal(p.amount_total or 0) - Decimal(applied_p))
        
        target_amount = payment_amount_dec
        if selected_line_ids and target_amount > selected_remaining_total:
            # Для списания из аванса не допускаем сумму выше выбранных строк:
            # в отличие от CARD-платежа здесь "излишек в аванс" невозможен.
            target_amount = selected_remaining_total
        if total_advance < target_amount:
            raise HTTPException(status_code=400, detail=f"Недостаточно средств на авансе (доступно {total_advance} ₼)")

        try:
            # 2) Создаем технический Payment (метод ADVANCE) ТОЛЬКО для истории
            # ВАЖНО: Мы НЕ создаем PaymentApplication для этого платежа напрямую,
            # так как приложения создаются на реальных платежах и помечаются reference_tag.
            reference_value = "ADVANCE_USE"
            comment_suffix = ""
            if target_invoice:
                reference_value = f"ADVANCE_USE:{target_invoice.id}"
                comment_suffix = f" (счёт {target_invoice.number or target_invoice.id})"

            duplicate_cutoff = datetime.utcnow() - timedelta(seconds=15)
            existing_recent = (
                db.query(Payment)
                .filter(
                    Payment.resident_id == data.resident_id,
                    Payment.method == PaymentMethod.ADVANCE,
                    Payment.created_by_id == user.id,
                    Payment.reference == reference_value,
                    Payment.amount_total == target_amount,
                    Payment.created_at >= duplicate_cutoff,
                )
                .order_by(Payment.id.desc())
                .first()
            )
            if existing_recent:
                return ResidentPaymentResponse(
                    ok=True,
                    payment_id=existing_recent.id,
                    message="Аванс уже был применён (повторный запрос проигнорирован)",
                )

            history_payment = Payment(
                resident_id=data.resident_id,
                received_at=now_baku(),
                amount_total=target_amount,
                method=PaymentMethod.ADVANCE,
                reference=reference_value,
                comment=f"Списание из аванса: {target_amount} ₼" + comment_suffix,
                created_by_id=user.id,
            )
            db.add(history_payment)
            db.flush()

            reference_tag = f"ADVANCE:{history_payment.id}"
            if application_reference:
                reference_tag = f"{reference_tag}|{application_reference}"

            # 3) Списываем реальные деньги из существующих платежей
            # Это создаст PaymentApplication с reference_tag к реальным платежам
            if target_invoice:
                # Для оплаты из аванса по конкретному счёту запрещаем сумму больше остатка
                paid_total = (
                    db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
                    .filter(PaymentApplication.invoice_id == target_invoice.id)
                    .scalar() or Decimal("0")
                )
                inv_leftover = Decimal(target_invoice.amount_total or 0) - Decimal(paid_total)
                if inv_leftover <= 0:
                    raise HTTPException(status_code=400, detail="Счёт уже оплачен")
                if target_amount > inv_leftover:
                    raise HTTPException(status_code=400, detail="Сумма оплаты из аванса не может превышать сумму счёта")

                affected_count = apply_advance_to_invoice(
                    db,
                    user.id,
                    data.resident_id,
                    target_invoice.id,
                    max_amount=target_amount,
                    reference_tag=reference_tag,
                )
            else:
                # Если scope не указан, по умолчанию бьем только текущий месяц,
                # чтобы не разбрасывать оплату по старым долгам.
                effective_scope = data.scope if data.scope in ("all", "month") else "month"

                affected_count = apply_advance_with_limit(
                    db,
                    user.id,
                    data.resident_id,
                    max_amount=target_amount,
                    scope=effective_scope,
                    reference_tag=reference_tag,
                )

            if affected_count <= 0:
                db.rollback()
                raise HTTPException(status_code=400, detail="Нет выставленных счетов для оплаты из аванса")
            
            # ЛОГИРОВАНИЕ действия
            db.add(PaymentLog(
                payment_id=history_payment.id,
                resident_id=data.resident_id,
                user_id=user.id,
                action="ADVANCE_USE",
                amount=float(target_amount),
                details=f"Списание из аванса. Распределено по {affected_count} счетам."
            ))
            
            db.commit()
            
            if target_invoice:
                if affected_count > 0:
                    if selected_line_ids and payment_amount_dec > selected_remaining_total:
                        message = f"Аванс применён к выбранным строкам. Сумма скорректирована до {float(target_amount):.2f} ₼"
                    else:
                        message = "Аванс успешно применён к выбранному счёту"
                else:
                    message = "Аванс не применён (нет подходящих открытых счетов)"
            else:
                if data.scope is None:
                    message = "Аванс зарезервирован (scope=None)"
                elif affected_count > 0:
                    message = f"Аванс успешно применён к {affected_count} счёт(ам)"
                else:
                    message = f"Аванс не применён (нет подходящих открытых счетов)"
                
        except Exception as e:
            if isinstance(e, HTTPException):
                raise
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Ошибка при обработке аванса: {str(e)}")
        
        return ResidentPaymentResponse(
            ok=True,
            payment_id=history_payment.id,
            message=message
        )
    
    # Для обычных платежей (CARD, TRANSFER и т.д.) создаем новый платеж
    payment = Payment(
        resident_id=data.resident_id,
        received_at=now_baku(),
        amount_total=payment_amount_dec,
        method=PaymentMethod(method_value),
        reference=data.reference or None,
        comment=data.comment or f"Онлайн-оплата через личный кабинет",
        created_by_id=None,  # Self-payment by resident
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)

    # ЛОГИРОВАНИЕ создания
    db.add(PaymentLog(
        payment_id=payment.id,
        resident_id=data.resident_id,
        user_id=user.id,
        action="CREATE",
        amount=float(payment_amount_dec),
        details=f"Онлайн-оплата (метод: {method_value})"
    ))

    # Apply payment to invoices based on scope
    # scope='month' - только текущий месяц, 'all' - все счета, None - только пополнение аванса
    try:
        if target_invoice:
            max_apply_amount = selected_remaining_total if selected_line_ids else None
            applied_amount = apply_payment_to_invoice(
                db,
                payment.id,
                target_invoice.id,
                reference=application_reference,
                max_amount=max_apply_amount,
            )
            affected_count = 1 if applied_amount > 0 else 0
        else:
            affected_count = apply_payment_to_invoices(
                db, 
                payment.id, 
                data.resident_id, 
                scope=data.scope
            )
        db.commit()
        
        if target_invoice:
            if affected_count > 0:
                if selected_line_ids and payment_amount_dec > selected_remaining_total:
                    message = "Платёж применён к выбранным строкам. Излишек зачислен в аванс"
                else:
                    message = "Платёж успешно создан и применён к выбранному счёту"
            else:
                message = "Платёж успешно создан (не применён к выбранному счёту)"
        else:
            if data.scope is None:
                message = "Платёж успешно создан (пополнение аванса)"
            elif affected_count > 0:
                message = f"Платёж успешно создан и применён к {affected_count} счёт(ам)"
            else:
                message = "Платёж успешно создан (не применён к счетам - нет подходящих счетов)"
    except Exception as e:
        print(f"Warning: apply_payment_to_invoices failed: {e}")
        # Payment still created, just not applied
        message = "Платёж успешно создан, но не применён к счетам"

    return ResidentPaymentResponse(
        ok=True,
        payment_id=payment.id,
        message=message
    )


@router.get("/dashboard", response_model=ResidentDashboardResponse)
def get_resident_dashboard(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Get dashboard data for the current resident user.
    Returns financial summary: month due, debt, advance, pay now.
    """
    user = _get_resident_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Load user with residents relationship
    user_with_residents = (
        db.query(User)
        .options(joinedload(User.resident_links).joinedload(Resident.block))
        .filter(User.id == user.id)
        .first()
    )
    
    if not user_with_residents:
        raise HTTPException(status_code=401, detail="Unauthorized")

    residents = user_with_residents.resident_links or []
    today = datetime.utcnow().date()
    cur_y, cur_m = today.year, today.month

    # Calculate financial data for each resident
    month_due: dict[int, Decimal] = {}
    month_total: dict[int, Decimal] = {}
    month_paid: dict[int, Decimal] = {}
    total_debt_per_res: dict[int, Decimal] = {}
    due_info: dict[int, dict] = {}

    resident_ids = [r.id for r in residents]

    # Global advance calculation for the user
    # Calculate leftover for each payment belonging to the user's residents
    total_advance_calc = Decimal("0")
    if resident_ids:
        # Get all payments (excluding ADVANCE records)
        from sqlalchemy import cast, String
        all_user_payments = db.query(Payment).filter(
            Payment.resident_id.in_(resident_ids),
            cast(Payment.method, String) != 'ADVANCE'
        ).all()
        for p in all_user_payments:
            # Sum of applications for THIS payment
            applied_p = db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))\
                          .filter(PaymentApplication.payment_id == p.id).scalar() or 0
            left = Decimal(p.amount_total or 0) - Decimal(applied_p)
            total_advance_calc += left
    
    shared_advance = total_advance_calc
    if shared_advance < 0: shared_advance = Decimal("0")

    current_month_invoice_id_by_resident: dict[int, Optional[int]] = {}

    for r in residents:
        # Current month invoice
        inv_month = (
            db.query(Invoice)
            .filter(
                Invoice.resident_id == r.id,
                Invoice.period_year == cur_y,
                Invoice.period_month == cur_m,
                Invoice.status != InvoiceStatus.CANCELED
            )
            .first()
        )
        current_month_invoice_id_by_resident[r.id] = inv_month.id if inv_month else None

        if inv_month:
            paid_m = (
                db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
                .filter(PaymentApplication.invoice_id == inv_month.id)
                .scalar() or 0
            )
            md = Decimal(inv_month.amount_total or 0) - Decimal(paid_m)
            month_due[r.id] = md if md > 0 else Decimal("0")
            month_total[r.id] = Decimal(inv_month.amount_total or 0)
            month_paid[r.id] = Decimal(paid_m)

            due = inv_month.due_date
            state = "none"
            # Не показывать срок оплаты, если счёт за текущий месяц полностью оплачен
            if Decimal(paid_m) >= Decimal(inv_month.amount_total or 0):
                due_info[r.id] = {"due_date": None, "state": "none"}
            elif due:
                days = (due - today).days
                if days < 0:
                    state = "over"
                elif days <= 3:
                    state = "soon"
                else:
                    state = "ok"
                due_info[r.id] = {"due_date": due, "state": state}
            else:
                due_info[r.id] = {"due_date": due, "state": state}
        else:
            month_due[r.id] = Decimal("0")
            month_total[r.id] = Decimal("0")
            month_paid[r.id] = Decimal("0")
            due_info[r.id] = {"due_date": None, "state": "none"}

        # Total debt across all invoices
        inv_total = (
            db.query(func.coalesce(func.sum(Invoice.amount_total), 0))
            .filter(
                Invoice.resident_id == r.id,
                Invoice.status != InvoiceStatus.CANCELED
            )
            .scalar() or 0
        )
        paid_total = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .join(Invoice, Invoice.id == PaymentApplication.invoice_id)
            .filter(
                Invoice.resident_id == r.id,
                Invoice.status != InvoiceStatus.CANCELED
            )
            .scalar() or 0
        )
        debt = Decimal(inv_total) - Decimal(paid_total)
        total_debt_per_res[r.id] = debt if debt > 0 else Decimal("0")

    # Summary across all residents
    total_month_due = sum(month_due.values(), Decimal("0"))
    total_debt = sum(total_debt_per_res.values(), Decimal("0"))
    
    # Pay now = total debt - shared advance (minimum 0)
    # This reflects the potential to pay EVERYTHING from the shared pool
    total_pay_now = max(total_debt - shared_advance, Decimal("0"))

    # Count unpaid invoices
    unpaid_count = 0
    if resident_ids:
        unpaid_count = (
            db.query(func.count(Invoice.id))
            .filter(
                Invoice.resident_id.in_(resident_ids),
                Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL, InvoiceStatus.DRAFT])
            )
            .scalar() or 0
        )
        # Also count invoices with remaining amount
        unpaid_with_remaining = db.query(Invoice).filter(
            Invoice.resident_id.in_(resident_ids),
            Invoice.status != InvoiceStatus.CANCELED
        ).all()
        for inv in unpaid_with_remaining:
            paid = (
                db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
                .filter(PaymentApplication.invoice_id == inv.id)
                .scalar() or 0
            )
            if float(inv.amount_total or 0) - float(paid) > 0.01:
                if inv.status not in [InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL, InvoiceStatus.DRAFT]:
                    unpaid_count += 1

    # Calculate monthly utility consumptions (electricity, water, gas)
    monthly_kwh = Decimal("0")
    monthly_water_m3 = Decimal("0")
    monthly_gas_m3 = Decimal("0")
    if resident_ids:
        today_now = datetime.utcnow()
        month_start = datetime(today_now.year, today_now.month, 1)
        month_end = datetime(today_now.year + (1 if today_now.month == 12 else 0), 
                             (1 if today_now.month == 12 else today_now.month + 1), 1)
        
        from ..models import MeterType
        
        # Electricity consumption
        electric_readings = (
            db.query(MeterReading)
            .join(ResidentMeter, ResidentMeter.id == MeterReading.resident_meter_id)
            .filter(
                ResidentMeter.resident_id.in_(resident_ids),
                ResidentMeter.meter_type == MeterType.ELECTRIC,
                MeterReading.reading_date >= month_start,
                MeterReading.reading_date < month_end
            )
            .all()
        )
        monthly_kwh = sum(Decimal(str(rd.consumption or 0)) for rd in electric_readings)
        
        # Water consumption
        water_readings = (
            db.query(MeterReading)
            .join(ResidentMeter, ResidentMeter.id == MeterReading.resident_meter_id)
            .filter(
                ResidentMeter.resident_id.in_(resident_ids),
                ResidentMeter.meter_type == MeterType.WATER,
                MeterReading.reading_date >= month_start,
                MeterReading.reading_date < month_end
            )
            .all()
        )
        monthly_water_m3 = sum(Decimal(str(rd.consumption or 0)) for rd in water_readings)
        
        # Gas consumption
        gas_readings = (
            db.query(MeterReading)
            .join(ResidentMeter, ResidentMeter.id == MeterReading.resident_meter_id)
            .filter(
                ResidentMeter.resident_id.in_(resident_ids),
                ResidentMeter.meter_type == MeterType.GAS,
                MeterReading.reading_date >= month_start,
                MeterReading.reading_date < month_end
            )
            .all()
        )
        monthly_gas_m3 = sum(Decimal(str(rd.consumption or 0)) for rd in gas_readings)

    # Count active notifications (excluding resident's own appeals)
    active_notifications = 0
    if user:
        active_notifications = (
            db.query(func.count(Notification.id))
            .filter(
                Notification.user_id == user.id,
                Notification.status == NotificationStatus.UNREAD,
                Notification.notification_type != "APPEAL",
                Notification.notification_type != None
            )
            .scalar() or 0
        )

    # Format resident data
    residents_data = []
    
    for r in residents:
        block_name = r.block.name if r.block else ""
        code = f"{block_name} / {r.unit_number}" if block_name else r.unit_number
        
        # Общий долг по этому дому (все счета)
        total_res_debt = total_debt_per_res.get(r.id, Decimal("0"))
        
        # Долг за предыдущие периоды (все минус текущий месяц)
        # Если в карточке мы отдельно показываем "К оплате за месяц", то "Долг" должен быть остатком
        res_month_due = month_due.get(r.id, Decimal("0"))
        res_overdue_debt = max(total_res_debt - res_month_due, Decimal("0"))
        
        # К ОПЛАТЕ СЕЙЧАС для конкретного дома:
        # Теперь это просто долг этого дома (включая текущий месяц), 
        # БЕЗ учета общего аванса, так как списание не происходит автоматически.
        res_pay_now = total_res_debt
        
        residents_data.append(ResidentDashboardData(
            id=r.id,
            code=code,
            full_name=r.owner_full_name if r.owner_full_name else None,
            month_due=float(res_month_due),
            month_total=float(month_total.get(r.id, Decimal("0"))),
            month_paid=float(month_paid.get(r.id, Decimal("0"))),
            debt_total=float(res_overdue_debt), # Теперь показываем только просроченный долг
            advance_total=float(shared_advance), # Показываем ОБЩИЙ аванс на всех карточках
            pay_now=float(res_pay_now),
            due_date=due_info.get(r.id, {}).get("due_date"),
            due_state=due_info.get(r.id, {}).get("state", "none"),
            current_month_invoice_id=current_month_invoice_id_by_resident.get(r.id),
        ))

    return ResidentDashboardResponse(
        user={
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "phone": user.phone,
            "email": user.email,
        },
        residents=residents_data,
        summary=DashboardSummary(
            total_month=float(total_month_due),
            total_debt=float(total_debt),
            total_advance=float(shared_advance),
            total_pay_now=float(total_pay_now),
            unpaid_invoices_count=unpaid_count,
            monthly_kwh=float(monthly_kwh),
            monthly_water_m3=float(monthly_water_m3),
            monthly_gas_m3=float(monthly_gas_m3),
            active_notifications_count=active_notifications,
        ),
    )


# ---------------------------------------------------------------------------
# Advance deduction history — shows the user where their advance went
# ---------------------------------------------------------------------------

@router.get("/advance-history")
def get_advance_history(
    request: Request,
    page: int = 1,
    per_page: int = 15,
    db: Session = Depends(get_db),
):
    user_id = get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    resident_ids = [
        r[0] for r in db.query(user_residents.c.resident_id)
        .filter(user_residents.c.user_id == user.id).all()
    ]
    if not resident_ids:
        return {"items": [], "total": 0, "page": page, "per_page": per_page, "pages": 0, "total_applied": 0}

    try:
        base_q = (
            db.query(PaymentApplication)
            .join(Payment, Payment.id == PaymentApplication.payment_id)
            .filter(
                Payment.resident_id.in_(resident_ids),
                or_(
                    PaymentApplication.reference.like("ADVANCE%"),
                    PaymentApplication.reference.like("AUTOADV%"),
                ),
            )
        )

        total_count = base_q.count()

        total_applied_raw = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .join(Payment, Payment.id == PaymentApplication.payment_id)
            .filter(
                Payment.resident_id.in_(resident_ids),
                or_(
                    PaymentApplication.reference.like("ADVANCE%"),
                    PaymentApplication.reference.like("AUTOADV%"),
                ),
            )
            .scalar()
        )

        total_pages = max(1, (total_count + per_page - 1) // per_page)
        if page < 1:
            page = 1
        if page > total_pages:
            page = total_pages

        apps = (
            base_q
            .order_by(PaymentApplication.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        items = []
        for app in apps:
            inv = db.get(Invoice, app.invoice_id) if app.invoice_id else None
            ref = app.reference or ""
            is_auto = ref.startswith("AUTOADV")
            dt = None
            if app.created_at:
                try:
                    dt = app.created_at.isoformat()
                except Exception:
                    dt = str(app.created_at)
            items.append({
                "id": app.id,
                "date": dt,
                "amount": float(app.amount_applied or 0),
                "invoice_number": inv.number if inv else None,
                "invoice_id": inv.id if inv else None,
                "period": f"{inv.period_month:02d}/{inv.period_year}" if inv else None,
                "type": "auto" if is_auto else "manual",
            })

        return {
            "items": items,
            "total": total_count,
            "page": page,
            "per_page": per_page,
            "pages": total_pages,
            "total_applied": float(total_applied_raw),
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/advance-history/{application_id}")
def get_advance_history_detail(
    application_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Return invoice line breakdown for a specific PaymentApplication."""
    user_id = get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    resident_ids = [
        r[0] for r in db.query(user_residents.c.resident_id)
        .filter(user_residents.c.user_id == user.id).all()
    ]

    app_row = db.get(PaymentApplication, application_id)
    if not app_row:
        raise HTTPException(status_code=404, detail="Not found")

    payment = db.get(Payment, app_row.payment_id)
    if not payment or payment.resident_id not in resident_ids:
        raise HTTPException(status_code=403, detail="Forbidden")

    invoice = db.get(Invoice, app_row.invoice_id) if app_row.invoice_id else None
    amount_applied = float(app_row.amount_applied or 0)
    lines_out = []

    # Try real per-line data first
    real_lines = (
        db.query(PaymentApplicationLine, InvoiceLine)
        .join(InvoiceLine, InvoiceLine.id == PaymentApplicationLine.invoice_line_id)
        .filter(PaymentApplicationLine.application_id == app_row.id)
        .all()
    )

    if real_lines:
        for pal, il in real_lines:
            lines_out.append({
                "description": il.description,
                "amount_total": float(il.amount_total or 0),
                "applied_share": float(pal.amount or 0),
            })
    elif invoice and invoice.lines:
        # Fallback: proportional estimate for old records
        inv_total = float(invoice.amount_total or 0)
        for ln in invoice.lines:
            line_total = float(ln.amount_total or 0)
            share = round(amount_applied * line_total / inv_total, 2) if inv_total else 0
            lines_out.append({
                "description": ln.description,
                "amount_total": line_total,
                "applied_share": share,
            })
        total_shares = sum(l["applied_share"] for l in lines_out)
        diff = round(amount_applied - total_shares, 2)
        if diff != 0 and lines_out:
            largest = max(lines_out, key=lambda l: l["amount_total"])
            largest["applied_share"] = round(largest["applied_share"] + diff, 2)

    return {
        "id": app_row.id,
        "amount_applied": amount_applied,
        "invoice_number": invoice.number if invoice else None,
        "invoice_total": float(invoice.amount_total or 0) if invoice else 0,
        "lines": lines_out,
    }
