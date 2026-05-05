from typing import List, Optional
from datetime import datetime, date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, and_

from ..database import get_db
from ..models import (
    User, Resident, Payment, Invoice, Block,
    Notification, NotificationStatus, PaymentMethod, PaymentApplication, InvoiceStatus,
    MeterReading, ResidentMeter, MeterType, Tariff, RoleEnum
)
from ..utils import now_baku, to_baku_datetime
from ..deps import require_any_role

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-api"])


# Pydantic models
class DashboardStatsOut(BaseModel):
    totalUsers: int
    totalBuildings: int
    totalResidents: int
    monthlyPayments: float
    activeRequests: int
    totalPayments: int
    totalInvoices: int
    unpaidInvoices: int
    monthly_kwh: float = 0.0
    monthly_water_m3: float = 0.0
    monthly_gas_m3: float = 0.0
    monthly_electricity_amount: float = 0.0
    monthly_water_amount: float = 0.0
    monthly_gas_amount: float = 0.0

    class Config:
        from_attributes = True


class RecentPaymentOut(BaseModel):
    id: int
    resident_code: str
    resident_info: str
    amount_total: float
    received_at: datetime
    method: str
    status: str

    class Config:
        from_attributes = True


class RecentActivityOut(BaseModel):
    id: int
    type: str  # "payment", "invoice", "reading", "notification"
    title: str
    description: str
    time: str
    icon: str  # "success", "warning", "info"
    raw_time: Optional[datetime] = None

    class Config:
        from_attributes = True


class PaymentChartDataOut(BaseModel):
    labels: List[str]
    data: List[float]
    period: str  # "week", "month", "year"


@router.get("/stats")
def get_dashboard_stats(
    db: Session = Depends(get_db),
    _staff_user: User = Depends(require_any_role(RoleEnum.ROOT, RoleEnum.ADMIN, RoleEnum.OPERATOR)),
):
    """Получить статистику для dashboard (только staff)."""
    try:
        # Общее количество пользователей
        total_users = db.query(func.count(User.id)).scalar() or 0
        
        # Общее количество зданий (блоков)
        total_buildings = db.query(func.count(Block.id)).scalar() or 0
        
        # Общее количество резидентов
        total_residents = db.query(func.count(Resident.id)).scalar() or 0
        
        # Платежи за текущий месяц
        now = now_baku()
        month_start = date(now.year, now.month, 1)
        monthly_payments_query = db.query(func.sum(Payment.amount_total)).filter(
            Payment.received_at >= month_start
        )
        monthly_payments = float(monthly_payments_query.scalar() or 0)
        
        # Активные заявки (непрочитанные уведомления)
        active_requests = db.query(func.count(Notification.id)).filter(
            Notification.status == NotificationStatus.UNREAD
        ).scalar() or 0
        
        # Общее количество платежей
        total_payments = db.query(func.count(Payment.id)).scalar() or 0
        
        # Общее количество счетов
        total_invoices = db.query(func.count(Invoice.id)).scalar() or 0
        
        # Неоплаченные счета (статус не "paid")
        from ..models import InvoiceStatus
        unpaid_invoices = db.query(func.count(Invoice.id)).filter(
            Invoice.status != InvoiceStatus.PAID
        ).scalar() or 0
        
        # Calculate monthly utility consumptions (electricity, water, gas) for all residents
        monthly_kwh = Decimal("0")
        monthly_water_m3 = Decimal("0")
        monthly_gas_m3 = Decimal("0")
        
        today_now = now_baku()
        month_start = datetime(today_now.year, today_now.month, 1)
        month_end = datetime(today_now.year + (1 if today_now.month == 12 else 0), 
                             (1 if today_now.month == 12 else today_now.month + 1), 1)
        
        # Electricity consumption
        electric_readings = (
            db.query(MeterReading)
            .join(ResidentMeter, ResidentMeter.id == MeterReading.resident_meter_id)
            .filter(
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
                ResidentMeter.meter_type == MeterType.GAS,
                MeterReading.reading_date >= month_start,
                MeterReading.reading_date < month_end
            )
            .all()
        )
        monthly_gas_m3 = sum(Decimal(str(rd.consumption or 0)) for rd in gas_readings)
        
        # Calculate monthly utility amounts (money) for all residents
        # Import compute_amount function from API readings router
        from .api_readings import compute_amount
        
        monthly_electricity_amount = Decimal("0")
        monthly_water_amount = Decimal("0")
        monthly_gas_amount = Decimal("0")
        
        # Calculate electricity amount
        for rd in electric_readings:
            if rd.resident_meter and rd.resident_meter.tariff_id:
                tariff = db.get(Tariff, rd.resident_meter.tariff_id)
                if tariff:
                    consumption = Decimal(str(rd.consumption or 0))
                    _, _, amount_total, _ = compute_amount(consumption, tariff)
                    monthly_electricity_amount += amount_total
        
        # Calculate water amount
        for rd in water_readings:
            if rd.resident_meter and rd.resident_meter.tariff_id:
                tariff = db.get(Tariff, rd.resident_meter.tariff_id)
                if tariff:
                    consumption = Decimal(str(rd.consumption or 0))
                    _, _, amount_total, _ = compute_amount(consumption, tariff)
                    monthly_water_amount += amount_total
        
        # Calculate gas amount
        for rd in gas_readings:
            if rd.resident_meter and rd.resident_meter.tariff_id:
                tariff = db.get(Tariff, rd.resident_meter.tariff_id)
                if tariff:
                    consumption = Decimal(str(rd.consumption or 0))
                    _, _, amount_total, _ = compute_amount(consumption, tariff)
                    monthly_gas_amount += amount_total
        
        return DashboardStatsOut(
            totalUsers=total_users,
            totalBuildings=total_buildings,
            totalResidents=total_residents,
            monthlyPayments=monthly_payments,
            activeRequests=active_requests,
            totalPayments=total_payments,
            totalInvoices=total_invoices,
            unpaidInvoices=unpaid_invoices,
            monthly_kwh=float(monthly_kwh),
            monthly_water_m3=float(monthly_water_m3),
            monthly_gas_m3=float(monthly_gas_m3),
            monthly_electricity_amount=float(monthly_electricity_amount),
            monthly_water_amount=float(monthly_water_amount),
            monthly_gas_amount=float(monthly_gas_amount)
        )
    except Exception as e:
        return {"error": str(e)}


@router.get("/recent-payments")
def get_recent_payments(
    limit: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
    _staff_user: User = Depends(require_any_role(RoleEnum.ROOT, RoleEnum.ADMIN, RoleEnum.OPERATOR)),
):
    """Получить последние платежи (только staff)."""
    try:
        payments = db.query(Payment).join(Resident, Resident.id == Payment.resident_id).order_by(
            Payment.received_at.desc()
        ).limit(limit).all()

        resident_ids = list({p.resident_id for p in payments if p.resident_id is not None})

        outstanding_by_resident: dict[int, Decimal] = {}
        if resident_ids:
            open_invoice_totals = {
                int(resident_id): Decimal(str(total_amount or 0))
                for resident_id, total_amount in db.query(
                    Invoice.resident_id,
                    func.coalesce(func.sum(Invoice.amount_total), 0)
                ).filter(
                    Invoice.resident_id.in_(resident_ids),
                    Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL])
                ).group_by(Invoice.resident_id).all()
            }

            applied_to_open_invoices = {
                int(resident_id): Decimal(str(applied_amount or 0))
                for resident_id, applied_amount in db.query(
                    Invoice.resident_id,
                    func.coalesce(func.sum(PaymentApplication.amount_applied), 0)
                ).join(Invoice, Invoice.id == PaymentApplication.invoice_id).filter(
                    Invoice.resident_id.in_(resident_ids),
                    Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIAL])
                ).group_by(Invoice.resident_id).all()
            }

            for resident_id in resident_ids:
                total_open = open_invoice_totals.get(resident_id, Decimal("0"))
                total_applied = applied_to_open_invoices.get(resident_id, Decimal("0"))
                remaining = total_open - total_applied
                outstanding_by_resident[resident_id] = remaining if remaining > Decimal("0") else Decimal("0")
        
        result = []
        for payment in payments:
            block_name = payment.resident.block.name if payment.resident.block else None
            unit_number = payment.resident.unit_number
            resident_code = f"{block_name} / {unit_number}" if block_name and unit_number else "—"
            resident_info = f"Блок {block_name}, №{unit_number}" if block_name and unit_number else "—"
            
            # Определяем статус для блока "Последние платежи":
            # - полностью погашено: после этого платежа у резидента нет открытого долга
            # - частично погашено: платеж разнесен, но открытый долг еще есть
            # - аванс зачислен: платеж ушел в аванс (не разнесен по начислениям)
            applied_total = sum([app.amount_applied for app in payment.applications], Decimal("0"))
            tolerance = Decimal("0.01")
            outstanding_after_payment = outstanding_by_resident.get(payment.resident_id, Decimal("0"))

            if applied_total <= tolerance:
                status = "Оплачено (аванс зачислен)"
            elif outstanding_after_payment <= tolerance:
                status = "Оплачено (полностью погашено)"
            else:
                status = "Оплачено (частично погашено)"
            
            result.append(RecentPaymentOut(
                id=payment.id,
                resident_code=resident_code,
                resident_info=resident_info,
                amount_total=float(payment.amount_total),
                received_at=to_baku_datetime(payment.received_at),
                method=payment.method.value if isinstance(payment.method, PaymentMethod) else (str(payment.method) if payment.method else '—'),
                status=status
            ))
        
        return {"payments": result}
    except Exception as e:
        return {"error": str(e)}


@router.get("/recent-activity")
def get_recent_activity(
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _staff_user: User = Depends(require_any_role(RoleEnum.ROOT, RoleEnum.ADMIN, RoleEnum.OPERATOR)),
):
    """Получить последнюю активность (только staff)."""
    try:
        activities = []
        
        # Последние платежи
        recent_payments = db.query(Payment).order_by(Payment.received_at.desc()).limit(limit).all()
        for payment in recent_payments:
            block_name = payment.resident.block.name if payment.resident.block else None
            unit_number = payment.resident.unit_number
            resident_info = f"Квартира #{unit_number}" if unit_number else "Квартира"
            if block_name:
                resident_info = f"{resident_info} (Блок {block_name})"
            
            dt_baku = to_baku_datetime(payment.received_at)
            time_ago = get_time_ago(dt_baku)
            activities.append(RecentActivityOut(
                id=payment.id,
                type="payment",
                title="Новый платеж получен",
                description=f"{resident_info} - ₼{payment.amount_total:.2f}",
                time=time_ago,
                icon="success",
                raw_time=dt_baku
            ))
        
        # Последние уведомления
        recent_notifications = db.query(Notification)\
            .options(joinedload(Notification.resident).joinedload(Resident.block))\
            .order_by(Notification.created_at.desc())\
            .limit(limit)\
            .all()
        for notif in recent_notifications:
            dt_baku = to_baku_datetime(notif.created_at)
            time_ago = get_time_ago(dt_baku)
            
            # Формируем описание: блок и номер дома, если есть
            description = "Новое обращение"
            if notif.resident:
                block_name = notif.resident.block.name if notif.resident.block else None
                unit_number = notif.resident.unit_number
                if block_name and unit_number:
                    description = f"Блок {block_name}, №{unit_number}"
                elif unit_number:
                    description = f"№{unit_number}"
                elif block_name:
                    description = f"Блок {block_name}"
            
            activities.append(RecentActivityOut(
                id=notif.id,
                type="notification",
                title="Новое обращение",
                description=description,
                time=time_ago,
                icon="warning" if notif.status == NotificationStatus.UNREAD else "info",
                raw_time=dt_baku
            ))
        
        # Сортируем по времени (самые новые первые) и берем limit
        activities.sort(key=lambda x: x.raw_time or datetime.min, reverse=True)
        activities = activities[:limit]
        
        return {"activities": activities}
    except Exception as e:
        return {"error": str(e)}


@router.get("/payment-chart")
def get_payment_chart_data(
    period: str = Query("week", regex="^(day|week|month|year)$"),
    db: Session = Depends(get_db),
    _staff_user: User = Depends(require_any_role(RoleEnum.ROOT, RoleEnum.ADMIN, RoleEnum.OPERATOR)),
):
    """Получить данные для графика платежей (только staff)."""
    try:
        now = now_baku()
        labels = []
        data = []
        
        if period == "day":
            # Данные за текущий день по часам (24 точки)
            current_day = now.date()
            for hour in range(24):
                labels.append(f"{hour:02d}:00")

                hour_start = datetime.combine(
                    current_day,
                    datetime.min.time().replace(hour=hour),
                    tzinfo=now.tzinfo
                )
                hour_end = hour_start + timedelta(hours=1) - timedelta(microseconds=1)

                total = db.query(func.sum(Payment.amount_total)).filter(
                    and_(
                        Payment.received_at >= hour_start,
                        Payment.received_at <= hour_end
                    )
                ).scalar() or Decimal("0")
                data.append(float(total))

        elif period == "week":
            # Данные за последние 7 дней
            labels = []
            data = []
            for i in range(6, -1, -1):
                day = now.date() - timedelta(days=i)
                labels.append(day.strftime("%d.%m"))
                
                day_start = datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo)
                day_end = datetime.combine(day, datetime.max.time(), tzinfo=now.tzinfo)
                
                total = db.query(func.sum(Payment.amount_total)).filter(
                    and_(
                        Payment.received_at >= day_start,
                        Payment.received_at <= day_end
                    )
                ).scalar() or Decimal("0")
                data.append(float(total))
        
        elif period == "month":
            # Данные за последние 4 недели
            labels = []
            data = []
            for i in range(3, -1, -1):
                week_start = now.date() - timedelta(days=(i * 7) + 6)
                week_end = now.date() - timedelta(days=i * 7)
                labels.append(f"Нед {4-i}")
                
                week_start_dt = datetime.combine(week_start, datetime.min.time(), tzinfo=now.tzinfo)
                week_end_dt = datetime.combine(week_end, datetime.max.time(), tzinfo=now.tzinfo)
                
                total = db.query(func.sum(Payment.amount_total)).filter(
                    and_(
                        Payment.received_at >= week_start_dt,
                        Payment.received_at <= week_end_dt
                    )
                ).scalar() or Decimal("0")
                data.append(float(total))
        
        elif period == "year":
            # Данные за последние 12 месяцев
            labels = []
            data = []
            month_names = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
            current_month_idx = now.year * 12 + (now.month - 1)
            for i in range(11, -1, -1):
                month_idx = current_month_idx - i
                year = month_idx // 12
                month = (month_idx % 12) + 1
                labels.append(month_names[month - 1])

                month_start = date(year, month, 1)
                if month == 12:
                    month_end = date(year + 1, 1, 1) - timedelta(days=1)
                else:
                    month_end = date(year, month + 1, 1) - timedelta(days=1)
                
                month_start_dt = datetime.combine(month_start, datetime.min.time(), tzinfo=now.tzinfo)
                month_end_dt = datetime.combine(month_end, datetime.max.time(), tzinfo=now.tzinfo)
                
                total = db.query(func.sum(Payment.amount_total)).filter(
                    and_(
                        Payment.received_at >= month_start_dt,
                        Payment.received_at <= month_end_dt
                    )
                ).scalar() or Decimal("0")
                data.append(float(total))
        
        return PaymentChartDataOut(
            labels=labels,
            data=data,
            period=period
        )
    except Exception as e:
        return {"error": str(e)}


def get_time_ago(dt: datetime) -> str:
    """Получить строку "X минут/часов/дней назад"."""
    dt = to_baku_datetime(dt)
    now = now_baku()
    diff = now - dt
    
    # Если событие из будущего (из-за рассинхрона времени) или только что
    if diff.total_seconds() < 60:
        return "Только что"
    
    if diff.days > 0:
        return f"{diff.days} {pluralize(diff.days, 'день', 'дня', 'дней')} назад"
    
    hours = diff.seconds // 3600
    if hours >= 1:
        return f"{hours} {pluralize(hours, 'час', 'часа', 'часов')} назад"
    
    minutes = diff.seconds // 60
    return f"{minutes} {pluralize(minutes, 'минуту', 'минуты', 'минут')} назад"


def pluralize(n: int, one: str, few: str, many: str) -> str:
    """Русская плюрализация."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    elif 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return few
    else:
        return many

