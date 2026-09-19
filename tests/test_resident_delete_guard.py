"""Регрессия P0/P1 по api_residents: удаление больше не сносит биллинг-историю.

Что фиксируем:
  1) DELETE /api/residents/{id} у резидента со счетами/платежами/показаниями -> 409
     (раньше db.delete(resident) каскадом уносил счета, строки счетов, счётчики и
     показания, а при наличии платежей падал невнятным 500 из-за RESTRICT).
  2) Резидент без единой связанной записи удаляется как раньше -> 204.
  3) PUT /api/residents/{id} со снятым счётчиком: счётчик с показаниями только
     деактивируется (не удаляется), без показаний — по-прежнему удаляется.
  4) Правка начального долга не уничтожает payment_application_lines: строки
     opening-инвойса обновляются на месте, а обнуление категории, по которой уже
     разнесён платёж, отдаёт 409 вместо тихой потери детализации.
  5) Обычные операции (список/карточка/создание/редактирование/INACTIVE) целы.
"""

from datetime import datetime
from decimal import Decimal

from app.models import (
    RoleEnum, ResidentStatus, MeterType, CustomerType, Tariff, ResidentMeter,
    MeterReading, Invoice, InvoiceLine, InvoiceStatus, Payment, PaymentMethod,
    PaymentApplication, PaymentApplicationLine,
)


def _staff(factory, username="admin_del"):
    return factory.user(username, RoleEnum.ADMIN)


def _tariff(db, name="El", meter_type=MeterType.ELECTRIC):
    t = Tariff(name=name, meter_type=meter_type, customer_type=CustomerType.INDIVIDUAL,
               vat_percent=0, is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    return t


def _create_resident_api(client, factory, block, cookies, unit="A/10", **extra):
    payload = {
        "block_id": block.id,
        "unit_number": unit,
        "resident_type": "OWNER",
        "customer_type": "INDIVIDUAL",
        "status": "ACTIVE",
        "owner_full_name": "Test Owner",
        "meters": [],
    }
    payload.update(extra)
    return client.post("/api/residents/", json=payload, cookies=cookies)


# ---------------------------------------------------------------------------
# 1) DELETE резидента
# ---------------------------------------------------------------------------

def test_delete_resident_with_invoice_blocked(client, factory, db_session):
    """Есть счёт -> 409, резидент и счёт на месте (не каскадное удаление)."""
    staff = _staff(factory)
    block = factory.block()
    resident = factory.resident(block, unit="A/1")
    inv = Invoice(resident_id=resident.id, number="INV/1", status=InvoiceStatus.ISSUED,
                  period_year=2026, period_month=1, amount_net=Decimal("10"),
                  amount_vat=Decimal("0"), amount_total=Decimal("10"))
    db_session.add(inv); db_session.commit()

    resp = client.delete(f"/api/residents/{resident.id}", cookies=factory.cookie(staff))
    assert resp.status_code == 409, resp.text
    assert "INACTIVE" in resp.json()["detail"]

    db_session.expire_all()
    assert db_session.get(type(resident), resident.id) is not None
    assert db_session.get(Invoice, inv.id) is not None


def test_delete_resident_with_payment_blocked(client, factory, db_session):
    """Есть платёж -> 409 (а не 500 от FK RESTRICT на payments.resident_id)."""
    staff = _staff(factory)
    block = factory.block()
    resident = factory.resident(block, unit="A/2")
    # created_at задаём явно: server_default NOW() не существует в sqlite-фикстуре.
    pay = Payment(resident_id=resident.id, received_at=datetime.utcnow(),
                  amount_total=Decimal("50"), method=PaymentMethod.CASH,
                  created_at=datetime.utcnow())
    db_session.add(pay); db_session.commit()

    resp = client.delete(f"/api/residents/{resident.id}", cookies=factory.cookie(staff))
    assert resp.status_code == 409, resp.text
    db_session.expire_all()
    assert db_session.get(Payment, pay.id) is not None


def test_delete_resident_with_readings_blocked(client, factory, db_session):
    """Есть показания -> 409, показание и счётчик целы."""
    staff = _staff(factory)
    block = factory.block()
    resident = factory.resident(block, unit="A/3")
    tar = _tariff(db_session)
    meter = ResidentMeter(resident_id=resident.id, meter_type=MeterType.ELECTRIC,
                          serial_number="SN-1", initial_reading=Decimal("0"),
                          tariff_id=tar.id, is_active=True)
    db_session.add(meter); db_session.commit(); db_session.refresh(meter)
    reading = MeterReading(resident_meter_id=meter.id, reading_date=datetime.utcnow(),
                           value=Decimal("100"), consumption=Decimal("100"), tariff_id=tar.id,
                           amount_net=Decimal("10"), vat_percent=0, amount_vat=Decimal("0"),
                           amount_total=Decimal("10"))
    db_session.add(reading); db_session.commit()

    resp = client.delete(f"/api/residents/{resident.id}", cookies=factory.cookie(staff))
    assert resp.status_code == 409, resp.text
    db_session.expire_all()
    assert db_session.get(MeterReading, reading.id) is not None
    assert db_session.get(ResidentMeter, meter.id) is not None


def test_delete_clean_resident_still_works(client, factory, db_session):
    """Резидент без связанных записей удаляется как раньше -> 204."""
    staff = _staff(factory)
    block = factory.block()
    resident = factory.resident(block, unit="A/4")
    rid = resident.id

    resp = client.delete(f"/api/residents/{rid}", cookies=factory.cookie(staff))
    assert resp.status_code == 204, resp.text
    db_session.expire_all()
    assert db_session.get(type(resident), rid) is None


# ---------------------------------------------------------------------------
# 2) Счётчики при PUT
# ---------------------------------------------------------------------------

def test_meter_with_readings_is_deactivated_not_deleted(client, factory, db_session):
    """Снятый счётчик с показаниями -> is_active=False, показание живо."""
    staff = _staff(factory)
    block = factory.block()
    tar = _tariff(db_session)
    cookies = factory.cookie(staff)

    created = _create_resident_api(
        client, factory, block, cookies, unit="B/1",
        meters=[{"meter_type": "ELECTRIC", "serial": "SN-9", "used": False,
                 "initial": 0, "tariff_id": tar.id}],
    )
    assert created.status_code == 201, created.text
    rid = created.json()["id"]
    meter_id = created.json()["meters"][0]["id"]

    reading = MeterReading(resident_meter_id=meter_id, reading_date=datetime.utcnow(),
                           value=Decimal("50"), consumption=Decimal("50"), tariff_id=tar.id,
                           amount_net=Decimal("5"), vat_percent=0, amount_vat=Decimal("0"),
                           amount_total=Decimal("5"))
    db_session.add(reading); db_session.commit()

    upd = client.put(f"/api/residents/{rid}", json={"meters": []}, cookies=cookies)
    assert upd.status_code == 200, upd.text
    assert upd.json()["meters"] == []

    db_session.expire_all()
    meter = db_session.get(ResidentMeter, meter_id)
    assert meter is not None and meter.is_active is False
    assert db_session.get(MeterReading, reading.id) is not None


def test_meter_without_readings_is_still_deleted(client, factory, db_session):
    """Счётчик без истории по-прежнему удаляется физически (поведение не менялось)."""
    staff = _staff(factory)
    block = factory.block()
    tar = _tariff(db_session)
    cookies = factory.cookie(staff)

    created = _create_resident_api(
        client, factory, block, cookies, unit="B/2",
        meters=[{"meter_type": "ELECTRIC", "serial": "SN-10", "used": False,
                 "initial": 0, "tariff_id": tar.id}],
    )
    assert created.status_code == 201, created.text
    rid = created.json()["id"]
    meter_id = created.json()["meters"][0]["id"]

    upd = client.put(f"/api/residents/{rid}", json={"meters": []}, cookies=cookies)
    assert upd.status_code == 200, upd.text
    db_session.expire_all()
    assert db_session.get(ResidentMeter, meter_id) is None


# ---------------------------------------------------------------------------
# 3) Начальный долг и разнесение платежей
# ---------------------------------------------------------------------------

def _apply_payment_to_line(db, resident_id, invoice_id, line_id, amount="30"):
    # created_at задаём явно: server_default NOW() не существует в sqlite-фикстуре.
    pay = Payment(resident_id=resident_id, received_at=datetime.utcnow(),
                  amount_total=Decimal(amount), method=PaymentMethod.CASH,
                  created_at=datetime.utcnow())
    db.add(pay); db.commit(); db.refresh(pay)
    app_row = PaymentApplication(payment_id=pay.id, invoice_id=invoice_id,
                                 amount_applied=Decimal(amount), reference="DIRECT",
                                 created_at=datetime.utcnow())
    db.add(app_row); db.commit(); db.refresh(app_row)
    pal = PaymentApplicationLine(application_id=app_row.id, invoice_line_id=line_id,
                                 amount=Decimal(amount))
    db.add(pal); db.commit(); db.refresh(pal)
    return pay, app_row, pal


def _opening_lines(db, resident_id):
    inv = db.query(Invoice).filter(
        Invoice.resident_id == resident_id,
        Invoice.number == f"OPEN/{resident_id:06d}",
    ).one()
    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == inv.id).order_by(InvoiceLine.id).all()
    return inv, lines


def test_opening_debt_edit_keeps_payment_application_lines(client, factory, db_session):
    """Правка суммы начального долга обновляет строки на месте: id строк и
    payment_application_lines сохраняются (раньше строки пересоздавались и
    детализация разнесения молча уходила каскадом)."""
    staff = _staff(factory)
    block = factory.block()
    cookies = factory.cookie(staff)

    created = _create_resident_api(client, factory, block, cookies, unit="C/1",
                                   debt_utility=100, debt_service=50, debt_rent=0)
    assert created.status_code == 201, created.text
    rid = created.json()["id"]

    inv, lines = _opening_lines(db_session, rid)
    assert len(lines) == 2
    utility_line = [ln for ln in lines if "(Utility)" in ln.description][0]
    _, _, pal = _apply_payment_to_line(db_session, rid, inv.id, utility_line.id, "30")
    line_ids_before = [ln.id for ln in lines]

    upd = client.put(f"/api/residents/{rid}",
                     json={"debt_utility": 200, "debt_service": 50, "debt_rent": 0},
                     cookies=cookies)
    assert upd.status_code == 200, upd.text
    assert upd.json()["debt_utility"] == 200.0
    assert upd.json()["debt_service"] == 50.0

    db_session.expire_all()
    _, lines_after = _opening_lines(db_session, rid)
    assert [ln.id for ln in lines_after] == line_ids_before  # строки те же, не пересозданы
    assert db_session.get(PaymentApplicationLine, pal.id) is not None


def test_opening_debt_zeroing_paid_category_blocked(client, factory, db_session):
    """Обнуление категории, по которой уже разнесён платёж -> 409, детализация цела."""
    staff = _staff(factory)
    block = factory.block()
    cookies = factory.cookie(staff)

    created = _create_resident_api(client, factory, block, cookies, unit="C/2",
                                   debt_utility=100, debt_service=50, debt_rent=0)
    assert created.status_code == 201, created.text
    rid = created.json()["id"]

    inv, lines = _opening_lines(db_session, rid)
    utility_line = [ln for ln in lines if "(Utility)" in ln.description][0]
    _, _, pal = _apply_payment_to_line(db_session, rid, inv.id, utility_line.id, "30")

    upd = client.put(f"/api/residents/{rid}",
                     json={"debt_utility": 0, "debt_service": 150, "debt_rent": 0},
                     cookies=cookies)
    assert upd.status_code == 409, upd.text

    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(PaymentApplicationLine, pal.id) is not None
    assert db_session.get(InvoiceLine, utility_line.id) is not None


def test_opening_debt_zeroing_unpaid_category_still_works(client, factory, db_session):
    """Категория без разнесённых платежей по-прежнему обнуляется (строка удаляется)."""
    staff = _staff(factory)
    block = factory.block()
    cookies = factory.cookie(staff)

    created = _create_resident_api(client, factory, block, cookies, unit="C/3",
                                   debt_utility=100, debt_service=50, debt_rent=0)
    assert created.status_code == 201, created.text
    rid = created.json()["id"]

    upd = client.put(f"/api/residents/{rid}",
                     json={"debt_utility": 100, "debt_service": 0, "debt_rent": 0},
                     cookies=cookies)
    assert upd.status_code == 200, upd.text
    assert upd.json()["debt_service"] == 0.0
    assert upd.json()["debt"] == 100.0

    db_session.expire_all()
    _, lines_after = _opening_lines(db_session, rid)
    assert len(lines_after) == 1
    assert "(Utility)" in lines_after[0].description


# ---------------------------------------------------------------------------
# 4) Регресс обычных сценариев
# ---------------------------------------------------------------------------

def test_regular_resident_flow_unchanged(client, factory, db_session):
    """Список / карточка / создание / редактирование / перевод в INACTIVE — как раньше."""
    staff = _staff(factory)
    block = factory.block()
    cookies = factory.cookie(staff)

    created = _create_resident_api(client, factory, block, cookies, unit="D/1")
    assert created.status_code == 201, created.text
    rid = created.json()["id"]

    lst = client.get("/api/residents/", cookies=cookies)
    assert lst.status_code == 200, lst.text
    assert any(item["id"] == rid for item in lst.json()["items"])

    card = client.get(f"/api/residents/{rid}", cookies=cookies)
    assert card.status_code == 200, card.text
    assert card.json()["unit_number"] == "D/1"

    upd = client.put(f"/api/residents/{rid}",
                     json={"owner_full_name": "New Owner", "comment": "test"},
                     cookies=cookies)
    assert upd.status_code == 200, upd.text
    assert upd.json()["owner_full_name"] == "New Owner"

    deact = client.put(f"/api/residents/{rid}", json={"status": "INACTIVE"}, cookies=cookies)
    assert deact.status_code == 200, deact.text
    assert deact.json()["status"] == ResidentStatus.INACTIVE.value
