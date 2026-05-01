import enum
from datetime import datetime, date
from decimal import Decimal
from sqlalchemy import (
    String, Integer, Enum, Boolean, DateTime, ForeignKey, UniqueConstraint, 
    Numeric, Text, Table, Column, Date
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy import text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base




# Связка: пользователь-житель ↔ объекты-резиденты (дома/виллы)
user_residents = Table(
    "user_residents",
    Base.metadata,
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("resident_id", ForeignKey("residents.id", ondelete="CASCADE"), primary_key=True),
)


class InvoiceStatus(str, enum.Enum):
    DRAFT = "DRAFT"       # черновик (до выставления)
    ISSUED = "ISSUED"     # выставлен (есть срок оплаты)
    PARTIAL = "PARTIAL"   # частично оплачен
    PAID = "PAID"         # полностью оплачен
    OVERPAID = "OVERPAID" # переплата (редко)
    CANCELED = "CANCELED" # отменён

#Енумы для ролей
class RoleEnum(str, enum.Enum):
    ROOT = "ROOT"
    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    RESIDENT = "RESIDENT"
    SALES = "SALES"  # менеджер по продаже вилл/домов (как Satish)

#Енумы типов счётчиков
class MeterType(str, enum.Enum):
    ELECTRIC = "ELECTRIC"
    GAS = "GAS"
    WATER = "WATER"
    SEWERAGE = "SEWERAGE"
    SERVICE = "SERVICE"
    RENT = "RENT"
    CONSTRUCTION = "CONSTRUCTION"



#Енумы для типо владельцев
class CustomerType(str, enum.Enum):
    INDIVIDUAL = "INDIVIDUAL"
    LEGAL = "LEGAL"

class ResidentType(str, enum.Enum):
    OWNER = "OWNER"            # Частный дом (владелец)
    TENANT = "TENANT"          # Арендатор
    SUBTENANT = "SUBTENANT"    # Субарендатор
    OFFICE = "OFFICE"          # Офис

class ResidentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"

# ----- PaymentMethod -----
class PaymentMethod(str, enum.Enum):
    CASH = "CASH"
    CARD = "CARD"
    TRANSFER = "TRANSFER"
    ONLINE = "ONLINE"
    ADVANCE = "ADVANCE"    # Оплата из внутреннего аванса резидента

# ----- Payment / PaymentApplication -----
class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    resident_id = Column(ForeignKey("residents.id", ondelete="RESTRICT"), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    amount_total = Column(Numeric(12, 2), nullable=False)
    method = Column(SAEnum(PaymentMethod, name="payment_method"), nullable=False)
    reference = Column(String(120), nullable=True)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))
    created_by_id = Column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)

    resident = relationship("Resident", lazy="joined")
    created_by = relationship("User", lazy="joined")
    applications = relationship(
        "PaymentApplication",
        back_populates="payment",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def applied_total(self) -> Decimal:
        # генератор целиком в скобках, стартовое значение — Decimal("0")
        return sum(((a.amount_applied or Decimal("0")) for a in (self.applications or [])), Decimal("0"))

    @property
    def leftover(self) -> Decimal:
        return (self.amount_total or Decimal("0")) - self.applied_total


class PaymentApplication(Base):
    __tablename__ = "payment_applications"
    # УБИРАЕМ уникальное ограничение, чтобы разрешить несколько применений одного платежа к одному счету
    # Это нужно для того, чтобы каждое "Погасить из аванса" создавало отдельную строку в invoice details

    id = Column(Integer, primary_key=True)
    payment_id = Column(ForeignKey("payments.id", ondelete="CASCADE"), nullable=False)
    invoice_id = Column(ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False)
    amount_applied = Column(Numeric(12, 2), nullable=False)
    reference = Column(String(100), nullable=True) # "ADVANCE" or "DIRECT"
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    payment = relationship("Payment", back_populates="applications")
    invoice = relationship("Invoice", lazy="joined")
    line_distributions = relationship(
        "PaymentApplicationLine",
        back_populates="application",
        cascade="all, delete-orphan",
    )


class PaymentApplicationLine(Base):
    __tablename__ = "payment_application_lines"

    id = Column(Integer, primary_key=True)
    application_id = Column(ForeignKey("payment_applications.id", ondelete="CASCADE"), nullable=False)
    invoice_line_id = Column(ForeignKey("invoice_lines.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)

    application = relationship("PaymentApplication", back_populates="line_distributions")
    invoice_line = relationship("InvoiceLine")


class OnlineTransaction(Base):
    __tablename__ = "online_transactions"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_online_transactions_order_id"),
    )

    id = Column(Integer, primary_key=True)
    payment_id = Column(ForeignKey("payments.id", ondelete="SET NULL"), nullable=True)
    resident_id = Column(ForeignKey("residents.id", ondelete="SET NULL"), nullable=True)
    invoice_id = Column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True)

    order_id = Column(String(32), nullable=False, index=True)
    amount_total = Column(Numeric(12, 2), nullable=False, default=0)
    currency = Column(String(8), nullable=False, default="AZN")
    trtype = Column(String(8), nullable=True)

    rrn = Column(String(32), nullable=True)
    int_ref = Column(String(64), nullable=True)
    approval = Column(String(32), nullable=True)
    action_code = Column(String(16), nullable=True)
    rc = Column(String(16), nullable=True)
    gateway_status = Column(String(32), nullable=False, default="INITIATED")

    terminal_category = Column(String(32), nullable=True)

    request_payload = Column(Text, nullable=True)
    callback_payload = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at = Column(DateTime, nullable=False, server_default=text("NOW()"), onupdate=datetime.utcnow)

    payment = relationship("Payment", lazy="joined")
    resident = relationship("Resident", lazy="joined")
    invoice = relationship("Invoice", lazy="joined")


class SavedCard(Base):
    __tablename__ = "saved_cards"
    __table_args__ = (
        UniqueConstraint("user_id", "token_id", name="uq_saved_cards_user_token"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_id = Column(String(256), nullable=False)
    masked_pan = Column(String(32), nullable=False, default="****")
    card_brand = Column(String(32), nullable=True)
    expiry_month = Column(Integer, nullable=True)
    expiry_year = Column(Integer, nullable=True)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))

    user = relationship("User", lazy="joined")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[RoleEnum] = mapped_column(Enum(RoleEnum), nullable=False, default=RoleEnum.RESIDENT)

    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    comment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    avatar_path: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Требуется смена пароля (первый вход / сброс):
    require_password_change: Mapped[bool] = mapped_column(Boolean, default=True)

    # Для локальной системы — хранить временный пароль открытым текстом (как просили):
    temp_password_plain: Mapped[str | None] = mapped_column(String(128), nullable=True)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    last_password_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_by = relationship("User", remote_side=[id], lazy="joined")

    # Привязанные дома (только для пользователей-Жителей)
    resident_links: Mapped[list["Resident"]] = relationship(
        "Resident",
        secondary=user_residents,
        back_populates="tenant_links",
        lazy="selectin",
    )


class PushDeviceToken(Base):
    __tablename__ = "push_device_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_push_device_tokens_token_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)  # android | ios | web
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    device_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    user: Mapped["User"] = relationship("User", lazy="joined")

class Block(Base):
    __tablename__ = "blocks"
    __table_args__ = (UniqueConstraint("name", name="uq_blocks_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by: Mapped["User"] = relationship("User", foreign_keys=[created_by_id], lazy="joined")

# --- НОВЫЕ ТАБЛИЦЫ ---
class Tariff(Base):
    __tablename__ = "tariffs"
    __table_args__ = (
        UniqueConstraint("name", "meter_type", "customer_type", name="uq_tariffs_name_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    meter_type: Mapped[MeterType] = mapped_column(Enum(MeterType), nullable=False)
    customer_type: Mapped[CustomerType] = mapped_column(Enum(CustomerType), nullable=False)
    vat_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0..100
    # Для некоторых счётчиков расход нужно умножать на коэффициент (например, трансформаторные).
    use_multiplier: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consumption_multiplier: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, default=Decimal("1"))
    # Для ELECTRIC/GAS: фиксированная сумма, добавляется к начислению независимо от расхода.
    stable_tariff: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    # Для WATER: процент канализации, который рассчитывается как % от суммы воды (доп. строка в счёте)
    sewerage_percent: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by = relationship("User", foreign_keys=[created_by_id], lazy="joined")

    steps: Mapped[list["TariffStep"]] = relationship(
        "TariffStep",
        back_populates="tariff",
        cascade="all, delete-orphan",
        order_by="TariffStep.from_value.asc()",
    )


class TariffStep(Base):
    __tablename__ = "tariff_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id", ondelete="CASCADE"), nullable=False)

    # Числовые границы (для большинства типов)
    from_value: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    to_value: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)

    # Датированные границы (для строительных услуг)
    from_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    to_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)

    tariff: Mapped["Tariff"] = relationship("Tariff", back_populates="steps")

# --- НОВЫЕ ТАБЛИЦЫ ---
class Resident(Base):
    __tablename__ = "residents"
    __table_args__ = (
        UniqueConstraint("block_id", "unit_number", name="uq_resident_block_unit"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    block_id: Mapped[int] = mapped_column(ForeignKey("blocks.id", ondelete="RESTRICT"), nullable=False)
    block: Mapped["Block"] = relationship("Block", lazy="joined")

    unit_number: Mapped[str] = mapped_column(String(64), nullable=False)  # номер дома/квартиры в блоке
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    resident_type: Mapped[ResidentType] = mapped_column(Enum(ResidentType), nullable=False, default=ResidentType.OWNER)
    customer_type: Mapped[CustomerType] = mapped_column(Enum(CustomerType), nullable=False, default=CustomerType.INDIVIDUAL)
    status: Mapped[ResidentStatus] = mapped_column(Enum(ResidentStatus), nullable=False, default=ResidentStatus.ACTIVE)

    owner_full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    owner_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    owner_email: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by = relationship("User", foreign_keys=[created_by_id], lazy="joined")

    meters: Mapped[list["ResidentMeter"]] = relationship(
        "ResidentMeter",
        back_populates="resident",
        cascade="all, delete-orphan",
        order_by="ResidentMeter.id.asc()",
        lazy="selectin",  # Используем selectin вместо default, чтобы избежать дубликатов при JOIN
    )

    tenant_links: Mapped[list["User"]] = relationship(
        "User",
        secondary=user_residents,
        back_populates="resident_links",
        lazy="selectin",
    )


class ResidentMeter(Base):
    __tablename__ = "resident_meters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resident_id: Mapped[int] = mapped_column(ForeignKey("residents.id", ondelete="CASCADE"), nullable=False)
    resident: Mapped["Resident"] = relationship("Resident", back_populates="meters")

    meter_type: Mapped[MeterType] = mapped_column(Enum(MeterType), nullable=False)
    serial_number: Mapped[str] = mapped_column(String(100), nullable=False)

    # Опорное показание: если счётчик Б/У — вводим текущие показания; если новый — 0.
    initial_reading: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False, default=0)

    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id", ondelete="RESTRICT"), nullable=False)
    tariff: Mapped["Tariff"] = relationship("Tariff", lazy="selectin")  # Исправлено: selectin вместо joined для избежания дубликатов

    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# --- НОВОЕ: показания, логи, счета ---

class MeterReading(Base):
    __tablename__ = "meter_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resident_meter_id: Mapped[int] = mapped_column(ForeignKey("resident_meters.id", ondelete="CASCADE"), nullable=False)
    resident_meter = relationship("ResidentMeter", lazy="joined")

    reading_date: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    value: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    consumption: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)

    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id", ondelete="RESTRICT"), nullable=False)
    tariff = relationship("Tariff", lazy="joined")

    amount_net: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)   # без НДС
    vat_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    amount_vat: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    amount_total: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    # Snapshot стабильного тарифа на момент записи показания.
    # Это защищает исторические инвойсы от изменений после редактирования тарифа.
    stable_fee_net: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    stable_fee_vat: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    stable_fee_total: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by = relationship("User", foreign_keys=[created_by_id], lazy="joined")


class MeterReadingPhoto(Base):
    __tablename__ = "meter_reading_photos"
    __table_args__ = (
        UniqueConstraint("meter_reading_id", name="uq_meter_reading_photos_reading_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meter_reading_id: Mapped[int] = mapped_column(ForeignKey("meter_readings.id", ondelete="CASCADE"), nullable=False)
    meter_reading = relationship("MeterReading", lazy="joined")

    file_path: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by = relationship("User", foreign_keys=[created_by_id], lazy="joined")


class ReadingLog(Base):
    __tablename__ = "reading_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False)   # CREATE / UPDATE / DELETE
    reading_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # может быть None, если удалили
    resident_meter_id: Mapped[int] = mapped_column(ForeignKey("resident_meters.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    
    # Relationships
    resident_meter: Mapped["ResidentMeter"] = relationship("ResidentMeter", lazy="joined")
    user: Mapped["User | None"] = relationship("User", foreign_keys=[user_id], lazy="joined")


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resident_id: Mapped[int] = mapped_column(ForeignKey("residents.id", ondelete="CASCADE"), nullable=False)
    resident = relationship("Resident", lazy="joined")

    number = Column(String(64), nullable=True, index=True)  # например: INV-<resident_id>/<tenant_id>/<YYYY-MM>
    status = Column(SAEnum(InvoiceStatus, name="invoice_status"), nullable=False, default=InvoiceStatus.DRAFT)
    due_date = Column(Date, nullable=True)  # срок оплаты
    notes = Column(Text, nullable=True)  # примечание для клиента

    period_year: Mapped[int] = mapped_column(Integer, nullable=False)  # например, 2025
    period_month: Mapped[int] = mapped_column(Integer, nullable=False) # 1..12

    amount_net: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    amount_vat: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    amount_total: Mapped[float] = mapped_column(Numeric(18, 2), default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by = relationship("User", foreign_keys=[created_by_id], lazy="joined")

    lines: Mapped[list["InvoiceLine"]] = relationship(
        "InvoiceLine",
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceLine.id.asc()",
    )

    __table_args__ = (
        UniqueConstraint("resident_id", "period_year", "period_month", name="uq_invoice_resident_period"),
        UniqueConstraint("number", name="uq_invoices_number"),
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False)
    invoice: Mapped["Invoice"] = relationship("Invoice", back_populates="lines")

    # ДОЛЖНО БЫТЬ nullable=True
    meter_reading_id: Mapped[int | None] = mapped_column(
        ForeignKey("meter_readings.id", ondelete="CASCADE"),
        nullable=True
    )
    meter_reading: Mapped["MeterReading"] = relationship("MeterReading", lazy="joined")

    description: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_net: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    amount_vat: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    amount_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)


class ResidentService(Base):
    """
    Фикс-ежемесячные услуги резидента (без показаний):
    - SERVICE  : Технические услуги
    - RENT     : Аренда
    """
    __tablename__ = "resident_services"

    id: Mapped[int] = mapped_column(primary_key=True)
    resident_id: Mapped[int] = mapped_column(ForeignKey("residents.id", ondelete="CASCADE"), nullable=False)
    resident: Mapped["Resident"] = relationship("Resident", lazy="joined")

    service_type: Mapped[str] = mapped_column(String(16), nullable=False)  # 'SERVICE' | 'RENT'
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    vat_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by: Mapped["User"] = relationship("User", foreign_keys=[created_by_id], lazy="joined")


class NotificationStatus(str, enum.Enum):
    UNREAD = "UNREAD"
    READ = "READ"


class NotificationType(str, enum.Enum):
    INVOICE = "INVOICE"  # Уведомление о выставленном счете
    NEWS = "NEWS"        # Уведомление о новости
    APPEAL = "APPEAL"    # Обращение (существующий тип для обращений)
    APPEAL_UPDATE = "APPEAL_UPDATE"  # Обновление стадии обращения (пуш жителю в колокольчик)


class AppealWorkflow(str, enum.Enum):
    """Стадия обработки обращения (только для notification_type == APPEAL)."""

    UNDER_REVIEW = "UNDER_REVIEW"  # Принято / на рассмотрении
    AWAITING_CONTACT = "AWAITING_CONTACT"  # Свяжемся с вами
    TECH_DISPATCHED = "TECH_DISPATCHED"  # Назначен / выезжает специалист
    IN_PROGRESS = "IN_PROGRESS"  # Выполняются работы
    AWAITING_RESIDENT = "AWAITING_RESIDENT"  # Нужны уточнения / доступ от жителя
    RESOLVED = "RESOLVED"  # Выполнено
    CLOSED = "CLOSED"  # Закрыто без действий / дубликат


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    resident_id: Mapped[int | None] = mapped_column(ForeignKey("residents.id", ondelete="SET NULL"), nullable=True)

    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(NotificationStatus, name="notification_status"),
        default=NotificationStatus.UNREAD,
        nullable=False,
    )
    
    # Новые поля для типа уведомления и связанного объекта
    notification_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # INVOICE, NEWS, APPEAL
    related_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # ID счета, новости и т.д.
    
    # Используем функцию из .utils (импорт внутри метода или через default)
    # Но проще изменить default на текущее время Баку в самом приложении
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, default=datetime.utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    # Обращения (APPEAL): стадия обработки и комментарий для жителя
    appeal_workflow: Mapped[str | None] = mapped_column(String(40), nullable=True)
    staff_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    workflow_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    user: Mapped["User"] = relationship("User", lazy="joined")
    resident: Mapped["Resident"] = relationship("Resident", lazy="joined")


class News(Base):
    """
    Новости для отображения пользователям.
    Поддерживает мультиязычность через JSON поля.
    """
    __tablename__ = "news"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    
    # Мультиязычные поля в TEXT формате ( JSON)
    
    title: Mapped[str] = mapped_column(Text, nullable=False)  
    content: Mapped[str] = mapped_column(Text, nullable=False)  
    
    
    icon: Mapped[str] = mapped_column(String(50), nullable=False, default='info')  
    icon_color: Mapped[str] = mapped_column(String(50), nullable=False, default='#667eea')  
    # JSON list of block names (e.g., ["A","B"]), null means "all"
    target_blocks: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)  
    
    
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by = relationship("User", foreign_keys=[created_by_id], lazy="joined")


class QRToken(Base):
    """
    Одноразовые QR-токены для установки пароля новыми пользователями.
    """
    __tablename__ = "qr_tokens"
    __table_args__ = (UniqueConstraint("token", name="uq_qr_tokens_token"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    user: Mapped["User"] = relationship("User", lazy="joined")


class PaymentLog(Base):
    """
    Логи всех процессов, связанных с платежами.
    """
    __tablename__ = "payment_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"), nullable=True)
    resident_id: Mapped[int | None] = mapped_column(ForeignKey("residents.id", ondelete="SET NULL"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    
    action: Mapped[str] = mapped_column(String(50), nullable=False)  # CREATE, APPLY, ADJUST, ADVANCE_USE
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)

    payment: Mapped["Payment"] = relationship("Payment", foreign_keys=[payment_id], lazy="joined")
    resident: Mapped["Resident"] = relationship("Resident", foreign_keys=[resident_id], lazy="joined")
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id], lazy="joined")


# =====================================================
#  SALES (Satish): договора купли-продажи вилл/домов
# =====================================================

class SalesContractType(str, enum.Enum):
    FULL = "FULL"              # Tam ödənişli
    INSTALLMENT = "INSTALLMENT"  # Hissəli ödənişli


class SalesContractStatus(str, enum.Enum):
    DRAFT = "DRAFT"                           # черновик (редактируется продажником)
    PENDING_APPROVAL = "PENDING_APPROVAL"     # отправлен root-у, ещё не просмотрен
    VIEWED = "VIEWED"                         # root открыл/увидел, но не решил
    APPROVED = "APPROVED"                     # root одобрил — можно печатать
    REJECTED = "REJECTED"                     # root отклонил — вернуть на доработку
    PRINTED = "PRINTED"                       # договор распечатан (финальная стадия)


class SalesContract(Base):
    """
    Договор купли-продажи жилой площади (вилла/дом) в комплексе.
    Заполняется пользователем с ролью SALES, утверждается ROOT.
    """
    __tablename__ = "sales_contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    contract_type: Mapped[SalesContractType] = mapped_column(
        SAEnum(SalesContractType, name="sales_contract_type"),
        nullable=False,
        default=SalesContractType.FULL,
    )
    status: Mapped[SalesContractStatus] = mapped_column(
        SAEnum(SalesContractStatus, name="sales_contract_status"),
        nullable=False,
        default=SalesContractStatus.DRAFT,
    )

    # Шапка договора
    contract_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    contract_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contract_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True, default="Bakı şəhəri")

    # Покупатель
    buyer_full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    buyer_id_series: Mapped[str | None] = mapped_column(String(20), nullable=True)   # Ş/V seriyası
    buyer_id_number: Mapped[str | None] = mapped_column(String(50), nullable=True)   # Ş/V № (номер документа)
    buyer_fin: Mapped[str | None] = mapped_column(String(30), nullable=True)
    buyer_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    buyer_email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    buyer_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Объект недвижимости
    house_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    area_m2: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    price_per_m2_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    total_price_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    # Только для INSTALLMENT
    initial_payment_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    remaining_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    months_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_payment_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    # Workflow / аудит
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approval_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    viewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    printed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    printed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, onupdate=datetime.utcnow)

    created_by: Mapped["User"] = relationship("User", foreign_keys=[created_by_id], lazy="joined")
    viewed_by: Mapped["User"] = relationship("User", foreign_keys=[viewed_by_id], lazy="joined")
    reviewed_by: Mapped["User"] = relationship("User", foreign_keys=[reviewed_by_id], lazy="joined")

    installments: Mapped[list["SalesContractInstallment"]] = relationship(
        "SalesContractInstallment",
        back_populates="contract",
        cascade="all, delete-orphan",
        order_by="SalesContractInstallment.month_no",
        lazy="selectin",
    )


class SalesContractInstallment(Base):
    """
    Строка графика платежей (Əlavə №2) для договора в рассрочку.
    """
    __tablename__ = "sales_contract_installments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("sales_contracts.id", ondelete="CASCADE"),
        nullable=False,
    )
    month_no: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)

    contract: Mapped["SalesContract"] = relationship("SalesContract", back_populates="installments")
