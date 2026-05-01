from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import desc, or_

from ..database import get_db
from ..models import (
    ReadingLog,
    ResidentMeter,
    Resident,
    Block,
    User,
    MeterType,
    PaymentLog,
    RoleEnum,
)

router = APIRouter(prefix="/api/logs", tags=["logs-api"])


# Pydantic models
class ReadingLogOut(BaseModel):
    id: int
    date_time: str  # "05.12.2025, 02:23"
    action: str  # "СОЗДАНИЕ", "ОБНОВЛЕНИЕ", "УДАЛЕНИЕ"
    resident: str  # "A/5122"
    meter: str  # "Электричество"
    user: str  # "root"
    details: str  # полные детали

    class Config:
        from_attributes = True


class ReadingLogListOut(BaseModel):
    logs: List[ReadingLogOut]
    total: int
    page: int
    per_page: int
    last_page: int


class PaymentLogOut(BaseModel):
    id: int
    date_time: str
    action: str
    resident: str
    amount: float
    user: str
    details: str

    class Config:
        from_attributes = True


class PaymentLogListOut(BaseModel):
    logs: List[PaymentLogOut]
    total: int
    page: int
    per_page: int
    last_page: int


@router.get("/reading-logs", response_model=ReadingLogListOut)
def get_reading_logs(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    action: Optional[str] = Query(None),
    resident_id: Optional[int] = Query(None),
    meter_id: Optional[int] = Query(None),
    user_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    """Получить логи чтений с фильтрацией и пагинацией (публичный endpoint)."""
    try:
        allowed_roles = [RoleEnum.ROOT, RoleEnum.ADMIN, RoleEnum.OPERATOR]
        # Построение запроса с JOIN для получения связанных данных
        query = db.query(ReadingLog)\
            .options(
                joinedload(ReadingLog.resident_meter).joinedload(ResidentMeter.resident).joinedload(Resident.block),
                joinedload(ReadingLog.user)
            )\
            .join(ResidentMeter, ResidentMeter.id == ReadingLog.resident_meter_id)\
            .join(Resident, Resident.id == ResidentMeter.resident_id)\
            .outerjoin(User, User.id == ReadingLog.user_id)\
            .filter(
                or_(
                    ReadingLog.user_id.is_(None),  # системные логи
                    User.role.in_(allowed_roles)   # только ROOT/ADMIN/OPERATOR
                )
            )
        
        # Фильтры
        if action and action.upper() in ["CREATE", "UPDATE", "DELETE"]:
            query = query.filter(ReadingLog.action == action.upper())
        
        if resident_id:
            # Получаем все meter_id для этого resident
            meter_ids = [m.id for m in db.query(ResidentMeter.id).filter(
                ResidentMeter.resident_id == resident_id
            ).all()]
            if meter_ids:
                query = query.filter(ReadingLog.resident_meter_id.in_(meter_ids))
            else:
                # Если у резидента нет счётчиков, возвращаем пустой результат
                query = query.filter(ReadingLog.id == -1)  # Невозможное условие
        
        if meter_id:
            query = query.filter(ReadingLog.resident_meter_id == meter_id)
        
        if user_id:
            query = query.filter(ReadingLog.user_id == user_id)
        
        # Подсчет общего количества
        total = query.count()
        last_page = max(1, (total + per_page - 1) // per_page)
        if page > last_page:
            page = last_page
        
        # Получение данных с пагинацией, сортировка по дате (новые сначала)
        offset = (page - 1) * per_page
        logs = query.order_by(desc(ReadingLog.created_at))\
            .limit(per_page)\
            .offset(offset)\
            .all()
        
        result = []
        for log in logs:
            # Форматируем дату/время в ISO-формате для корректного отображения
            # на фронтенде с учётом локального часового пояса пользователя.
            dt = log.created_at
            date_time = dt.isoformat()
            
            # Определяем действие на русском
            action_map = {
                "CREATE": "СОЗДАНИЕ",
                "UPDATE": "ОБНОВЛЕНИЕ",
                "DELETE": "УДАЛЕНИЕ"
            }
            action = action_map.get(log.action, log.action)
            
            # Получаем информацию о резиденте
            resident_meter = log.resident_meter
            if resident_meter and resident_meter.resident:
                resident = resident_meter.resident
                block_name = resident.block.name if resident.block else ""
                unit_number = resident.unit_number or ""
                resident_code = f"{block_name}/{unit_number}" if block_name and unit_number else "—"
            else:
                resident_code = "—"
            
            # Получаем тип счётчика
            if resident_meter:
                meter_type = resident_meter.meter_type
                meter_map = {
                    MeterType.ELECTRIC: "Электричество",
                    MeterType.GAS: "Газ",
                    MeterType.WATER: "Вода",
                    MeterType.SEWERAGE: "Канализация",
                    MeterType.SERVICE: "Услуги",
                    MeterType.RENT: "Аренда",
                    MeterType.CONSTRUCTION: "Строительство"
                }
                meter = meter_map.get(meter_type, str(meter_type))
            else:
                meter = "—"
            
            # Получаем пользователя
            user = log.user
            user_name = user.username if user else "—"
            
            # Детали
            details = log.details or "—"
            
            result.append(ReadingLogOut(
                id=log.id,
                date_time=date_time,
                action=action,
                resident=resident_code,
                meter=meter,
                user=user_name,
                details=details
            ))
        
        return ReadingLogListOut(
            logs=result,
            total=total,
            page=page,
            per_page=per_page,
            last_page=last_page
        )
    except Exception as e:
        # Логируем ошибку и возвращаем пустой список с корректной структурой
        # В production можно использовать logger
        import traceback
        traceback.print_exc()
        return ReadingLogListOut(
            logs=[],
            total=0,
            page=1,
            per_page=per_page,
            last_page=1
        )


@router.get("/payment-logs", response_model=PaymentLogListOut)
def get_payment_logs(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    action: Optional[str] = Query(None),
    resident_id: Optional[int] = Query(None),
    user_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    """Получить логи платежей с фильтрацией и пагинацией."""
    try:
        allowed_roles = [RoleEnum.ROOT, RoleEnum.ADMIN, RoleEnum.OPERATOR]
        query = db.query(PaymentLog)\
            .options(
                joinedload(PaymentLog.resident).joinedload(Resident.block),
                joinedload(PaymentLog.user)
            )\
            .outerjoin(User, User.id == PaymentLog.user_id)\
            .filter(
                or_(
                    PaymentLog.user_id.is_(None),  # системные действия
                    User.role.in_(allowed_roles)   # только ROOT/ADMIN/OPERATOR
                )
            )
        
        # Фильтры
        if action:
            query = query.filter(PaymentLog.action == action.upper())
        if resident_id:
            query = query.filter(PaymentLog.resident_id == resident_id)
        if user_id:
            query = query.filter(PaymentLog.user_id == user_id)
        
        total = query.count()
        last_page = max(1, (total + per_page - 1) // per_page)
        if page > last_page:
            page = last_page
        
        offset = (page - 1) * per_page
        logs = query.order_by(desc(PaymentLog.created_at))\
            .limit(per_page)\
            .offset(offset)\
            .all()
        
        result = []
        for log in logs:
            dt = log.created_at
            date_time = dt.isoformat()
            
            action_map = {
                "CREATE": "СОЗДАНИЕ",
                "APPLY": "ПРИМЕНЕНИЕ",
                "APPLY_AUTO": "АВТО-ПРИМЕНЕНИЕ",
                "ADVANCE_USE": "ИСПОЛЬЗОВАНИЕ АВАНСА",
                "ADJUST": "КОРРЕКТИРОВКА"
            }
            action_ru = action_map.get(log.action, log.action)
            
            # Формируем строку резидента
            res_str = "—"
            if log.resident:
                block_name = log.resident.block.name if log.resident.block else ""
                res_str = f"{block_name}/{log.resident.unit_number}" if block_name else log.resident.unit_number
            
            # Формируем имя пользователя
            user_str = "Система"
            if log.user:
                user_str = log.user.full_name or log.user.username
            
            result.append(PaymentLogOut(
                id=log.id,
                date_time=date_time,
                action=action_ru,
                resident=res_str,
                amount=float(log.amount),
                user=user_str,
                details=log.details or ""
            ))
            
        return PaymentLogListOut(
            logs=result,
            total=total,
            page=page,
            per_page=per_page,
            last_page=last_page
        )
    except Exception as e:
        print(f"Error in get_payment_logs: {e}")
        return PaymentLogListOut(
            logs=[],
            total=0,
            page=1,
            per_page=per_page,
            last_page=1
        )

