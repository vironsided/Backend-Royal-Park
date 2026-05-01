from typing import List, Optional
from datetime import datetime, date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, or_, and_

from ..database import get_db
from ..models import (
    User, RoleEnum, Block, Resident,
    Invoice, InvoiceStatus, InvoiceLine,
    PaymentApplication, Payment, PaymentMethod,
    Tariff, ResidentMeter, MeterReading, MeterType, CustomerType, user_residents
)
from ..deps import get_current_user
from ..utils import to_baku_datetime, create_invoice_notification, now_baku, build_invoice_number


router = APIRouter(prefix="/api/invoices", tags=["invoices-api"])


# Pydantic models
class InvoiceLineOut(BaseModel):
    id: Optional[int] = None
    description: str
    amount_net: float
    amount_vat: float
    amount_total: float
    payment_status: Optional[str] = None  # "Оплачена", "Частично", "Не оплачена"
    paid_amount: Optional[float] = None  # Сколько оплачено по этой строке
    remaining_amount: Optional[float] = None  # Сколько осталось оплатить
    meter_previous: Optional[float] = None
    meter_current: Optional[float] = None
    meter_unit: Optional[str] = None
    tariff_name: Optional[str] = None
    tariff_from_date: Optional[str] = None
    tariff_to_date: Optional[str] = None

    class Config:
        from_attributes = True


class InvoiceOut(BaseModel):
    id: int
    resident_id: int
    resident_code: str  # "A / 205"
    resident_info: str  # "Блок A, №205"
    block_name: str
    unit_number: str
    number: Optional[str] = None
    resident_user_phone: Optional[str] = None
    status: str
    due_date: Optional[date] = None
    notes: Optional[str] = None
    period_year: int
    period_month: int
    amount_net: float
    amount_vat: float
    amount_total: float
    paid_amount: float = 0.0  # сумма оплат
    created_at: datetime
    lines: List[InvoiceLineOut] = []

    class Config:
        from_attributes = True


def _to_int(val: str | None) -> int | None:
    """Мягкое приведение строкового query-параметра к int."""
    if val is None:
        return None
    v = str(val).strip()
    if v == "":
        return None
    try:
        return int(v)
    except Exception:
        return None


def _list_invoices_internal(
    db: Session,
    block_id: Optional[int] = None,
    resident_id: Optional[int] = None,
    status_val: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    q: Optional[str] = None,
    page: int = 1,
    per_page: int = 25,
):
    """Внутренняя функция для получения списка счетов (без авторизации)."""
    query = db.query(Invoice).join(Resident, Resident.id == Invoice.resident_id).join(Block, Block.id == Resident.block_id)
    
    if block_id:
        query = query.filter(Resident.block_id == block_id)
    if resident_id:
        query = query.filter(Invoice.resident_id == resident_id)
    # Фильтр по статусу:
    # - Поддерживаем реальные статусы InvoiceStatus (PAID, ISSUED, ...)
    # - Также поддерживаем агрегированные фильтры из UI: UNPAID, OVERDUE
    if status_val:
        s = status_val.strip().upper()
        if s in {st.value for st in InvoiceStatus}:
            query = query.filter(Invoice.status == InvoiceStatus(s))
        elif s == "UNPAID":
            # "Не оплачен" = не полностью оплачен и не отменён
            query = query.filter(Invoice.status.in_([InvoiceStatus.DRAFT, InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL]))
        elif s == "OVERDUE":
            # "Просрочен" = due_date < сегодня (Baku) и счёт ещё не закрыт оплатой/отменой
            today = now_baku().date()
            query = query.filter(
                Invoice.due_date.isnot(None),
                Invoice.due_date < today,
                Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL]),
            )
    if year:
        query = query.filter(Invoice.period_year == year)
    if month:
        query = query.filter(Invoice.period_month == month)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Invoice.number.ilike(like), Invoice.notes.ilike(like)))
    
    total = query.count()
    last_page = max(1, (total + per_page - 1) // per_page)
    if page > last_page:
        page = last_page
    
    items = (
        query
        # Показываем самые новые счета первыми
        .order_by(Invoice.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    
    # Получаем сумму оплат по каждому счёту
    ids = [inv.id for inv in items]
    paid_map = {}
    payment_methods_map: dict[int, list[str]] = {}
    if ids:
        rows = (
            db.query(PaymentApplication.invoice_id, func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.invoice_id.in_(ids))
            .group_by(PaymentApplication.invoice_id)
            .all()
        )
        paid_map = {inv_id: float(paid) for inv_id, paid in rows}

        method_rows = (
            db.query(PaymentApplication.invoice_id, Payment.method)
            .join(Payment, Payment.id == PaymentApplication.payment_id)
            .filter(PaymentApplication.invoice_id.in_(ids))
            .all()
        )
        method_sets: dict[int, set[str]] = {}
        for invoice_id, method in method_rows:
            method_key = method.value if isinstance(method, PaymentMethod) else str(method or "").strip()
            if not method_key:
                continue
            method_sets.setdefault(int(invoice_id), set()).add(method_key)
        payment_methods_map = {inv_id: sorted(list(methods)) for inv_id, methods in method_sets.items()}
    
    # Проверяем и исправляем amount_total для каждого счета, если он не совпадает с суммой строк
    needs_commit = False
    for inv in items:
        canonical_number = build_invoice_number(db, inv.resident_id, inv.period_year, inv.period_month)
        if inv.number != canonical_number:
            inv.number = canonical_number
            needs_commit = True

        lines_sum = db.query(
            func.coalesce(func.sum(InvoiceLine.amount_total), 0)
        ).filter(InvoiceLine.invoice_id == inv.id).scalar() or 0
        
        if abs(float(inv.amount_total or 0) - float(lines_sum)) > 0.01:
            inv.amount_total = Decimal(str(lines_sum))
            net_sum = db.query(func.coalesce(func.sum(InvoiceLine.amount_net), 0)).filter(InvoiceLine.invoice_id == inv.id).scalar() or 0
            vat_sum = db.query(func.coalesce(func.sum(InvoiceLine.amount_vat), 0)).filter(InvoiceLine.invoice_id == inv.id).scalar() or 0
            inv.amount_net = Decimal(str(net_sum))
            inv.amount_vat = Decimal(str(vat_sum))
            needs_commit = True
    
    if needs_commit:
        db.commit()
    
    # Получаем блоки и резидентов для формирования данных
    blocks = {b.id: b for b in db.query(Block).all()}
    
    resident_ids = [int(inv.resident_id) for inv in items if inv.resident_id]
    resident_user_names = _resident_user_names_map(db, resident_ids)
    resident_user_phones = _resident_user_phones_map(db, resident_ids)

    result = []
    for inv in items:
        resident = inv.resident
        block = blocks.get(resident.block_id)
        
        result.append({
            "id": inv.id,
            "resident_id": inv.resident_id,
            "resident_code": f"{block.name if block else ''} / {resident.unit_number}" if block else resident.unit_number,
            "resident_info": f"Блок {block.name if block else ''}, №{resident.unit_number}" if block else f"№{resident.unit_number}",
            "block_name": block.name if block else "",
            "unit_number": resident.unit_number,
            "resident_user_full_name": resident_user_names.get(int(inv.resident_id)) if inv.resident_id else None,
            "resident_user_phone": resident_user_phones.get(int(inv.resident_id)) if inv.resident_id else None,
            "number": inv.number,
            "status": inv.status.value,
            "due_date": inv.due_date,
            "notes": inv.notes,
            "period_year": inv.period_year,
            "period_month": inv.period_month,
            "amount_net": float(inv.amount_net),
            "amount_vat": float(inv.amount_vat),
            "amount_total": float(inv.amount_total),
            "paid_amount": paid_map.get(inv.id, 0.0),
            "payment_methods": payment_methods_map.get(inv.id, []),
            "created_at": inv.created_at,
            "lines": [
                {
                    "id": line.id,
                    "description": line.description,
                    "amount_net": float(line.amount_net),
                    "amount_vat": float(line.amount_vat),
                    "amount_total": float(line.amount_total),
                }
                for line in inv.lines
            ],
        })
    
    return {
        "invoices": result,
        "blocks": [{"id": b.id, "name": b.name} for b in blocks.values()],
        "residents": [
            {"id": r.id, "block_name": blocks.get(r.block_id).name if blocks.get(r.block_id) else "", "unit_number": r.unit_number}
            for r in db.query(Resident).all()
        ],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "last_page": last_page,
        },
    }


@router.get("")
def list_invoices_api(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    block_id: Optional[str] = Query(None),
    resident_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    year: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    """Список счетов с фильтрами."""
    block_id_i = _to_int(block_id)
    resident_id_i = _to_int(resident_id)
    year_i = _to_int(year)
    month_i = _to_int(month)
    
    return _list_invoices_internal(
        db, block_id_i, resident_id_i, status, year_i, month_i, q, page, per_page
    )


# Bulk issue models
class BulkIssueRequest(BaseModel):
    action: str  # 'by_block' | 'all'
    block_id: Optional[int] = None
    due_date: Optional[str] = None  # YYYY-MM-DD
    
    @classmethod
    def parse_obj(cls, obj):
        # Ensure block_id is None if it's null in JSON
        if isinstance(obj, dict) and 'block_id' in obj and obj['block_id'] is None:
            obj['block_id'] = None
        return super().parse_obj(obj)


class BulkIssueResponse(BaseModel):
    success: bool
    count: int
    message: str


def _bulk_issue_internal(
    db: Session,
    action: str,
    block_id: Optional[int] = None,
    due_date: Optional[str] = None,
):
    """Внутренняя функция для массового выставления счетов (без авторизации)."""
    if not due_date or not due_date.strip():
        raise HTTPException(status_code=400, detail="Due date is required")

    if action == "by_block" and not block_id:
        raise HTTPException(status_code=400, detail="Block ID is required for 'by_block' action")
    
    if action not in ("by_block", "all"):
        raise HTTPException(status_code=400, detail="Action must be 'by_block' or 'all'")
    
    # Query for DRAFT invoices
    query = db.query(Invoice).join(Resident, Resident.id == Invoice.resident_id)\
                             .filter(Invoice.status == InvoiceStatus.DRAFT)
    
    if action == "by_block":
        if not block_id:
            raise HTTPException(status_code=400, detail="Block ID is required for 'by_block' action")
        query = query.filter(Resident.block_id == block_id)
    
    # Count before processing
    draft_count = query.count()
    
    if draft_count == 0:
        return {
            "success": True,
            "count": 0,
            "message": "Нет черновиков для выставления"
        }
    
    # Parse due_date
    due: Optional[date] = None
    if due_date:
        try:
            due = datetime.strptime(due_date, "%Y-%m-%d").date()
        except Exception:
            due = None
    if due is None:
        raise HTTPException(status_code=400, detail="Invalid due_date format, expected YYYY-MM-DD")
    
    cnt = 0
    invoices_to_process = query.all()
    
    for inv in invoices_to_process:
        # Канонический номер счета для финансовой уникальности
        inv.number = build_invoice_number(db, inv.resident_id, inv.period_year, inv.period_month)
        
        inv.status = InvoiceStatus.ISSUED
        inv.due_date = due
        cnt += 1
    
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to commit changes: {str(e)}")
    
    # Создаем уведомления для всех выставленных счетов
    # Сохраняем ID счетов до commit, затем перезагружаем
    invoice_ids = [inv.id for inv in invoices_to_process]
    db.expire_all()  # Сбрасываем кэш сессии
    
    for invoice_id in invoice_ids:
        inv = db.get(Invoice, invoice_id)
        if inv and inv.status == InvoiceStatus.ISSUED:
            create_invoice_notification(db, inv)
    
    return {
        "success": True,
        "count": cnt,
        "message": f"Выставлено счетов: {cnt}"
    }


@router.post("/bulk-issue", response_model=BulkIssueResponse)
def bulk_issue_api(
    data: BulkIssueRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Массовое выставление счетов."""
    try:
        # Normalize block_id: if it's None or 0, set to None
        block_id = data.block_id if data.block_id else None
        return _bulk_issue_internal(db, data.action, block_id, data.due_date)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


# ====== Bulk Notify existing invoices ======
class BulkNotifyRequest(BaseModel):
    due_date: Optional[str] = None  # YYYY-MM-DD

@router.post("/bulk-notify")
def bulk_notify_api(
    data: BulkNotifyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Рассылка уведомлений по уже выставленным счетам."""
    # Ищем счета, которые уже выставлены или частично оплачены
    # Берем счета за текущий месяц или все открытые? 
    # Обычно уведомляют по открытым (не оплаченным) счетам.
    query = db.query(Invoice).filter(
        Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL])
    )
    
    invoices = query.all()
    if not invoices:
        return {"success": True, "count": 0, "message": "Нет выставленных счетов для уведомления"}

    # Парсим новый due_date если пришел
    due: Optional[date] = None
    if data.due_date:
        try:
            due = datetime.strptime(data.due_date, "%Y-%m-%d").date()
        except:
            due = None

    cnt = 0
    invoice_ids = []
    for inv in invoices:
        if due:
            inv.due_date = due
        invoice_ids.append(inv.id)
        cnt += 1
    
    db.commit()
    db.expire_all()

    # Создаем уведомления
    for invoice_id in invoice_ids:
        inv = db.get(Invoice, invoice_id)
        if inv:
            # create_invoice_notification сама проверяет, есть ли UNREAD уведомление
            from ..utils import create_invoice_notification
            create_invoice_notification(db, inv)

    return {
        "success": True, 
        "count": cnt, 
        "message": f"Уведомления отправлены по {cnt} счетам"
    }


# ====== Get invoice details ======
class PaymentOut(BaseModel):
    id: int
    date: datetime
    method: str
    amount: float
    payment_id: int
    reference: Optional[str] = None
    app_reference: Optional[str] = None
    comment: Optional[str] = None

    class Config:
        from_attributes = True


class InvoiceDetailOut(BaseModel):
    id: int
    resident_id: int
    resident_code: str  # "A / 205"
    resident_user_full_name: Optional[str] = None  # ФИО (или username) жителя (User), привязанного к резиденту
    resident_user_phone: Optional[str] = None
    number: Optional[str] = None
    status: str
    due_date: Optional[date] = None
    notes: Optional[str] = None
    period_year: int
    period_month: int
    period_dates: Optional[dict] = None
    amount_net: float
    amount_vat: float
    amount_total: float
    paid_amount: float = 0.0
    remaining_amount: float = 0.0
    lines: List[InvoiceLineOut] = []
    payments: List[PaymentOut] = []

    class Config:
        from_attributes = True


def _resident_user_names_map(db: Session, resident_ids: list[int]) -> dict[int, str]:
    """
    Возвращает отображаемое имя 'жителя' (User) для каждого resident_id.
    Если привязано несколько пользователей — склеиваем через ", ".
    Имя берём из User.full_name, иначе fallback на User.username.
    """
    if not resident_ids:
        return {}

    rows = (
        db.query(user_residents.c.resident_id, User.full_name, User.username)
        .join(User, User.id == user_residents.c.user_id)
        .filter(user_residents.c.resident_id.in_(resident_ids))
        .filter(User.is_active.is_(True))
        .filter(User.role == RoleEnum.RESIDENT)
        .all()
    )

    acc: dict[int, list[str]] = {}
    for rid, full_name, username in rows:
        name = (full_name or "").strip() or (username or "").strip()
        if not name:
            continue
        rid_i = int(rid)
        acc.setdefault(rid_i, [])
        if name not in acc[rid_i]:
            acc[rid_i].append(name)

    return {rid: ", ".join(names) for rid, names in acc.items()}


def _resident_user_phones_map(db: Session, resident_ids: list[int]) -> dict[int, str]:
    """
    Возвращает телефоны 'жителей' (User) для каждого resident_id.
    Если привязано несколько — склеиваем через ", ".
    """
    if not resident_ids:
        return {}

    rows = (
        db.query(user_residents.c.resident_id, User.phone)
        .join(User, User.id == user_residents.c.user_id)
        .filter(user_residents.c.resident_id.in_(resident_ids))
        .filter(User.is_active.is_(True))
        .filter(User.role == RoleEnum.RESIDENT)
        .all()
    )

    acc: dict[int, list[str]] = {}
    for rid, phone in rows:
        value = (phone or "").strip()
        if not value:
            continue
        rid_i = int(rid)
        acc.setdefault(rid_i, [])
        if value not in acc[rid_i]:
            acc[rid_i].append(value)

    return {rid: ", ".join(values) for rid, values in acc.items()}


def _money2(x: Decimal) -> Decimal:
    # Приводим к 2 знакам после запятой (денежное округление)
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
    """
    Гарантирует наличие синтетической строки "Канализация (авто)" как % от суммы воды.
    Логику держим здесь, чтобы:
    - новые счета работали сразу,
    - старые счета (созданные до внедрения) подтягивали строку при открытии.
    """
    if not inv or not inv.id:
        return

    # Реальные показания/начисления по счёту (только те, что привязаны к meter_reading_id)
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
        # На случай, если раньше применяли "деление" для INDIVIDUAL — восстановим воду из MeterReading
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


def _get_invoice_detail_internal(db: Session, invoice_id: int):
    """Внутренняя функция для получения деталей счета (без авторизации)."""
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    canonical_number = build_invoice_number(db, inv.resident_id, inv.period_year, inv.period_month)
    if inv.number != canonical_number:
        inv.number = canonical_number
        db.commit()
        db.refresh(inv)
    
    resident = inv.resident
    block = resident.block if resident else None
    
    # Автодобавление "Канализация" как % от воды (если настроено)
    _ensure_auto_sewerage_line(db, inv)
    db.flush()

    # Получаем строки счета
    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == inv.id).all()
    
    # Получаем оплаты по счету
    all_apps = (
        db.query(PaymentApplication)
        .join(Payment, Payment.id == PaymentApplication.payment_id)
        .filter(PaymentApplication.invoice_id == inv.id)
        .order_by(PaymentApplication.created_at.asc(), PaymentApplication.id.asc())
        .all()
    )
    
    # ВАЖНО: Фильтруем применения для отображения
    # Если есть применение от платежа с method=ADVANCE (history_payment),
    # то скрываем ВСЕ применения от реальных платежей (CARD) с reference="ADVANCE",
    # чтобы показывать только один платеж ADVANCE вместо множества CARD платежей
    
    # Проверяем, есть ли хотя бы одно применение от ADVANCE платежа
    has_advance_payment = False
    for app in all_apps:
        if app.payment.method == PaymentMethod.ADVANCE:
            has_advance_payment = True
            break
    
    # Фильтруем применения
    apps = []
    for app in all_apps:
        # Если есть применение от ADVANCE платежа (history_payment),
        # скрываем ВСЕ применения от реальных платежей (CARD, TRANSFER и т.д.) с reference="ADVANCE"
        if has_advance_payment:
            # Скрываем применения от реальных платежей с reference="ADVANCE"
            # (они создаются apply_advance_with_limit, но мы показываем только history_payment)
            if app.reference == "ADVANCE" and app.payment.method != PaymentMethod.ADVANCE:
                continue  # Пропускаем - скрываем применение от реального платежа
        apps.append(app)
    
    # Пересчитываем amount_total из строк счета на случай, если он устарел
    lines_sum = db.query(
        func.coalesce(func.sum(InvoiceLine.amount_total), 0)
    ).filter(InvoiceLine.invoice_id == inv.id).scalar() or 0
    
    # Обновляем amount_total в счете, если он отличается от суммы строк
    if abs(float(inv.amount_total or 0) - float(lines_sum)) > 0.01:
        inv.amount_total = Decimal(str(lines_sum))
        # Также пересчитываем net и vat
        net_sum = db.query(func.coalesce(func.sum(InvoiceLine.amount_net), 0)).filter(InvoiceLine.invoice_id == inv.id).scalar() or 0
        vat_sum = db.query(func.coalesce(func.sum(InvoiceLine.amount_vat), 0)).filter(InvoiceLine.invoice_id == inv.id).scalar() or 0
        inv.amount_net = Decimal(str(net_sum))
        inv.amount_vat = Decimal(str(vat_sum))
        db.commit()
    
    # Считаем paid_total по ВСЕМ применениям (включая скрытые), для правильного расчета остатка
    paid_total = db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))\
                   .filter(PaymentApplication.invoice_id == inv.id).scalar() or 0
    remaining = float(inv.amount_total or 0) - float(paid_total)
    
    payments = []
    for app in apps:
        p = app.payment
        
        # ВАЖНО: Если платеж с method=ADVANCE, это всегда списание из аванса (history_payment)
        if p.method == PaymentMethod.ADVANCE:
            display_method = "ADVANCE"
            # Используем comment из платежа, если он есть, иначе стандартный
            display_comment = p.comment or "Royal Park Pass"
        elif (app.reference == "ADVANCE") or (app.reference and app.reference.startswith(("ADVANCE:", "AUTOADV:"))):
            # Применение от реального платежа с reference="ADVANCE"/"ADVANCE:<id>"/"AUTOADV:<...>"
            display_method = "ADVANCE"
            display_comment = "Royal Park Pass"
        else:
            # Обычное применение платежа
            display_method = p.method.value
            display_comment = p.comment or "—"
            
        received_at = to_baku_datetime(app.created_at or p.created_at or p.received_at)

        payment_data = {
            "id": app.id,
            "date": received_at,
            "method": display_method,
            "amount": float(app.amount_applied),
            "payment_id": p.id,
            "reference": p.reference,
            "app_reference": app.reference,
            "comment": display_comment,
        }
        payments.append(payment_data)

    # Period date range (from previous reading date to current reading date), like user panel
    period_dates = None
    if lines:
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
            period_dates = {"from": start_str, "to": end_str}

    # Align admin invoice detail/print period with bills list chaining:
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

    resident_user_names = _resident_user_names_map(db, [int(inv.resident_id)]) if inv.resident_id else {}
    resident_user_phones = _resident_user_phones_map(db, [int(inv.resident_id)]) if inv.resident_id else {}

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

    # Распределяем оплату по строкам с учётом выбранных строк (LINESEL)
    # Используем все применения (all_apps), а не отфильтрованные apps для отображения.
    line_payment_map = _build_invoice_line_payment_map(lines, all_apps)
    
    # Разделяем стабильный тариф в отдельную строку, как в user panel
    reading_ids = [line.meter_reading_id for line in lines if line.meter_reading_id]
    reading_map = {}
    if reading_ids:
        readings = db.query(MeterReading).filter(MeterReading.id.in_(reading_ids)).all()
        reading_map = {rd.id: rd for rd in readings}

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
        meter_previous = None
        meter_current = None
        meter_unit = None
        tariff_name = None
        tariff_from_date = None
        tariff_to_date = None
        if rd is not None:
            try:
                current_val_dec = Decimal(str(getattr(rd, "value", 0) or 0))
                consumption_dec = Decimal(str(getattr(rd, "consumption", 0) or 0))
                previous_val_dec = current_val_dec - consumption_dec
                meter_current = float(current_val_dec)
                meter_previous = float(previous_val_dec)
            except Exception:
                meter_previous = None
                meter_current = None

            meter_type_val = str(getattr(getattr(rd, "resident_meter", None), "meter_type", "") or "").upper()
            if meter_type_val == "ELECTRIC":
                meter_unit = "кВт·ч"
            elif meter_type_val in {"GAS", "WATER", "SEWERAGE"}:
                meter_unit = "м³"
            tariff_name = str(getattr(getattr(rd, "tariff", None), "name", "") or "").strip() or None

            try:
                tariff_obj = getattr(rd, "tariff", None)
                rd_date = getattr(rd, "reading_date", None)
                rd_day = rd_date.date() if hasattr(rd_date, "date") else None
                tariff_steps = list(getattr(tariff_obj, "steps", []) or [])
                dated_steps = [st for st in tariff_steps if getattr(st, "from_date", None) or getattr(st, "to_date", None)]
                selected_step = None
                if rd_day and dated_steps:
                    for st in dated_steps:
                        fd = getattr(st, "from_date", None)
                        td = getattr(st, "to_date", None)
                        if (fd is None or rd_day >= fd) and (td is None or rd_day <= td):
                            selected_step = st
                            break
                if selected_step is None and dated_steps:
                    selected_step = dated_steps[0]
                if selected_step is not None:
                    fd = getattr(selected_step, "from_date", None)
                    td = getattr(selected_step, "to_date", None)
                    tariff_from_date = fd.strftime("%d.%m.%Y") if fd else None
                    tariff_to_date = td.strftime("%d.%m.%Y") if td else None
            except Exception:
                tariff_from_date = None
                tariff_to_date = None

        base_line["meter_previous"] = meter_previous
        base_line["meter_current"] = meter_current
        base_line["meter_unit"] = meter_unit
        base_line["tariff_name"] = tariff_name
        base_line["tariff_from_date"] = tariff_from_date
        base_line["tariff_to_date"] = tariff_to_date
        try:
            stable_fee_total = _money2(Decimal(str(getattr(rd, "stable_fee_total", 0) or 0)))
        except Exception:
            stable_fee_total = Decimal("0")

        if stable_fee_total > 0:
            line_net = Decimal(str(line.amount_net or 0))
            line_vat_total = Decimal(str(line.amount_vat or 0))
            line_total = _money2(Decimal(str(line.amount_total or 0)))
            stable_fee_total = min(stable_fee_total, line_total)
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

            if line_net >= stable_fee_net:
                # Получаем статус оплаты для основной строки
                line_payment = line_payment_map.get(line.id, {"paid": Decimal("0"), "remaining": Decimal("0"), "status": "Не оплачена"})
                
                # Распределяем оплату между переменной частью и стабильным тарифом пропорционально
                variable_total_dec = variable_total
                stable_total_dec = stable_fee_total
                total_for_split = variable_total_dec + stable_total_dec
                
                if total_for_split > 0 and line_payment["paid"] > 0:
                    # Пропорциональное распределение оплаты
                    variable_paid = _money2(line_payment["paid"] * variable_total_dec / total_for_split)
                    stable_paid = _money2(line_payment["paid"] * stable_total_dec / total_for_split)
                    
                    variable_remaining = max(variable_total_dec - variable_paid, Decimal("0"))
                    stable_remaining = max(stable_total_dec - stable_paid, Decimal("0"))
                    
                    variable_status = "Оплачена" if variable_remaining <= Decimal("0.0001") else ("Частично" if variable_paid > Decimal("0.0001") else "Не оплачена")
                    stable_status = "Оплачена" if stable_remaining <= Decimal("0.0001") else ("Частично" if stable_paid > Decimal("0.0001") else "Не оплачена")
                else:
                    variable_paid = Decimal("0")
                    stable_paid = Decimal("0")
                    variable_remaining = variable_total_dec
                    stable_remaining = stable_total_dec
                    variable_status = "Не оплачена"
                    stable_status = "Не оплачена"
                
                lines_out.append({
                    "id": line.id,
                    "description": line.description,
                    "amount_net": float(variable_net),
                    "amount_vat": float(variable_vat),
                    "amount_total": float(variable_total),
                    "payment_status": variable_status,
                    "paid_amount": float(variable_paid),
                    "remaining_amount": float(variable_remaining),
                    "meter_previous": meter_previous,
                    "meter_current": meter_current,
                    "meter_unit": meter_unit,
                    "tariff_name": tariff_name,
                    "tariff_from_date": tariff_from_date,
                    "tariff_to_date": tariff_to_date,
                })
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
                    "remaining_amount": float(stable_remaining),
                    "tariff_name": tariff_name,
                })
                continue

        # Получаем статус оплаты для строки
        line_payment = line_payment_map.get(line.id, {"paid": Decimal("0"), "remaining": Decimal("0"), "status": "Не оплачена"})
        base_line["payment_status"] = line_payment["status"]
        base_line["paid_amount"] = float(line_payment["paid"])
        base_line["remaining_amount"] = float(line_payment["remaining"])
        lines_out.append(base_line)
    
    return {
        "id": inv.id,
        "resident_id": inv.resident_id,
        "resident_code": f"{block.name if block else ''} / {resident.unit_number}" if block else resident.unit_number,
        "resident_user_full_name": resident_user_names.get(int(inv.resident_id)) if inv.resident_id else None,
        "resident_user_phone": resident_user_phones.get(int(inv.resident_id)) if inv.resident_id else None,
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


@router.get("/{invoice_id}", response_model=InvoiceDetailOut)
def get_invoice_detail_api(
    invoice_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Получение деталей счета."""
    return _get_invoice_detail_internal(db, invoice_id)


# ====== Update invoice ======
class InvoiceUpdateRequest(BaseModel):
    due_date: Optional[str] = None  # YYYY-MM-DD
    notes: Optional[str] = None


@router.put("/{invoice_id}")
def update_invoice_api(
    invoice_id: int,
    data: InvoiceUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Обновление счета."""
    return _update_invoice_internal(db, invoice_id, data.due_date, data.notes)


def _update_invoice_internal(db: Session, invoice_id: int, due_date: Optional[str], notes: Optional[str]):
    """Внутренняя функция для обновления счета (без авторизации)."""
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    
    if inv.status in (InvoiceStatus.PAID, InvoiceStatus.CANCELED):
        raise HTTPException(status_code=400, detail="Cannot update PAID or CANCELED invoice")
    
    if notes is not None:
        inv.notes = notes.strip() if notes else None
    
    # due_date: при пустой строке или null — очищаем срок оплаты; иначе парсим дату
    if due_date is not None and due_date.strip():
        try:
            inv.due_date = datetime.strptime(due_date.strip(), "%Y-%m-%d").date()
        except Exception as e:
            print(f"Warning: Failed to parse due_date '{due_date}': {e}")
            inv.due_date = None
    else:
        # Явная очистка: клиент отправил null или пустое поле — удаляем срок оплаты
        inv.due_date = None
    
    # Если статус DRAFT и есть due_date, можно автоматически выставить счет
    if inv.status == InvoiceStatus.DRAFT and inv.due_date:
        # Канонический номер счета для финансовой уникальности
        inv.number = build_invoice_number(db, inv.resident_id, inv.period_year, inv.period_month)
        inv.status = InvoiceStatus.ISSUED
        db.commit()
        db.refresh(inv)
        # Создаем уведомления для пользователей
        create_invoice_notification(db, inv)
    else:
        db.commit()
        db.refresh(inv)
        # Если счет уже был ISSUED и мы обновили due_date, также создаем уведомления
        if inv.status == InvoiceStatus.ISSUED and inv.due_date:
            create_invoice_notification(db, inv)
    
    return {"success": True, "message": "Счёт успешно обновлён!"}


# ====== Cancel invoice ======
class InvoiceCancelRequest(BaseModel):
    reason: str


@router.post("/{invoice_id}/cancel")
def cancel_invoice_api(
    invoice_id: int,
    data: InvoiceCancelRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Отмена счета."""
    return _cancel_invoice_internal(db, invoice_id, data.reason)


def _cancel_invoice_internal(db: Session, invoice_id: int, reason: str):
    """Внутренняя функция для отмены счета (без авторизации)."""
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    
    if inv.status not in (InvoiceStatus.DRAFT, InvoiceStatus.ISSUED):
        raise HTTPException(status_code=400, detail="Can only cancel DRAFT or ISSUED invoices")
    
    if not reason or not reason.strip():
        raise HTTPException(status_code=400, detail="Reason is required")
    
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    audit_line = f"[CANCELED {stamp}] {reason.strip()}"
    inv.notes = f"{inv.notes}\n{audit_line}" if inv.notes else audit_line
    inv.status = InvoiceStatus.CANCELED
    
    db.commit()
    return {"success": True, "message": "Счёт отменён!"}


# ====== Reissue invoice ======
class InvoiceReissueRequest(BaseModel):
    due_date: Optional[str] = None  # YYYY-MM-DD
    comment: Optional[str] = None


@router.post("/{invoice_id}/reissue")
def reissue_invoice_api(
    invoice_id: int,
    data: InvoiceReissueRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Выставить счет заново (только для CANCELED)."""
    return _reissue_invoice_internal(db, invoice_id, data.due_date, data.comment)


def _reissue_invoice_internal(db: Session, invoice_id: int, due_date: Optional[str], comment: Optional[str]):
    """Внутренняя функция для повторного выставления счета (без авторизации)."""
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    
    if inv.status != InvoiceStatus.CANCELED:
        raise HTTPException(status_code=400, detail="Can only reissue CANCELED invoices")
    
    # Проверяем, нет ли уже активного счета для этого резидента за тот же период
    exists_active = db.query(Invoice.id).filter(
        Invoice.resident_id == inv.resident_id,
        Invoice.period_year == inv.period_year,
        Invoice.period_month == inv.period_month,
        Invoice.status != InvoiceStatus.CANCELED,
        Invoice.id != inv.id,
    ).first()
    if exists_active:
        raise HTTPException(status_code=400, detail="Active invoice already exists for this period")
    
    # Обновляем due_date если указан
    if due_date and due_date.strip():
        try:
            inv.due_date = datetime.strptime(due_date.strip(), "%Y-%m-%d").date()
        except Exception as e:
            print(f"Warning: Failed to parse due_date '{due_date}': {e}")
    
    # Канонический номер счета для финансовой уникальности
    inv.number = build_invoice_number(db, inv.resident_id, inv.period_year, inv.period_month)
    
    # Добавляем запись в примечания
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    line = f"[REISSUED {stamp}] {comment.strip()}" if comment and comment.strip() else f"[REISSUED {stamp}]"
    inv.notes = f"{inv.notes}\n{line}" if inv.notes else line
    
    # Меняем статус на ISSUED
    inv.status = InvoiceStatus.ISSUED
    
    db.commit()
    db.refresh(inv)
    # Создаем уведомления для пользователей
    create_invoice_notification(db, inv)
    
    return {"success": True, "message": "Счёт выставлен заново!"}

