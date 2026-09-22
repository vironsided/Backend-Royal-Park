from datetime import datetime
from decimal import Decimal
import json
from types import SimpleNamespace

from app.models import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    OnlineTransaction,
    PaymentApplication,
)
from app.routers.api_azericard import _confirm_local_transaction_from_callback
from app.services.payment_line_allocation import (
    build_invoice_line_payment_map,
    normalize_selected_line_ids_for_water_sewer,
    parse_selected_line_ids,
)


def _line(line_id: int, description: str, total: str):
    return SimpleNamespace(
        id=line_id,
        description=description,
        amount_total=Decimal(total),
    )


def _application(amount: str, reference: str):
    return SimpleNamespace(
        id=1,
        amount_applied=Decimal(amount),
        reference=reference,
        created_at=datetime(2026, 1, 1),
    )


def test_water_selection_automatically_includes_sewerage_first():
    descriptions = {
        10: "Холодная вода",
        11: "Канализация (авто)",
        12: "Электроэнергия",
    }

    selected = normalize_selected_line_ids_for_water_sewer([10], descriptions)

    assert selected == [11, 10]


def test_exact_water_amount_pays_sewerage_before_water():
    lines = [
        _line(10, "Холодная вода", "2.00"),
        _line(11, "Канализация (авто)", "0.40"),
    ]
    applications = [_application("2.00", "AZERICARD:1|LINESEL:11,10")]

    state = build_invoice_line_payment_map(lines, applications)

    assert state[11]["paid"] == Decimal("0.40")
    assert state[11]["remaining"] == Decimal("0.00")
    assert state[10]["paid"] == Decimal("1.60")
    assert state[10]["remaining"] == Decimal("0.40")


def test_selected_line_parser_ignores_invalid_and_non_positive_values():
    assert parse_selected_line_ids("AZERICARD:1|LINESEL:11, nope, 0, -2, 10|X") == [
        11,
        10,
    ]


def test_azericard_callback_keeps_selection_and_closes_sewerage_first(
    db_session,
    factory,
):
    db_session.connection().connection.create_function(
        "NOW",
        0,
        lambda: "2026-01-01 00:00:00",
    )
    resident = factory.resident(factory.block())
    invoice = Invoice(
        resident_id=resident.id,
        status=InvoiceStatus.ISSUED,
        period_year=2026,
        period_month=1,
        amount_net=Decimal("2.40"),
        amount_vat=Decimal("0.00"),
        amount_total=Decimal("2.40"),
    )
    db_session.add(invoice)
    db_session.flush()

    water = InvoiceLine(
        invoice_id=invoice.id,
        description="Холодная вода",
        amount_net=Decimal("2.00"),
        amount_vat=Decimal("0.00"),
        amount_total=Decimal("2.00"),
    )
    sewerage = InvoiceLine(
        invoice_id=invoice.id,
        description="Канализация (авто)",
        amount_net=Decimal("0.40"),
        amount_vat=Decimal("0.00"),
        amount_total=Decimal("0.40"),
    )
    db_session.add_all([water, sewerage])
    db_session.flush()

    transaction = OnlineTransaction(
        resident_id=resident.id,
        invoice_id=invoice.id,
        order_id="ORDER-WATER-2-AZN",
        amount_total=Decimal("2.00"),
        currency="AZN",
        trtype="0",
        terminal_category="utility",
        gateway_status="INITIATED",
        request_payload=json.dumps({"_selected_line_ids": [water.id]}),
    )
    db_session.add(transaction)
    db_session.flush()

    _confirm_local_transaction_from_callback(
        db_session,
        transaction,
        callback_data={},
    )
    db_session.flush()

    application = db_session.query(PaymentApplication).one()
    assert application.amount_applied == Decimal("2.00")
    assert application.reference == (
        f"AZERICARD:{transaction.order_id}|LINESEL:{sewerage.id},{water.id}"
    )

    state = build_invoice_line_payment_map([water, sewerage], [application])
    assert state[sewerage.id]["remaining"] == Decimal("0.00")
    assert state[water.id]["remaining"] == Decimal("0.40")
