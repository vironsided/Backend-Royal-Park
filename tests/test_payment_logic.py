"""Money-logic tests: FIFO application, leftovers, advance pool, statuses.

These pin down the behavior of api_payment_logic before any future
PaymentService refactor. Concurrency (FOR UPDATE) is not testable on the
sqlite harness — covered by the live 2-thread race check on Postgres.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from app.models import (
    Invoice, InvoiceStatus, Payment, PaymentApplication, PaymentMethod,
)
from app.routers.api_payment_logic import (
    apply_payment_to_invoices,
    apply_payment_to_invoice,
    auto_apply_advance,
    apply_advance_with_limit,
    _recompute_invoice_status,
)


def _invoice(db, resident, year, month, total, status=InvoiceStatus.ISSUED):
    inv = Invoice(resident_id=resident.id, period_year=year, period_month=month,
                  status=status, amount_net=Decimal(total), amount_vat=Decimal("0"),
                  amount_total=Decimal(total))
    db.add(inv); db.commit(); db.refresh(inv)
    return inv


def _payment(db, resident, total, method=None):
    p = Payment(resident_id=resident.id, amount_total=Decimal(total),
                received_at=datetime(2026, 6, 1, 12, 0),
                created_at=datetime(2026, 6, 1, 12, 0),  # sqlite has no NOW()
                method=method or list(PaymentMethod)[0])
    db.add(p); db.commit(); db.refresh(p)
    return p


def _applied_for_payment(db, payment_id):
    return sum((Decimal(a.amount_applied or 0) for a in
                db.query(PaymentApplication).filter(PaymentApplication.payment_id == payment_id)),
               Decimal("0"))


@pytest.fixture()
def setup(db_session, factory):
    b = factory.block()
    r = factory.resident(b)
    u = factory.user("res1", __import__("app.models", fromlist=["RoleEnum"]).RoleEnum.RESIDENT)
    factory.link(u, r)
    return db_session, r, u


# ── apply_payment_to_invoices ────────────────────────────────────────────────

def test_payment_fully_covers_invoices(setup):
    db, r, _ = setup
    i1 = _invoice(db, r, 2026, 4, "30")
    i2 = _invoice(db, r, 2026, 5, "70")
    p = _payment(db, r, "100")
    apply_payment_to_invoices(db, p.id, r.id, scope="all")
    db.commit()
    assert _applied_for_payment(db, p.id) == Decimal("100")
    assert db.get(Invoice, i1.id).status == InvoiceStatus.PAID
    assert db.get(Invoice, i2.id).status == InvoiceStatus.PAID


def test_partial_payment_marks_partial(setup):
    db, r, _ = setup
    inv = _invoice(db, r, 2026, 5, "100")
    p = _payment(db, r, "40")
    apply_payment_to_invoices(db, p.id, r.id, scope="all")
    db.commit()
    assert _applied_for_payment(db, p.id) == Decimal("40")
    assert db.get(Invoice, inv.id).status == InvoiceStatus.PARTIAL


def test_second_apply_does_not_double_spend(setup):
    """The leftover check must make a repeated apply a no-op."""
    db, r, _ = setup
    _invoice(db, r, 2026, 5, "200")
    p = _payment(db, r, "100")
    apply_payment_to_invoices(db, p.id, r.id, scope="all")
    db.commit()
    apply_payment_to_invoices(db, p.id, r.id, scope="all")
    db.commit()
    assert _applied_for_payment(db, p.id) == Decimal("100")


def test_payment_never_exceeds_invoice_total(setup):
    db, r, _ = setup
    inv = _invoice(db, r, 2026, 5, "60")
    p = _payment(db, r, "100")
    apply_payment_to_invoices(db, p.id, r.id, scope="all")
    db.commit()
    paid = sum((Decimal(a.amount_applied or 0) for a in
                db.query(PaymentApplication).filter(PaymentApplication.invoice_id == inv.id)),
               Decimal("0"))
    assert paid == Decimal("60")
    assert db.get(Invoice, inv.id).status == InvoiceStatus.PAID


def test_scope_none_applies_nothing(setup):
    db, r, _ = setup
    _invoice(db, r, 2026, 5, "100")
    p = _payment(db, r, "100")
    assert apply_payment_to_invoices(db, p.id, r.id, scope=None) == 0
    assert _applied_for_payment(db, p.id) == Decimal("0")


def test_scope_month_only_touches_current_period(setup):
    db, r, _ = setup
    today = datetime.utcnow()
    cur = _invoice(db, r, today.year, today.month, "50")
    old = _invoice(db, r, 2025, 1, "50")
    p = _payment(db, r, "100")
    apply_payment_to_invoices(db, p.id, r.id, scope="month")
    db.commit()
    assert db.get(Invoice, cur.id).status == InvoiceStatus.PAID
    assert db.get(Invoice, old.id).status == InvoiceStatus.ISSUED
    assert _applied_for_payment(db, p.id) == Decimal("50")


# ── apply_payment_to_invoice ─────────────────────────────────────────────────

def test_single_invoice_max_amount_cap(setup):
    db, r, _ = setup
    inv = _invoice(db, r, 2026, 5, "100")
    p = _payment(db, r, "100")
    applied = apply_payment_to_invoice(db, p.id, inv.id, max_amount=Decimal("25"))
    db.commit()
    assert applied == Decimal("25")
    assert db.get(Invoice, inv.id).status == InvoiceStatus.PARTIAL


def test_single_invoice_no_apply_when_paid(setup):
    db, r, _ = setup
    inv = _invoice(db, r, 2026, 5, "50")
    p1 = _payment(db, r, "50")
    apply_payment_to_invoice(db, p1.id, inv.id)
    db.commit()
    p2 = _payment(db, r, "50")
    assert apply_payment_to_invoice(db, p2.id, inv.id) == Decimal("0")


# ── advance pool ─────────────────────────────────────────────────────────────

def test_auto_apply_advance_uses_leftovers_oldest_first(setup):
    """Two payments with leftovers form the pool; oldest invoice paid first."""
    db, r, _ = setup
    i_old = _invoice(db, r, 2026, 1, "80")
    i_new = _invoice(db, r, 2026, 2, "80")
    _payment(db, r, "60")
    _payment(db, r, "60")
    affected, applied = auto_apply_advance(db, r.id)
    db.commit()
    assert applied == Decimal("120")
    assert db.get(Invoice, i_old.id).status == InvoiceStatus.PAID      # 80
    assert db.get(Invoice, i_new.id).status == InvoiceStatus.PARTIAL   # 40 of 80


def test_auto_apply_advance_empty_pool_is_noop(setup):
    db, r, _ = setup
    inv = _invoice(db, r, 2026, 5, "100")
    p = _payment(db, r, "100")
    apply_payment_to_invoice(db, p.id, inv.id)  # exhaust the payment
    db.commit()
    affected, applied = auto_apply_advance(db, r.id)
    assert (affected, applied) == (0, Decimal("0"))


def test_apply_advance_with_limit_respects_max(setup):
    db, r, u = setup
    inv = _invoice(db, r, 2026, 5, "100")
    _payment(db, r, "100")
    apply_advance_with_limit(db, u.id, r.id, max_amount=Decimal("30"), scope="all")
    db.commit()
    paid = sum((Decimal(a.amount_applied or 0) for a in
                db.query(PaymentApplication).filter(PaymentApplication.invoice_id == inv.id)),
               Decimal("0"))
    assert paid == Decimal("30")
    assert db.get(Invoice, inv.id).status == InvoiceStatus.PARTIAL


# ── status recompute ─────────────────────────────────────────────────────────

def test_recompute_overpaid(setup):
    db, r, _ = setup
    inv = _invoice(db, r, 2026, 5, "50")
    p = _payment(db, r, "70")
    db.add(PaymentApplication(payment_id=p.id, invoice_id=inv.id,
                              amount_applied=Decimal("70"), created_at=datetime.utcnow()))
    db.flush()
    _recompute_invoice_status(db, inv)
    db.commit()
    assert inv.status == InvoiceStatus.OVERPAID


def test_recompute_canceled_stays_canceled(setup):
    db, r, _ = setup
    inv = _invoice(db, r, 2026, 5, "50", status=InvoiceStatus.CANCELED)
    _recompute_invoice_status(db, inv)
    assert inv.status == InvoiceStatus.CANCELED
