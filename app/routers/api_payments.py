from typing import List, Optional
from datetime import datetime, date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Form
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from ..database import get_db
from ..models import (
    User, RoleEnum, Block, Resident,
    Payment, PaymentApplication, PaymentMethod,
    Invoice, InvoiceStatus, PaymentLog, user_residents
)
from ..deps import get_current_user
from ..utils import now_baku, to_baku_datetime
from ..utils import build_invoice_number
from .api_payment_logic import auto_apply_advance, _recompute_invoice_status, _to_int


router = APIRouter(prefix="/api/payments", tags=["payments-api"])


# Pydantic models
class PaymentApplicationOut(BaseModel):
    id: int
    invoice_id: int
    invoice_number: Optional[str] = None
    invoice_period: Optional[str] = None
    amount_applied: float

    class Config:
        from_attributes = True


class PaymentOut(BaseModel):
    id: int
    resident_id: int
    resident_code: str  # "A / 205"
    resident_info: str  # "Блок A, №205"
    resident_user_full_name: Optional[str] = None  # ФИО(или username) жителя (User), привязанного к этому резиденту
    block_name: str
    unit_number: str
    received_at: datetime
    amount_total: float
    method: str
    reference: Optional[str] = None
    comment: Optional[str] = None
    created_at: datetime
    applied_total: float = 0.0
    leftover: float = 0.0
    applications: List[PaymentApplicationOut] = []

    class Config:
        from_attributes = True


class PaymentCreate(BaseModel):
    resident_id: int
    received_at: Optional[datetime] = None  # игнорируем и ставим системное время
    amount_total: float
    method: str
    reference: Optional[str] = None
    comment: Optional[str] = None


class InvoiceForDistribution(BaseModel):
    id: int
    number: Optional[str] = None
    period: str  # "YYYY-MM"
    amount_total: float
    paid_amount: float
    left_to_pay: float
    due_date: Optional[date] = None


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
        acc.setdefault(int(rid), [])
        if name not in acc[int(rid)]:
            acc[int(rid)].append(name)

    return {rid: ", ".join(names) for rid, names in acc.items()}


def _build_payment_applications(db: Session, p: Payment) -> list[dict]:
    # Если у платежа есть реальные применения — отдаём их
    if p.applications:
        return [
            {
                "id": app.id,
                "invoice_id": app.invoice_id,
                "invoice_number": (
                    build_invoice_number(db, app.invoice.resident_id, app.invoice.period_year, app.invoice.period_month)
                    if app.invoice else None
                ),
                "invoice_period": f"{app.invoice.period_year}-{app.invoice.period_month:02d}" if app.invoice else None,
                "amount_applied": float(app.amount_applied),
            }
            for app in p.applications
        ]

    # Для ADVANCE списаний по конкретному счёту — показываем синтетическое применение
    if p.method == PaymentMethod.ADVANCE:
        # Группируем применения с reference="ADVANCE:<payment_id>"
        ref_tag = f"ADVANCE:{p.id}"
        rows = (
            db.query(
                Invoice.id,
                Invoice.number,
                Invoice.resident_id,
                Invoice.period_year,
                Invoice.period_month,
                func.coalesce(func.sum(PaymentApplication.amount_applied), 0)
            )
            .join(PaymentApplication, PaymentApplication.invoice_id == Invoice.id)
            # Поддерживаем как точный тег "ADVANCE:<id>", так и расширенный
            # формат с дополнительными метками (например, "|LINESEL:...").
            .filter(PaymentApplication.reference.like(f"{ref_tag}%"))
            .group_by(Invoice.id, Invoice.number, Invoice.resident_id, Invoice.period_year, Invoice.period_month)
            .all()
        )
        if rows:
            return [
                {
                    "id": 0,
                    "invoice_id": inv_id,
                    "invoice_number": build_invoice_number(db, resident_id, inv_year, inv_month) if inv_id else inv_number,
                    "invoice_period": f"{inv_year}-{inv_month:02d}",
                    "amount_applied": float(amount_applied),
                }
                for (inv_id, inv_number, resident_id, inv_year, inv_month, amount_applied) in rows
            ]

    return []


def _is_owner_level_advance_topup(p: Payment) -> bool:
    """
    Определяет платежи пополнения общего аванса из личного кабинета пользователя.
    Такие платежи создаются как обычные CARD/ONLINE записи, но:
    - без ручного created_by_id (self-service),
    - с положительным свободным остатком,
    - без применений к счетам.
    """
    if p.method == PaymentMethod.ADVANCE:
        return False
    if p.created_by_id is not None:
        return False
    if p.applications:
        return False
    return Decimal(p.leftover or 0) > 0


def _owner_resident_codes(db: Session, resident_id: int) -> str | None:
    """
    Возвращает список кодов всех домов владельца для заданного resident_id:
    например, "L / 55, L / 66".
    """
    linked_users = (
        db.query(user_residents.c.user_id)
        .filter(user_residents.c.resident_id == resident_id)
        .subquery()
    )
    resident_ids = [
        rid for (rid,) in (
            db.query(user_residents.c.resident_id)
            .filter(user_residents.c.user_id.in_(linked_users))
            .distinct()
            .all()
        )
    ]
    if not resident_ids:
        return None

    residents_rows = (
        db.query(Resident.id, Resident.unit_number, Block.name)
        .outerjoin(Block, Block.id == Resident.block_id)
        .filter(Resident.id.in_(resident_ids))
        .all()
    )
    if not residents_rows:
        return None

    def _sort_key(row):
        _, unit, block_name = row
        return ((block_name or ""), str(unit or ""))

    codes = []
    for _, unit, block_name in sorted(residents_rows, key=_sort_key):
        if block_name:
            codes.append(f"{block_name} / {unit}")
        else:
            codes.append(str(unit))

    # Если дом только один, не меняем стандартное отображение.
    if len(codes) <= 1:
        return None
    return ", ".join(codes)


def _list_payments_internal(
    db: Session,
    resident_id: Optional[int] = None,
    method: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    q: Optional[str] = None,
    page: int = 1,
    per_page: int = 25,
):
    """Внутренняя функция для получения списка платежей (без авторизации)."""
    query = db.query(Payment).join(Resident, Resident.id == Payment.resident_id)
    
    if resident_id:
        query = query.filter(Payment.resident_id == resident_id)
    if method and method in {m.value for m in PaymentMethod}:
        query = query.filter(Payment.method == PaymentMethod(method))
    if date_from:
        query = query.filter(Payment.received_at >= date_from)
    if date_to:
        query = query.filter(Payment.received_at <= date_to)
    
    # ВАЖНО: НЕ исключаем ADVANCE из списка, чтобы админ видел эти записи
    # Фильтр ADVANCE применяется только в расчетах балансов (dashboard, графики)
    # Но в списке платежей админ должен видеть все операции, включая списания из аванса

    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Payment.reference.ilike(like), Payment.comment.ilike(like)))
    
    total = query.count()
    last_page = max(1, (total + per_page - 1) // per_page)
    if page > last_page:
        page = last_page
    
    items = (
        query
        .order_by(Payment.received_at.desc(), Payment.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    
    # Получаем блоки для формирования данных
    blocks = {b.id: b for b in db.query(Block).all()}

    # Маппинг resident_id -> "ФИО жителя"
    resident_ids = sorted({int(p.resident_id) for p in items if p.resident_id})
    resident_user_names = _resident_user_names_map(db, resident_ids)
    
    result = []
    for p in items:
        resident = p.resident
        block = blocks.get(resident.block_id)
        resident_code = f"{block.name if block else ''} / {resident.unit_number}" if block else resident.unit_number
        
        applied_total = float(p.applied_total)
        leftover = float(p.leftover)
        if p.method == PaymentMethod.ADVANCE:
            # ADVANCE записи — это списания из аванса, у них остатка быть не должно
            applied_total = float(p.amount_total or 0)
            leftover = 0.0
        elif _is_owner_level_advance_topup(p):
            owner_codes = _owner_resident_codes(db, int(p.resident_id))
            if owner_codes:
                resident_code = owner_codes
        
        result.append({
            "id": p.id,
            "resident_id": p.resident_id,
            "resident_code": resident_code,
            "resident_info": f"Блок {block.name if block else ''}, №{resident.unit_number}" if block else f"№{resident.unit_number}",
            "resident_user_full_name": resident_user_names.get(int(p.resident_id)) if p.resident_id else None,
            "block_name": block.name if block else "",
            "unit_number": resident.unit_number,
            "received_at": to_baku_datetime(p.received_at),
            "amount_total": float(p.amount_total),
            "method": p.method.value,
            "reference": p.reference,
            "comment": p.comment,
            "created_at": p.created_at,
            "applied_total": applied_total,
            "leftover": leftover,
            "applications": _build_payment_applications(db, p)
        })
    
    return {
        "items": result,
        "total": total,
        "page": page,
        "per_page": per_page,
        "last_page": last_page,
    }


@router.get("/")
def list_payments_api(
    resident_id: Optional[str] = Query(None),
    method: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Получить список платежей."""
    resident_id_i = _to_int(resident_id)
    
    date_from_d = None
    if date_from:
        try:
            date_from_d = datetime.strptime(date_from, "%Y-%m-%d").date()
        except Exception:
            pass
    
    date_to_d = None
    if date_to:
        try:
            date_to_d = datetime.strptime(date_to, "%Y-%m-%d").date()
        except Exception:
            pass
    
    return _list_payments_internal(
        db=db,
        resident_id=resident_id_i,
        method=method,
        date_from=date_from_d,
        date_to=date_to_d,
        q=q,
        page=page,
        per_page=per_page,
    )


@router.get("/{payment_id}")
def get_payment_api(
    payment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Получить детали платежа."""
    p = db.get(Payment, payment_id)
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    resident = p.resident
    block = db.get(Block, resident.block_id) if resident else None
    
    applied_total = float(p.applied_total)
    leftover = float(p.leftover)
    resident_code = f"{block.name if block else ''} / {resident.unit_number}" if block else resident.unit_number
    if p.method == PaymentMethod.ADVANCE:
        applied_total = float(p.amount_total or 0)
        leftover = 0.0
    elif _is_owner_level_advance_topup(p):
        owner_codes = _owner_resident_codes(db, int(p.resident_id))
        if owner_codes:
            resident_code = owner_codes

    resident_user_names = _resident_user_names_map(db, [int(p.resident_id)]) if p.resident_id else {}
    
    return {
        "id": p.id,
        "resident_id": p.resident_id,
        "resident_code": resident_code,
        "resident_info": f"Блок {block.name if block else ''}, №{resident.unit_number}" if block else f"№{resident.unit_number}",
        "resident_user_full_name": resident_user_names.get(int(p.resident_id)) if p.resident_id else None,
        "block_name": block.name if block else "",
        "unit_number": resident.unit_number,
        "received_at": to_baku_datetime(p.received_at),
        "amount_total": float(p.amount_total),
        "method": p.method.value,
        "reference": p.reference,
        "comment": p.comment,
        "created_at": p.created_at,
        "applied_total": applied_total,
        "leftover": leftover,
        "applications": _build_payment_applications(db, p)
    }


@router.post("/")
def create_payment_api(
    payment: PaymentCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Создать платеж."""
    if payment.method not in {m.value for m in PaymentMethod}:
        raise HTTPException(status_code=400, detail="Invalid payment method")
    
    resident = db.get(Resident, payment.resident_id)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")
    
    p = Payment(
        resident_id=payment.resident_id,
        received_at=now_baku(),
        amount_total=Decimal(str(payment.amount_total)),
        method=PaymentMethod(payment.method),
        reference=payment.reference or None,
        comment=payment.comment or None,
        created_by_id=user.id,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    
    # ЛОГИРОВАНИЕ
    db.add(PaymentLog(
        payment_id=p.id,
        resident_id=p.resident_id,
        user_id=user.id,
        action="CREATE",
        amount=float(p.amount_total),
        details=f"Ручное создание платежа администратором (метод: {p.method.value})"
    ))

    # НЕ ПРИМЕНЯЕМ АВТОМАТИЧЕСКИ (по просьбе пользователя для прозрачности)
    # auto_apply_advance(db, payment.resident_id)
    
    db.commit()
    db.refresh(p)
    
    return {"id": p.id, "ok": True}


@router.post("/public")
def create_payment_public(
    payment: PaymentCreate,
    db: Session = Depends(get_db),
):
    """Создать платеж (публичный endpoint)."""
    if payment.method not in {m.value for m in PaymentMethod}:
        raise HTTPException(status_code=400, detail="Invalid payment method")
    
    resident = db.get(Resident, payment.resident_id)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")
    
    p = Payment(
        resident_id=payment.resident_id,
        received_at=now_baku(),
        amount_total=Decimal(str(payment.amount_total)),
        method=PaymentMethod(payment.method),
        reference=payment.reference or None,
        comment=payment.comment or None,
        created_by_id=None,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    
    # ЛОГИРОВАНИЕ
    db.add(PaymentLog(
        payment_id=p.id,
        resident_id=p.resident_id,
        user_id=None,
        action="CREATE",
        amount=float(p.amount_total),
        details=f"Публичное создание платежа (метод: {p.method.value})"
    ))

    # НЕ ПРИМЕНЯЕМ АВТОМАТИЧЕСКИ
    # auto_apply_advance(db, payment.resident_id)
    
    db.commit()
    db.refresh(p)
    
    return {"id": p.id, "ok": True}


@router.get("/{payment_id}/open-invoices")
def get_open_invoices_for_payment(
    payment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Получить открытые счета для распределения платежа."""
    p = db.get(Payment, payment_id)
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    # Находим всех резидентов того же пользователя(ей)
    resident_ids = db.query(user_residents.c.resident_id).filter(
        user_residents.c.user_id.in_(
            db.query(user_residents.c.user_id).filter(user_residents.c.resident_id == p.resident_id)
        )
    ).all()
    all_resident_ids = [r[0] for r in resident_ids]
    if not all_resident_ids:
        all_resident_ids = [p.resident_id]

    # Открытые счета ВСЕХ связанных резидентов (ISSUED/PARTIAL), FIFO по периоду
    open_invoices = (
        db.query(Invoice)
        .filter(
            Invoice.resident_id.in_(all_resident_ids),
            Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL])
        )
        .order_by(Invoice.period_year.asc(), Invoice.period_month.asc(), Invoice.id.asc())
        .all()
    )
    
    # Остаток к оплате по счету = total - paid
    def invoice_left(inv: Invoice) -> Decimal:
        paid = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.invoice_id == inv.id)
            .scalar() or Decimal("0")
        )
        return Decimal(inv.amount_total or 0) - paid
    
    result = []
    for inv in open_invoices:
        canonical_number = build_invoice_number(db, inv.resident_id, inv.period_year, inv.period_month)
        if inv.number != canonical_number:
            inv.number = canonical_number
        left = invoice_left(inv)
        paid = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.invoice_id == inv.id)
            .scalar() or Decimal("0")
        )
        # Исключаем счета с остатком <= 0 (полностью оплаченные или переплаченные)
        if left > Decimal("0"):
            result.append({
                "id": inv.id,
                "number": canonical_number,
                "period": f"{inv.period_year}-{inv.period_month:02d}",
                "amount_total": float(inv.amount_total),
                "paid_amount": float(Decimal(inv.amount_total or 0) - left),
                "left_to_pay": float(left),
                "due_date": inv.due_date,
            })
    db.commit()
    return {"invoices": result}


@router.get("/{payment_id}/open-invoices/public")
def get_open_invoices_for_payment_public(
    payment_id: int,
    db: Session = Depends(get_db),
):
    """Получить открытые счета для распределения платежа (публичный endpoint)."""
    p = db.get(Payment, payment_id)
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    # Находим всех резидентов того же пользователя(ей)
    resident_ids = db.query(user_residents.c.resident_id).filter(
        user_residents.c.user_id.in_(
            db.query(user_residents.c.user_id).filter(user_residents.c.resident_id == p.resident_id)
        )
    ).all()
    all_resident_ids = [r[0] for r in resident_ids]
    if not all_resident_ids:
        all_resident_ids = [p.resident_id]

    # Открытые счета ВСЕХ связанных резидентов (ISSUED/PARTIAL), FIFO по периоду
    open_invoices = (
        db.query(Invoice)
        .filter(
            Invoice.resident_id.in_(all_resident_ids),
            Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL])
        )
        .order_by(Invoice.period_year.asc(), Invoice.period_month.asc(), Invoice.id.asc())
        .all()
    )
    
    # Остаток к оплате по счету = total - paid
    def invoice_left(inv: Invoice) -> Decimal:
        paid = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.invoice_id == inv.id)
            .scalar() or Decimal("0")
        )
        return Decimal(inv.amount_total or 0) - paid
    
    result = []
    for inv in open_invoices:
        canonical_number = build_invoice_number(db, inv.resident_id, inv.period_year, inv.period_month)
        if inv.number != canonical_number:
            inv.number = canonical_number
        left = invoice_left(inv)
        paid = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.invoice_id == inv.id)
            .scalar() or Decimal("0")
        )
        # Исключаем счета с остатком <= 0 (полностью оплаченные или переплаченные)
        if left > Decimal("0"):
            result.append({
                "id": inv.id,
                "number": canonical_number,
                "period": f"{inv.period_year}-{inv.period_month:02d}",
                "amount_total": float(inv.amount_total),
                "paid_amount": float(Decimal(inv.amount_total or 0) - left),
                "left_to_pay": float(left),
                "due_date": inv.due_date,
            })
    db.commit()
    return {"invoices": result}


@router.get("/{payment_id}/advance-balance")
def get_advance_balance_for_payment(
    payment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Получить общий доступный аванс для резидента платежа."""
    p = db.get(Payment, payment_id)
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")

    # Находим всех резидентов того же пользователя(ей)
    resident_ids = db.query(user_residents.c.resident_id).filter(
        user_residents.c.user_id.in_(
            db.query(user_residents.c.user_id).filter(user_residents.c.resident_id == p.resident_id)
        )
    ).all()
    all_resident_ids = [r[0] for r in resident_ids]
    if not all_resident_ids:
        all_resident_ids = [p.resident_id]

    payments = (
        db.query(Payment)
        .filter(
            Payment.resident_id.in_(all_resident_ids),
            Payment.method != PaymentMethod.ADVANCE
        )
        .all()
    )

    total_advance = Decimal("0")
    for pay in payments:
        applied = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.payment_id == pay.id)
            .scalar() or Decimal("0")
        )
        total_advance += Decimal(pay.amount_total or 0) - Decimal(applied)

    if total_advance < 0:
        total_advance = Decimal("0")

    return {"advance_balance": float(total_advance)}


@router.post("/{payment_id}/auto-apply-advance")
def auto_apply_advance_for_payment(
    payment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Автоматически распределить аванс по счетам резидента платежа."""
    p = db.get(Payment, payment_id)
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")

    affected_count, total_applied = auto_apply_advance(db, p.resident_id)
    db.commit()

    return {
        "ok": True,
        "affected_count": affected_count,
        "applied_amount": float(total_applied),
    }


@router.post("/{payment_id}/auto-apply")
def auto_apply_payment(
    payment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Автоматически распределить остаток платежа по открытым счетам."""
    p = db.get(Payment, payment_id)
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    # Вычисляем остаток платежа
    leftover = Decimal(p.amount_total or 0) - (
        db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
          .filter(PaymentApplication.payment_id == p.id).scalar() or Decimal("0")
    )
    plan: list[dict] = []
    if leftover <= 0:
        return {"ok": True, "plan": plan, "left_after": float(leftover)}
    
    # Находим всех резидентов того же пользователя(ей)
    resident_ids = db.query(user_residents.c.resident_id).filter(
        user_residents.c.user_id.in_(
            db.query(user_residents.c.user_id).filter(user_residents.c.resident_id == p.resident_id)
        )
    ).all()
    all_resident_ids = [r[0] for r in resident_ids]
    if not all_resident_ids:
        all_resident_ids = [p.resident_id]

    # Получаем открытые счета ВСЕХ связанных резидентов
    open_invoices = (
        db.query(Invoice)
        .filter(
            Invoice.resident_id.in_(all_resident_ids),
            Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL])
        )
        .order_by(Invoice.period_year.asc(), Invoice.period_month.asc(), Invoice.id.asc())
        .all()
    )
    
    for inv in open_invoices:
        paid = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.invoice_id == inv.id)
            .scalar() or Decimal("0")
        )
        left = Decimal(inv.amount_total or 0) - paid
        if left <= 0:
            continue
        apply_amt = min(leftover, left)
        if apply_amt > 0:
            plan.append({"invoice_id": inv.id, "amount": float(apply_amt)})
            leftover -= apply_amt
            if leftover <= 0:
                break
    
    return {"ok": True, "plan": plan, "left_after": float(leftover)}


@router.post("/{payment_id}/auto-apply/public")
def auto_apply_payment_public(
    payment_id: int,
    db: Session = Depends(get_db),
):
    """Автоматически распределить остаток платежа по открытым счетам (публичный endpoint)."""
    p = db.get(Payment, payment_id)
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    # Вычисляем остаток платежа
    leftover = Decimal(p.amount_total or 0) - (
        db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
          .filter(PaymentApplication.payment_id == p.id).scalar() or Decimal("0")
    )
    plan: list[dict] = []
    if leftover <= 0:
        return {"ok": True, "plan": plan, "left_after": float(leftover)}
    
    # Находим всех резидентов того же пользователя(ей)
    resident_ids = db.query(user_residents.c.resident_id).filter(
        user_residents.c.user_id.in_(
            db.query(user_residents.c.user_id).filter(user_residents.c.resident_id == p.resident_id)
        )
    ).all()
    all_resident_ids = [r[0] for r in resident_ids]
    if not all_resident_ids:
        all_resident_ids = [p.resident_id]

    # Получаем открытые счета ВСЕХ связанных резидентов
    open_invoices = (
        db.query(Invoice)
        .filter(
            Invoice.resident_id.in_(all_resident_ids),
            Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL])
        )
        .order_by(Invoice.period_year.asc(), Invoice.period_month.asc(), Invoice.id.asc())
        .all()
    )
    
    for inv in open_invoices:
        paid = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.invoice_id == inv.id)
            .scalar() or Decimal("0")
        )
        left = Decimal(inv.amount_total or 0) - paid
        if left <= 0:
            continue
        apply_amt = min(leftover, left)
        if apply_amt > 0:
            plan.append({"invoice_id": inv.id, "amount": float(apply_amt)})
            leftover -= apply_amt
            if leftover <= 0:
                break
    
    return {"ok": True, "plan": plan, "left_after": float(leftover)}


@router.post("/{payment_id}/applications")
def save_payment_applications(
    payment_id: int,
    data_json: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Сохранить распределение платежа по счетам."""
    from fastapi.responses import RedirectResponse
    
    # Вызываем backend функцию, но возвращаем JSON вместо редиректа
    try:
        import json
        items = json.loads(data_json)
        assert isinstance(items, list)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    
    p = db.get(Payment, payment_id)
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    # Текущие применения этого платежа
    cur_apps = {
        a.invoice_id: Decimal(a.amount_applied or 0)
        for a in db.query(PaymentApplication).filter(PaymentApplication.payment_id == p.id).all()
    }
    
    # Построим целевые значения после сохранения
    target_apps = dict(cur_apps)
    
    # Валидации по каждому переданному элементу
    for it in items:
        inv_id = int(it["invoice_id"])
        amt_new = Decimal(str(it["amount"]))
        if amt_new <= 0:
            continue
        
        inv = db.get(Invoice, inv_id)
        if not inv or inv.resident_id != p.resident_id:
            raise HTTPException(status_code=400, detail=f"Invalid invoice {inv_id}")
        
        # Остаток по счёту на сейчас
        paid_all = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.invoice_id == inv.id)
            .scalar() or Decimal("0")
        )
        left_now = Decimal(inv.amount_total or 0) - Decimal(paid_all)
        if left_now < 0:
            left_now = Decimal("0")
        
        # Сколько уже применено ИМЕННО этим платежом
        cur_amt_this = cur_apps.get(inv_id, Decimal("0"))
        
        # Разрешённый верхний предел
        allowed_max = left_now + cur_amt_this
        if amt_new > allowed_max:
            raise HTTPException(status_code=400, detail=f"Amount too much for invoice {inv_id}")
        
        target_apps[inv_id] = amt_new
    
    # Проверка лимита по сумме платежа
    total_target = sum(target_apps.values(), Decimal("0"))
    if total_target > Decimal(p.amount_total or 0):
        raise HTTPException(status_code=400, detail="Total amount exceeds payment amount")
    
    # Применяем изменения
    for inv_id, tgt in target_apps.items():
        app = (
            db.query(PaymentApplication)
            .filter(PaymentApplication.payment_id == p.id, PaymentApplication.invoice_id == inv_id)
            .first()
        )
        if app:
            app.amount_applied = tgt
        else:
                db.add(PaymentApplication(
                    payment_id=p.id,
                    invoice_id=inv_id,
                    amount_applied=tgt,
                    created_at=now_baku(),
                ))
    
    db.flush()
    
    # ЛОГИРОВАНИЕ
    try:
        log_user_id = user.id if 'user' in locals() else None
        db.add(PaymentLog(
            payment_id=p.id,
            resident_id=p.resident_id,
            user_id=log_user_id,
            action="APPLY",
            amount=float(total_target),
            details=f"Распределение платежа по {len(target_apps)} счетам"
        ))
    except Exception:
        pass

    # Пересчитать статусы вовлечённых счетов
    inv_ids = list(target_apps.keys())
    invs = db.query(Invoice).filter(Invoice.id.in_(inv_ids)).all()
    for inv in invs:
        _recompute_invoice_status(db, inv)
    
    db.commit()
    return {"ok": True}


@router.post("/{payment_id}/applications/public")
def save_payment_applications_public(
    payment_id: int,
    data_json: str = Form(...),
    db: Session = Depends(get_db),
):
    """Сохранить распределение платежа по счетам (публичный endpoint)."""
    try:
        import json
        items = json.loads(data_json)
        assert isinstance(items, list)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    
    p = db.get(Payment, payment_id)
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    # Текущие применения этого платежа
    cur_apps = {
        a.invoice_id: Decimal(a.amount_applied or 0)
        for a in db.query(PaymentApplication).filter(PaymentApplication.payment_id == p.id).all()
    }
    
    # Построим целевые значения после сохранения
    target_apps = dict(cur_apps)
    
    # Валидации по каждому переданному элементу
    for it in items:
        inv_id = int(it["invoice_id"])
        amt_new = Decimal(str(it["amount"]))
        if amt_new <= 0:
            continue
        
        inv = db.get(Invoice, inv_id)
        if not inv or inv.resident_id != p.resident_id:
            raise HTTPException(status_code=400, detail=f"Invalid invoice {inv_id}")
        
        # Остаток по счёту на сейчас
        paid_all = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.invoice_id == inv.id)
            .scalar() or Decimal("0")
        )
        left_now = Decimal(inv.amount_total or 0) - Decimal(paid_all)
        if left_now < 0:
            left_now = Decimal("0")
        
        # Сколько уже применено ИМЕННО этим платежом
        cur_amt_this = cur_apps.get(inv_id, Decimal("0"))
        
        # Разрешённый верхний предел
        allowed_max = left_now + cur_amt_this
        if amt_new > allowed_max:
            raise HTTPException(status_code=400, detail=f"Amount too much for invoice {inv_id}")
        
        target_apps[inv_id] = amt_new
    
    # Проверка лимита по сумме платежа
    total_target = sum(target_apps.values(), Decimal("0"))
    if total_target > Decimal(p.amount_total or 0):
        raise HTTPException(status_code=400, detail="Total amount exceeds payment amount")
    
    # Применяем изменения
    for inv_id, tgt in target_apps.items():
        app = (
            db.query(PaymentApplication)
            .filter(PaymentApplication.payment_id == p.id, PaymentApplication.invoice_id == inv_id)
            .first()
        )
        if app:
            app.amount_applied = tgt
        else:
            db.add(PaymentApplication(
                payment_id=p.id,
                invoice_id=inv_id,
                amount_applied=tgt,
                created_at=now_baku(),
            ))
    
    db.flush()
    
    # ЛОГИРОВАНИЕ
    try:
        log_user_id = user.id if 'user' in locals() else None
        db.add(PaymentLog(
            payment_id=p.id,
            resident_id=p.resident_id,
            user_id=log_user_id,
            action="APPLY",
            amount=float(total_target),
            details=f"Распределение платежа по {len(target_apps)} счетам"
        ))
    except Exception:
        pass

    # Пересчитать статусы вовлечённых счетов
    inv_ids = list(target_apps.keys())
    invs = db.query(Invoice).filter(Invoice.id.in_(inv_ids)).all()
    for inv in invs:
        _recompute_invoice_status(db, inv)
    
    db.commit()
    return {"ok": True}

