"""
Business logic for payments/applications/advance.

IMPORTANT:
- This module is intentionally prefixed with `api_` so other `api_*.py` routers can import
  business logic without depending on non-API (HTML/Jinja) routers like `payments.py`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from ..models import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentApplication,
    PaymentApplicationLine,
    PaymentLog,
    user_residents,
)
from ..utils import now_baku


def _to_int(val: str | None) -> int | None:
    if val is None:
        return None
    v = str(val).strip()
    if v == "":
        return None
    try:
        return int(v)
    except Exception:
        return None


def _recompute_invoice_status(db: Session, inv: Invoice) -> None:
    """Recompute invoice status based on applied amounts."""
    paid = (
        db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
        .filter(PaymentApplication.invoice_id == inv.id)
        .scalar()
        or Decimal("0")
    )
    total = Decimal(inv.amount_total or 0)
    if paid <= 0:
        inv.status = inv.status if inv.status == InvoiceStatus.CANCELED else InvoiceStatus.ISSUED
    elif paid < total:
        inv.status = InvoiceStatus.PARTIAL
    elif paid == total:
        inv.status = InvoiceStatus.PAID
    else:
        inv.status = InvoiceStatus.OVERPAID


def _record_line_distribution(
    db: Session,
    application_id: int,
    invoice_id: int,
    amount_applied: Decimal,
) -> None:
    """Persist per-invoice-line breakdown for a PaymentApplication."""
    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice_id).all()
    if not lines:
        return
    inv_total = sum(Decimal(str(ln.amount_total or 0)) for ln in lines)
    if inv_total <= 0:
        return
    rows: list[tuple[int, Decimal]] = []
    for ln in lines:
        share = round(amount_applied * Decimal(str(ln.amount_total or 0)) / inv_total, 2)
        rows.append((ln.id, share))
    total_shares = sum(r[1] for r in rows)
    diff = round(amount_applied - total_shares, 2)
    if diff != 0 and rows:
        largest_idx = max(range(len(rows)), key=lambda i: rows[i][1])
        rows[largest_idx] = (rows[largest_idx][0], rows[largest_idx][1] + diff)
    for line_id, amt in rows:
        db.add(PaymentApplicationLine(
            application_id=application_id, invoice_line_id=line_id, amount=amt,
        ))


def auto_apply_advance(
    db: Session,
    resident_id: int,
    reference_tag: Optional[str] = None,
) -> tuple[int, Decimal]:
    """
    Automatically apply available leftover amounts (advance pool) to open invoices
    of a specific resident (resident_id). Pool is formed from all residents linked
    to the same user(s) as the target resident.
    """
    affected_invoice_ids: set[int] = set()

    linked_users_subquery = (
        db.query(user_residents.c.user_id)
        .filter(user_residents.c.resident_id == resident_id)
        .subquery()
    )
    associated_residents = (
        db.query(user_residents.c.resident_id)
        .filter(user_residents.c.user_id.in_(linked_users_subquery))
        .all()
    )
    all_resident_ids = [r[0] for r in associated_residents]
    if not all_resident_ids:
        all_resident_ids = [resident_id]

    open_invoices: list[Invoice] = (
        db.query(Invoice)
        .filter(
            Invoice.resident_id == resident_id,
            Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL]),
        )
        .order_by(Invoice.period_year.asc(), Invoice.period_month.asc(), Invoice.id.asc())
        .all()
    )
    if not open_invoices:
        return (0, Decimal("0"))

    from sqlalchemy import cast, String

    payments: list[Payment] = (
        db.query(Payment)
        .filter(Payment.resident_id.in_(all_resident_ids), cast(Payment.method, String) != "ADVANCE")
        .order_by(Payment.received_at.asc(), Payment.id.asc())
        .all()
    )

    total_available_advance = Decimal("0")
    pay_leftover: dict[int, Decimal] = {}
    for p in payments:
        actual_applied = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.payment_id == p.id)
            .scalar()
            or Decimal("0")
        )
        left = Decimal(p.amount_total or 0) - Decimal(actual_applied)
        total_available_advance += left
        if left > 0:
            pay_leftover[p.id] = left

    if total_available_advance <= 0 or not pay_leftover:
        return (0, Decimal("0"))

    remaining_pool = total_available_advance
    total_actually_applied = Decimal("0")

    for inv in open_invoices:
        if remaining_pool <= 0:
            break

        paid_all = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.invoice_id == inv.id)
            .scalar()
            or Decimal("0")
        )
        inv_left = Decimal(inv.amount_total or 0) - Decimal(paid_all)
        if inv_left <= 0:
            continue

        for p in payments:
            if inv_left <= 0 or remaining_pool <= 0:
                break
            left = pay_leftover.get(p.id, Decimal("0"))
            if left <= 0:
                continue

            apply_amt = min(inv_left, left, remaining_pool)
            if apply_amt <= 0:
                continue

            if reference_tag:
                new_app = PaymentApplication(
                    payment_id=p.id,
                    invoice_id=inv.id,
                    amount_applied=apply_amt,
                    reference=reference_tag,
                    created_at=now_baku(),
                )
                db.add(new_app)
                db.flush()
                _record_line_distribution(db, new_app.id, inv.id, apply_amt)
            else:
                existing_app = (
                    db.query(PaymentApplication)
                    .filter(
                        PaymentApplication.payment_id == p.id,
                        PaymentApplication.invoice_id == inv.id,
                    )
                    .first()
                )
                if existing_app:
                    existing_app.amount_applied = Decimal(existing_app.amount_applied or 0) + apply_amt
                    existing_app.reference = "ADVANCE"
                    db.flush()
                    _record_line_distribution(db, existing_app.id, inv.id, apply_amt)
                else:
                    new_app = PaymentApplication(
                        payment_id=p.id,
                        invoice_id=inv.id,
                        amount_applied=apply_amt,
                        reference="ADVANCE",
                        created_at=now_baku(),
                    )
                    db.add(new_app)
                    db.flush()
                    _record_line_distribution(db, new_app.id, inv.id, apply_amt)

            pay_leftover[p.id] = left - apply_amt
            inv_left -= apply_amt
            remaining_pool -= apply_amt
            total_actually_applied += apply_amt
            affected_invoice_ids.add(inv.id)

        _recompute_invoice_status(db, inv)

    if affected_invoice_ids:
        db.add(
            PaymentLog(
                resident_id=resident_id,
                action="APPLY_AUTO",
                amount=float(total_actually_applied),
                details=f"Автоматическое распределение аванса по {len(affected_invoice_ids)} счетам",
            )
        )

    return (len(affected_invoice_ids), total_actually_applied)


def apply_payment_to_invoices(
    db: Session,
    payment_id: int,
    resident_id: int,
    scope: Optional[str] = None,
) -> int:
    """Apply a payment to invoices based on scope ('month'|'all'|None)."""
    payment = db.get(Payment, payment_id)
    if not payment:
        return 0
    if scope is None:
        return 0

    applied_total = (
        db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
        .filter(PaymentApplication.payment_id == payment_id)
        .scalar()
        or Decimal("0")
    )
    payment_leftover = Decimal(payment.amount_total or 0) - Decimal(applied_total)
    if payment_leftover <= 0:
        return 0

    today = date.today()
    cur_year, cur_month = today.year, today.month

    if scope == "all":
        linked_users_subquery = (
            db.query(user_residents.c.user_id)
            .filter(user_residents.c.resident_id == resident_id)
            .subquery()
        )
        associated_residents = (
            db.query(user_residents.c.resident_id)
            .filter(user_residents.c.user_id.in_(linked_users_subquery))
            .all()
        )
        all_resident_ids = [r[0] for r in associated_residents] or [resident_id]
        query = db.query(Invoice).filter(
            Invoice.resident_id.in_(all_resident_ids),
            Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL]),
        )
    else:
        query = db.query(Invoice).filter(
            Invoice.resident_id == resident_id,
            Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL]),
        )

    if scope == "month":
        query = query.filter(Invoice.period_year == cur_year, Invoice.period_month == cur_month)

    open_invoices = (
        query.order_by(Invoice.period_year.desc(), Invoice.period_month.desc(), Invoice.id.desc()).all()
    )
    if not open_invoices:
        return 0

    affected_invoice_ids: set[int] = set()
    remaining_payment = payment_leftover

    for inv in open_invoices:
        if remaining_payment <= 0:
            break

        paid_total = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.invoice_id == inv.id)
            .scalar()
            or Decimal("0")
        )
        inv_leftover = Decimal(inv.amount_total or 0) - Decimal(paid_total)
        if inv_leftover <= 0:
            continue

        apply_amount = min(remaining_payment, inv_leftover)
        if apply_amount <= 0:
            continue

        app = (
            db.query(PaymentApplication)
            .filter(PaymentApplication.payment_id == payment_id, PaymentApplication.invoice_id == inv.id)
            .first()
        )
        if app:
            app.amount_applied = Decimal(app.amount_applied or 0) + apply_amount
        else:
            db.add(
                PaymentApplication(
                    payment_id=payment_id,
                    invoice_id=inv.id,
                    amount_applied=apply_amount,
                    created_at=now_baku(),
                )
            )

        db.flush()
        remaining_payment -= apply_amount
        affected_invoice_ids.add(inv.id)
        _recompute_invoice_status(db, inv)

    return len(affected_invoice_ids)


def apply_payment_to_invoice(
    db: Session,
    payment_id: int,
    invoice_id: int,
    reference: Optional[str] = None,
    max_amount: Optional[Decimal] = None,
) -> Decimal:
    """Apply a payment to a single invoice. Returns applied amount."""
    payment = db.get(Payment, payment_id)
    inv = db.get(Invoice, invoice_id)
    if not payment or not inv:
        return Decimal("0")

    paid_total = (
        db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
        .filter(PaymentApplication.invoice_id == inv.id)
        .scalar()
        or Decimal("0")
    )
    inv_leftover = Decimal(inv.amount_total or 0) - Decimal(paid_total)
    if inv_leftover <= 0:
        return Decimal("0")

    applied_total = (
        db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
        .filter(PaymentApplication.payment_id == payment_id)
        .scalar()
        or Decimal("0")
    )
    payment_leftover = Decimal(payment.amount_total or 0) - Decimal(applied_total)
    if payment_leftover <= 0:
        return Decimal("0")

    apply_amt = min(inv_leftover, payment_leftover)
    if max_amount is not None:
        try:
            max_amount_dec = Decimal(str(max_amount))
        except Exception:
            max_amount_dec = Decimal("0")
        if max_amount_dec <= 0:
            return Decimal("0")
        apply_amt = min(apply_amt, max_amount_dec)
    if apply_amt <= 0:
        return Decimal("0")

    db.add(
        PaymentApplication(
            payment_id=payment_id,
            invoice_id=inv.id,
            amount_applied=apply_amt,
            reference=reference,
            created_at=now_baku(),
        )
    )

    db.flush()
    _recompute_invoice_status(db, inv)
    return apply_amt


def apply_advance_to_invoice(
    db: Session,
    user_id: int,
    resident_id: int,
    invoice_id: int,
    max_amount: Decimal,
    reference_tag: Optional[str] = None,
) -> int:
    """Apply advance pool to a single invoice with a max_amount limit."""
    if max_amount <= 0:
        return 0

    inv = db.get(Invoice, invoice_id)
    if not inv or inv.resident_id != resident_id:
        return 0

    paid_total = (
        db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
        .filter(PaymentApplication.invoice_id == inv.id)
        .scalar()
        or Decimal("0")
    )
    inv_leftover = Decimal(inv.amount_total or 0) - Decimal(paid_total)
    if inv_leftover <= 0:
        return 0

    associated_residents = (
        db.query(user_residents.c.resident_id)
        .filter(user_residents.c.user_id == user_id)
        .all()
    )
    all_resident_ids = [r[0] for r in associated_residents] or [resident_id]

    from sqlalchemy import cast, String

    payments: list[Payment] = (
        db.query(Payment)
        .filter(Payment.resident_id.in_(all_resident_ids), cast(Payment.method, String) != "ADVANCE")
        .order_by(Payment.created_at.asc(), Payment.id.asc())
        .all()
    )

    total_available_pool = Decimal("0")
    pay_leftover: dict[int, Decimal] = {}
    for p in payments:
        actual_applied = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.payment_id == p.id)
            .scalar()
            or Decimal("0")
        )
        left = Decimal(p.amount_total or 0) - Decimal(actual_applied)
        total_available_pool += left
        if left > 0:
            pay_leftover[p.id] = left

    if total_available_pool <= 0 or not pay_leftover:
        return 0

    remaining_to_apply = min(max_amount, total_available_pool, inv_leftover)
    if remaining_to_apply <= 0:
        return 0

    for p in payments:
        if remaining_to_apply <= 0:
            break
        left = pay_leftover.get(p.id, Decimal("0"))
        if left <= 0:
            continue
        apply_amt = min(left, remaining_to_apply)
        if apply_amt <= 0:
            continue

        new_app = PaymentApplication(
            payment_id=p.id,
            invoice_id=inv.id,
            amount_applied=apply_amt,
            reference=reference_tag or "ADVANCE",
            created_at=now_baku(),
        )
        db.add(new_app)
        db.flush()
        _record_line_distribution(db, new_app.id, inv.id, apply_amt)
        pay_leftover[p.id] = left - apply_amt
        remaining_to_apply -= apply_amt

    _recompute_invoice_status(db, inv)
    return 1


def apply_advance_with_limit(
    db: Session,
    user_id: int,
    resident_id: int,
    max_amount: Decimal,
    scope: Optional[str] = None,
    reference_tag: Optional[str] = None,
) -> int:
    """Apply advance pool to invoices with a max limit and scope ('month'|'all'|None)."""
    if scope is None:
        return 0
    if max_amount <= 0:
        return 0

    associated_residents = (
        db.query(user_residents.c.resident_id)
        .filter(user_residents.c.user_id == user_id)
        .all()
    )
    all_resident_ids = [r[0] for r in associated_residents] or [resident_id]

    today = date.today()
    cur_year, cur_month = today.year, today.month

    if scope == "all":
        query = db.query(Invoice).filter(
            Invoice.resident_id.in_(all_resident_ids),
            Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL]),
        )
    else:
        query = db.query(Invoice).filter(
            Invoice.resident_id == resident_id,
            Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL]),
        )

    if scope == "month":
        query = query.filter(Invoice.period_year == cur_year, Invoice.period_month == cur_month)

    open_invoices = (
        query.order_by(Invoice.period_year.asc(), Invoice.period_month.asc(), Invoice.id.asc()).all()
    )
    if not open_invoices:
        return 0

    from sqlalchemy import cast, String

    payments: list[Payment] = (
        db.query(Payment)
        .filter(Payment.resident_id.in_(all_resident_ids), cast(Payment.method, String) != "ADVANCE")
        .order_by(Payment.created_at.asc(), Payment.id.asc())
        .all()
    )

    total_available_pool = Decimal("0")
    pay_leftover: dict[int, Decimal] = {}
    for p in payments:
        actual_applied = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.payment_id == p.id)
            .scalar()
            or Decimal("0")
        )
        left = Decimal(p.amount_total or 0) - Decimal(actual_applied)
        total_available_pool += left
        if left > 0:
            pay_leftover[p.id] = left

    if total_available_pool <= 0 or not pay_leftover:
        return 0

    affected_invoice_ids: set[int] = set()
    remaining_to_apply = min(max_amount, total_available_pool)

    for inv in open_invoices:
        if remaining_to_apply <= 0:
            break

        paid_total = (
            db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))
            .filter(PaymentApplication.invoice_id == inv.id)
            .scalar()
            or Decimal("0")
        )
        inv_leftover = Decimal(inv.amount_total or 0) - Decimal(paid_total)
        if inv_leftover <= 0:
            continue

        inv_need = min(inv_leftover, remaining_to_apply)
        if inv_need <= 0:
            continue

        for p in payments:
            if inv_need <= 0 or remaining_to_apply <= 0:
                break
            left = pay_leftover.get(p.id, Decimal("0"))
            if left <= 0:
                continue
            apply_amt = min(left, inv_need, remaining_to_apply)
            if apply_amt <= 0:
                continue

            new_app = PaymentApplication(
                payment_id=p.id,
                invoice_id=inv.id,
                amount_applied=apply_amt,
                reference=reference_tag or "ADVANCE",
                created_at=now_baku(),
            )
            db.add(new_app)
            db.flush()
            _record_line_distribution(db, new_app.id, inv.id, apply_amt)
            pay_leftover[p.id] = left - apply_amt
            inv_need -= apply_amt
            remaining_to_apply -= apply_amt

        _recompute_invoice_status(db, inv)
        affected_invoice_ids.add(inv.id)

    return len(affected_invoice_ids)

