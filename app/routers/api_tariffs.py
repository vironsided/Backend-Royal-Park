from typing import List, Optional
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
import json

from ..database import get_db
from ..models import Tariff, TariffStep, MeterType, CustomerType, ResidentMeter, MeterReading
from ..deps import get_current_user
from ..models import User

router = APIRouter(prefix="/api/tariffs", tags=["tariffs-api"])
DISABLED_TARIFF_TYPES = {MeterType.SEWERAGE.value}


class TariffStepOut(BaseModel):
    id: int
    from_value: Optional[float] = None
    to_value: Optional[float] = None
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    price: float

    class Config:
        from_attributes = True


class TariffStepCreate(BaseModel):
    from_value: Optional[float] = None
    to_value: Optional[float] = None
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    price: float


class TariffOut(BaseModel):
    id: int
    name: str
    meter_type: str
    customer_type: str
    vat_percent: int
    use_multiplier: bool = False
    consumption_multiplier: float = 1.0
    stable_tariff: float = 0.0
    sewerage_percent: float = 0.0
    is_active: bool
    created_at: datetime
    updated_at: datetime
    created_by_id: Optional[int] = None
    steps: List[TariffStepOut] = []

    class Config:
        from_attributes = True


class TariffCreate(BaseModel):
    name: str
    meter_type: str
    customer_type: str
    vat_percent: int
    use_multiplier: Optional[bool] = False
    consumption_multiplier: Optional[float] = 1.0
    stable_tariff: Optional[float] = 0.0
    sewerage_percent: Optional[float] = 0.0
    steps: List[TariffStepCreate]


class TariffUpdate(BaseModel):
    name: Optional[str] = None
    meter_type: Optional[str] = None
    customer_type: Optional[str] = None
    vat_percent: Optional[int] = None
    use_multiplier: Optional[bool] = None
    consumption_multiplier: Optional[float] = None
    stable_tariff: Optional[float] = None
    sewerage_percent: Optional[float] = None
    is_active: Optional[bool] = None
    steps: Optional[List[TariffStepCreate]] = None


def _to_decimal(value: object) -> Decimal:
    """Безопасно приводит вход к Decimal."""
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# Helpers used by admin HTML routes too.
# Source-of-truth should live in api_*.py (even if duplicated elsewhere).
# ---------------------------------------------------------------------------
def _parse_steps_json(raw: str):
    """
    Parse tier steps JSON for non-CONSTRUCTION tariffs.
    Format: [{"from": 0, "to": 100, "price": 0.1}, ...]
    Returns: List[(from: Decimal, to: Optional[Decimal], price: Decimal)]
    """
    try:
        data = json.loads(raw or "[]")
        assert isinstance(data, list)
    except Exception:
        raise ValueError("invalid_json")

    steps = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"invalid_step_{i}")

        try:
            f = _to_decimal(item.get("from"))
            t_raw = item.get("to")
            t = None if (t_raw is None or str(t_raw).strip() == "") else _to_decimal(t_raw)
            p = _to_decimal(item.get("price"))
        except (InvalidOperation, Exception):
            raise ValueError(f"invalid_step_{i}")

        if f < 0 or p < 0:
            raise ValueError(f"negative_values_{i}")
        if t is not None and t <= f:
            raise ValueError(f"range_order_{i}")

        steps.append((f, t, p))

    if not steps:
        raise ValueError("empty_steps")

    for i in range(len(steps) - 1):
        _f1, t1, _p1 = steps[i]
        f2, _t2, _p2 = steps[i + 1]
        if t1 is None:
            raise ValueError("infinite_not_last")
        if f2 != t1:
            raise ValueError("not_continuous")

    return steps


def _parse_construction_steps_json(raw: str):
    """
    Parse steps for CONSTRUCTION tariffs (date ranges).
    Format: [{"from_date":"YYYY-MM-DD","to_date":"YYYY-MM-DD","price":1000.00}, ...]
    Returns: List[(from_date: date, to_date: date, price: Decimal)]
    """
    try:
        data = json.loads(raw or "[]")
        assert isinstance(data, list)
    except Exception:
        raise ValueError("invalid_json")

    if not data:
        raise ValueError("empty_steps")

    steps = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"invalid_step_{i}")

        from_raw = item.get("from_date")
        to_raw = item.get("to_date")
        if not from_raw or not to_raw:
            raise ValueError(f"invalid_dates_{i}")
        try:
            start = datetime.strptime(from_raw, "%Y-%m-%d").date()
            end = datetime.strptime(to_raw, "%Y-%m-%d").date()
        except Exception:
            raise ValueError(f"invalid_dates_{i}")
        if end < start:
            raise ValueError(f"date_order_{i}")

        try:
            price = _to_decimal(item.get("price"))
        except (InvalidOperation, Exception):
            raise ValueError(f"invalid_step_{i}")
        if price < 0:
            raise ValueError(f"negative_values_{i}")

        steps.append((start, end, price))

    return steps


def _ensure_tariff_type_allowed(meter_type: str) -> None:
    if meter_type in DISABLED_TARIFF_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Тариф 'Канализация' создается автоматически от воды и не может быть создан вручную",
        )


def _parse_steps(steps_data: List[TariffStepCreate], meter_type: str) -> List[tuple]:
    """Валидация и преобразование ступеней."""
    if not steps_data:
        raise ValueError("empty_steps")
    
    is_construction = meter_type == MeterType.CONSTRUCTION.value
    steps = []
    
    for i, step in enumerate(steps_data):
        if is_construction:
            if not step.from_date or not step.to_date:
                raise ValueError(f"invalid_dates_{i}")
            if step.to_date < step.from_date:
                raise ValueError(f"date_order_{i}")
            if step.price < 0:
                raise ValueError(f"negative_values_{i}")
            steps.append((step.from_date, step.to_date, _to_decimal(step.price)))
        else:
            f = _to_decimal(step.from_value or 0)
            t = None if step.to_value is None else _to_decimal(step.to_value)
            p = _to_decimal(step.price)
            
            if f < 0 or p < 0:
                raise ValueError(f"negative_values_{i}")
            if t is not None and t <= f:
                raise ValueError(f"range_order_{i}")
            
            steps.append((f, t, p))
    
    # Проверка непрерывности (для не-construction)
    if not is_construction:
        for i in range(len(steps) - 1):
            f1, t1, _ = steps[i]
            f2, _t2, _ = steps[i + 1]
            if t1 is None:
                raise ValueError("infinite_not_last")
            if f2 != t1:
                raise ValueError("not_continuous")
    
    return steps


# ВРЕМЕННО: endpoint без авторизации для теста (должен быть ПЕРЕД динамическими маршрутами!)
@router.get("/public", response_model=List[TariffOut])
@router.get("/public/", response_model=List[TariffOut])
def list_tariffs_public(
    db: Session = Depends(get_db),
    meter: Optional[str] = None,
    ctype: Optional[str] = None,
    q: Optional[str] = None,
    include_inactive: bool = False,
):
    """ВРЕМЕННЫЙ endpoint без авторизации для теста SPA."""
    stmt = select(Tariff)
    if not include_inactive:
        stmt = stmt.where(Tariff.is_active == True)  # noqa: E712
    query = db.execute(stmt).scalars().all()
    
    if meter and meter in {m.value for m in MeterType}:
        query = [t for t in query if t.meter_type == MeterType(meter)]
    
    if ctype and ctype in {c.value for c in CustomerType}:
        query = [t for t in query if t.customer_type == CustomerType(ctype)]
    
    if q:
        needle = q.strip().lower()
        query = [t for t in query if needle in t.name.lower()]
    
    # Новые тарифы должны идти первыми, поэтому сортируем по id по убыванию.
    tariffs = sorted(query, key=lambda t: t.id, reverse=True)
    return tariffs


@router.get("/{tariff_id}/public", response_model=TariffOut)
def get_tariff_public(
    tariff_id: int,
    db: Session = Depends(get_db),
):
    """ВРЕМЕННЫЙ endpoint без авторизации для теста."""
    tariff = db.get(Tariff, tariff_id)
    if not tariff:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тариф не найден")
    return tariff


@router.get("", response_model=List[TariffOut])
@router.get("/", response_model=List[TariffOut])
def list_tariffs_api(
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
    meter: Optional[str] = None,
    ctype: Optional[str] = None,
    q: Optional[str] = None,
    include_inactive: bool = False,
):
    """
    JSON-список тарифов для SPA-админки с фильтрами.
    """
    stmt = select(Tariff)
    if not include_inactive:
        stmt = stmt.where(Tariff.is_active == True)  # noqa: E712
    query = db.execute(stmt).scalars().all()
    
    if meter and meter in {m.value for m in MeterType}:
        query = [t for t in query if t.meter_type == MeterType(meter)]
    
    if ctype and ctype in {c.value for c in CustomerType}:
        query = [t for t in query if t.customer_type == CustomerType(ctype)]
    
    if q:
        needle = q.strip().lower()
        query = [t for t in query if needle in t.name.lower()]
    
    # Новые тарифы должны идти первыми, поэтому сортируем по id по убыванию.
    tariffs = sorted(query, key=lambda t: t.id, reverse=True)
    return tariffs


@router.get("/{tariff_id}", response_model=TariffOut)
def get_tariff_api(
    tariff_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """Получение одного тарифа по ID."""
    tariff = db.get(Tariff, tariff_id)
    if not tariff:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тариф не найден")
    return tariff


# ВРЕМЕННО: endpoint без авторизации для создания (должен быть ПЕРЕД обычным!)
@router.post("/public", response_model=TariffOut, status_code=status.HTTP_201_CREATED)
def create_tariff_public(
    payload: TariffCreate,
    db: Session = Depends(get_db),
):
    """ВРЕМЕННЫЙ endpoint без авторизации для теста SPA."""
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Название не может быть пустым")
    
    if payload.meter_type not in {m.value for m in MeterType}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неверный тип счётчика")
    _ensure_tariff_type_allowed(payload.meter_type)
    
    if payload.customer_type not in {c.value for c in CustomerType}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неверный тип клиента")
    
    if not (0 <= payload.vat_percent <= 100):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="НДС должен быть от 0 до 100")

    stable_tariff = float(payload.stable_tariff or 0)
    if stable_tariff < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Стабильный тариф не может быть отрицательным")
    if payload.meter_type not in {MeterType.ELECTRIC.value, MeterType.GAS.value}:
        stable_tariff = 0.0
    stable_tariff_dec = _to_decimal(stable_tariff)
    use_multiplier = bool(payload.use_multiplier or False)
    if payload.meter_type != MeterType.ELECTRIC.value:
        use_multiplier = False
    multiplier_raw = float(payload.consumption_multiplier or 1)
    if multiplier_raw <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Коэффициент должен быть больше 0")
    if not use_multiplier:
        multiplier_raw = 1.0
    consumption_multiplier_dec = _to_decimal(multiplier_raw)

    sewerage_percent = float(payload.sewerage_percent or 0)
    if sewerage_percent < 0 or sewerage_percent > 100:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Процент канализации должен быть от 0 до 100")
    if payload.meter_type != MeterType.WATER.value:
        sewerage_percent = 0.0
    
    exists = db.execute(
        select(Tariff.id).where(
            func.lower(Tariff.name) == func.lower(name),
            Tariff.meter_type == MeterType(payload.meter_type),
            Tariff.customer_type == CustomerType(payload.customer_type),
        )
    ).first()
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Тариф с таким названием уже существует")
    
    try:
        steps = _parse_steps(payload.steps, payload.meter_type)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Ошибка валидации ступеней: {e}")
    
    try:
        tariff = Tariff(
            name=name,
            meter_type=MeterType(payload.meter_type),
            customer_type=CustomerType(payload.customer_type),
            vat_percent=payload.vat_percent,
            use_multiplier=use_multiplier,
            consumption_multiplier=consumption_multiplier_dec,
            stable_tariff=stable_tariff_dec,
            sewerage_percent=sewerage_percent,
            created_by_id=None,
        )
        db.add(tariff)
        db.flush()
        
        is_construction = payload.meter_type == MeterType.CONSTRUCTION.value
        if is_construction:
            for start, end, price in steps:
                db.add(TariffStep(
                    tariff_id=tariff.id,
                    from_date=start,
                    to_date=end,
                    price=price,
                ))
        else:
            for f, t, p in steps:
                db.add(TariffStep(
                    tariff_id=tariff.id,
                    from_value=float(f),
                    to_value=float(t) if t is not None else None,
                    price=float(p),
                ))
        
        db.commit()
        db.refresh(tariff)
        return tariff
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Ошибка создания тарифа: {str(e)}")


@router.post("", response_model=TariffOut, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=TariffOut, status_code=status.HTTP_201_CREATED)
def create_tariff_api(
    payload: TariffCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """Создание тарифа из SPA."""
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Название не может быть пустым")
    
    if payload.meter_type not in {m.value for m in MeterType}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неверный тип счётчика")
    _ensure_tariff_type_allowed(payload.meter_type)
    
    if payload.customer_type not in {c.value for c in CustomerType}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неверный тип клиента")
    
    if not (0 <= payload.vat_percent <= 100):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="НДС должен быть от 0 до 100")

    stable_tariff = float(payload.stable_tariff or 0)
    if stable_tariff < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Стабильный тариф не может быть отрицательным")
    if payload.meter_type not in {MeterType.ELECTRIC.value, MeterType.GAS.value}:
        stable_tariff = 0.0
    stable_tariff_dec = _to_decimal(stable_tariff)
    use_multiplier = bool(payload.use_multiplier or False)
    if payload.meter_type != MeterType.ELECTRIC.value:
        use_multiplier = False
    multiplier_raw = float(payload.consumption_multiplier or 1)
    if multiplier_raw <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Коэффициент должен быть больше 0")
    if not use_multiplier:
        multiplier_raw = 1.0
    consumption_multiplier_dec = _to_decimal(multiplier_raw)

    sewerage_percent = float(payload.sewerage_percent or 0)
    if sewerage_percent < 0 or sewerage_percent > 100:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Процент канализации должен быть от 0 до 100")
    if payload.meter_type != MeterType.WATER.value:
        sewerage_percent = 0.0
    
    # Проверка уникальности
    exists = db.execute(
        select(Tariff.id).where(
            func.lower(Tariff.name) == func.lower(name),
            Tariff.meter_type == MeterType(payload.meter_type),
            Tariff.customer_type == CustomerType(payload.customer_type),
        )
    ).first()
    if exists:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Тариф с таким названием уже существует")
    
    try:
        steps = _parse_steps(payload.steps, payload.meter_type)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Ошибка валидации ступеней: {e}")
    
    try:
        tariff = Tariff(
            name=name,
            meter_type=MeterType(payload.meter_type),
            customer_type=CustomerType(payload.customer_type),
            vat_percent=payload.vat_percent,
            use_multiplier=use_multiplier,
            consumption_multiplier=consumption_multiplier_dec,
            stable_tariff=stable_tariff_dec,
            sewerage_percent=sewerage_percent,
            created_by_id=actor.id,
        )
        db.add(tariff)
        db.flush()
        
        is_construction = payload.meter_type == MeterType.CONSTRUCTION.value
        if is_construction:
            for start, end, price in steps:
                db.add(TariffStep(
                    tariff_id=tariff.id,
                    from_date=start,
                    to_date=end,
                    price=price,
                ))
        else:
            for f, t, p in steps:
                db.add(TariffStep(
                    tariff_id=tariff.id,
                    from_value=float(f),
                    to_value=float(t) if t is not None else None,
                    price=float(p),
                ))
        
        db.commit()
        db.refresh(tariff)
        return tariff
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Ошибка создания тарифа: {str(e)}")




@router.put("/{tariff_id}", response_model=TariffOut)
def update_tariff_api(
    tariff_id: int,
    payload: TariffUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """Обновление тарифа."""
    tariff = db.get(Tariff, tariff_id)
    if not tariff:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тариф не найден")
    
    meter_type = payload.meter_type or tariff.meter_type.value
    if payload.meter_type is not None:
        _ensure_tariff_type_allowed(payload.meter_type)
    
    if payload.name is not None:
        name = (payload.name or "").strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Название не может быть пустым")
        
        exists = db.execute(
            select(Tariff.id).where(
                Tariff.id != tariff_id,
                func.lower(Tariff.name) == func.lower(name),
                Tariff.meter_type == MeterType(meter_type),
                Tariff.customer_type == CustomerType(payload.customer_type or tariff.customer_type.value),
            )
        ).first()
        if exists:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Тариф с таким названием уже существует")
        
        tariff.name = name
    
    if payload.meter_type is not None:
        tariff.meter_type = MeterType(payload.meter_type)
    
    if payload.customer_type is not None:
        tariff.customer_type = CustomerType(payload.customer_type)
    
    if payload.vat_percent is not None:
        if not (0 <= payload.vat_percent <= 100):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="НДС должен быть от 0 до 100")
        tariff.vat_percent = payload.vat_percent

    effective_meter_type = payload.meter_type or tariff.meter_type.value

    if payload.use_multiplier is not None:
        tariff.use_multiplier = bool(payload.use_multiplier)
    if effective_meter_type != MeterType.ELECTRIC.value:
        tariff.use_multiplier = False
        tariff.consumption_multiplier = _to_decimal(1)
    if payload.use_multiplier is not None:
        if not tariff.use_multiplier:
            tariff.consumption_multiplier = _to_decimal(1)
    if payload.consumption_multiplier is not None:
        cm = float(payload.consumption_multiplier)
        if cm <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Коэффициент должен быть больше 0")
        tariff.consumption_multiplier = _to_decimal(cm)
    if bool(getattr(tariff, "use_multiplier", False)) and (getattr(tariff, "consumption_multiplier", _to_decimal(1)) or _to_decimal(1)) <= 0:
        tariff.consumption_multiplier = _to_decimal(1)

    # ELECTRIC/GAS-only: фиксированная часть
    if payload.stable_tariff is not None:
        st = float(payload.stable_tariff or 0)
        if st < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Стабильный тариф не может быть отрицательным")
        tariff.stable_tariff = _to_decimal(st) if effective_meter_type in {MeterType.ELECTRIC.value, MeterType.GAS.value} else _to_decimal(0)
    else:
        if effective_meter_type not in {MeterType.ELECTRIC.value, MeterType.GAS.value}:
            tariff.stable_tariff = _to_decimal(0)

    # WATER-only: процент канализации
    if payload.sewerage_percent is not None:
        sp = float(payload.sewerage_percent or 0)
        if sp < 0 or sp > 100:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Процент канализации должен быть от 0 до 100")
        tariff.sewerage_percent = sp if effective_meter_type == MeterType.WATER.value else 0.0
    else:
        # Если тариф перестал быть WATER — сбросим
        if effective_meter_type != MeterType.WATER.value:
            tariff.sewerage_percent = 0.0
    
    if payload.is_active is not None:
        tariff.is_active = payload.is_active
    
    if payload.steps is not None:
        try:
            steps = _parse_steps(payload.steps, meter_type)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Ошибка валидации ступеней: {e}")
        
        # Удаляем старые ступени
        db.query(TariffStep).filter(TariffStep.tariff_id == tariff_id).delete()
        
        # Добавляем новые
        is_construction = meter_type == MeterType.CONSTRUCTION.value
        if is_construction:
            for start, end, price in steps:
                db.add(TariffStep(
                    tariff_id=tariff.id,
                    from_date=start,
                    to_date=end,
                    price=price,
                ))
        else:
            for f, t, p in steps:
                db.add(TariffStep(
                    tariff_id=tariff.id,
                    from_value=float(f),
                    to_value=float(t) if t is not None else None,
                    price=float(p),
                ))
    
    db.commit()
    db.refresh(tariff)
    return tariff


# ВРЕМЕННО: публичный endpoint для обновления
@router.put("/{tariff_id}/public", response_model=TariffOut)
def update_tariff_public(
    tariff_id: int,
    payload: TariffUpdate,
    db: Session = Depends(get_db),
):
    """ВРЕМЕННЫЙ endpoint без авторизации для теста."""
    tariff = db.get(Tariff, tariff_id)
    if not tariff:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тариф не найден")
    
    meter_type = payload.meter_type or tariff.meter_type.value
    if payload.meter_type is not None:
        _ensure_tariff_type_allowed(payload.meter_type)
    
    if payload.name is not None:
        name = (payload.name or "").strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Название не может быть пустым")
        
        exists = db.execute(
            select(Tariff.id).where(
                Tariff.id != tariff_id,
                func.lower(Tariff.name) == func.lower(name),
                Tariff.meter_type == MeterType(meter_type),
                Tariff.customer_type == CustomerType(payload.customer_type or tariff.customer_type.value),
            )
        ).first()
        if exists:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Тариф с таким названием уже существует")
        
        tariff.name = name
    
    if payload.meter_type is not None:
        tariff.meter_type = MeterType(payload.meter_type)
    
    if payload.customer_type is not None:
        tariff.customer_type = CustomerType(payload.customer_type)
    
    if payload.vat_percent is not None:
        if not (0 <= payload.vat_percent <= 100):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="НДС должен быть от 0 до 100")
        tariff.vat_percent = payload.vat_percent

    effective_meter_type = payload.meter_type or tariff.meter_type.value

    if payload.use_multiplier is not None:
        tariff.use_multiplier = bool(payload.use_multiplier)
    if effective_meter_type != MeterType.ELECTRIC.value:
        tariff.use_multiplier = False
        tariff.consumption_multiplier = _to_decimal(1)
    if payload.use_multiplier is not None:
        if not tariff.use_multiplier:
            tariff.consumption_multiplier = _to_decimal(1)
    if payload.consumption_multiplier is not None:
        cm = float(payload.consumption_multiplier)
        if cm <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Коэффициент должен быть больше 0")
        tariff.consumption_multiplier = _to_decimal(cm)
    if bool(getattr(tariff, "use_multiplier", False)) and (getattr(tariff, "consumption_multiplier", _to_decimal(1)) or _to_decimal(1)) <= 0:
        tariff.consumption_multiplier = _to_decimal(1)

    # ELECTRIC/GAS-only: фиксированная часть
    if payload.stable_tariff is not None:
        st = float(payload.stable_tariff or 0)
        if st < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Стабильный тариф не может быть отрицательным")
        tariff.stable_tariff = _to_decimal(st) if effective_meter_type in {MeterType.ELECTRIC.value, MeterType.GAS.value} else _to_decimal(0)
    else:
        if effective_meter_type not in {MeterType.ELECTRIC.value, MeterType.GAS.value}:
            tariff.stable_tariff = _to_decimal(0)

    if payload.sewerage_percent is not None:
        sp = float(payload.sewerage_percent or 0)
        if sp < 0 or sp > 100:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Процент канализации должен быть от 0 до 100")
        tariff.sewerage_percent = sp if effective_meter_type == MeterType.WATER.value else 0.0
    else:
        if effective_meter_type != MeterType.WATER.value:
            tariff.sewerage_percent = 0.0
    
    if payload.is_active is not None:
        tariff.is_active = payload.is_active
    
    if payload.steps is not None:
        try:
            steps = _parse_steps(payload.steps, meter_type)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Ошибка валидации ступеней: {e}")
        
        db.query(TariffStep).filter(TariffStep.tariff_id == tariff_id).delete()
        
        is_construction = meter_type == MeterType.CONSTRUCTION.value
        if is_construction:
            for start, end, price in steps:
                db.add(TariffStep(
                    tariff_id=tariff.id,
                    from_date=start,
                    to_date=end,
                    price=price,
                ))
        else:
            for f, t, p in steps:
                db.add(TariffStep(
                    tariff_id=tariff.id,
                    from_value=float(f),
                    to_value=float(t) if t is not None else None,
                    price=float(p),
                ))
    
    db.commit()
    db.refresh(tariff)
    return tariff


@router.delete("/{tariff_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tariff_api(
    tariff_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """Удаление тарифа."""
    tariff = db.get(Tariff, tariff_id)
    if not tariff:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тариф не найден")

    # Если тариф уже используется (счётчики/показания), физическое удаление запрещено FK (RESTRICT).
    # Вместо этого деактивируем тариф, чтобы:
    # - он не назначался новым счётчикам,
    # - он исчезал из списков (по умолчанию),
    # - история показаний/счетов оставалась консистентной.
    in_meters = db.execute(
        select(func.count(ResidentMeter.id)).where(ResidentMeter.tariff_id == tariff_id)
    ).scalar_one() or 0
    in_readings = db.execute(
        select(func.count(MeterReading.id)).where(MeterReading.tariff_id == tariff_id)
    ).scalar_one() or 0

    if in_meters > 0 or in_readings > 0:
        tariff.is_active = False
        db.commit()
        return None

    try:
        db.delete(tariff)
        db.commit()
        return None
    except IntegrityError:
        db.rollback()
        # На всякий случай (если найдутся другие связи): деактивируем.
        tariff = db.get(Tariff, tariff_id)
        if not tariff:
            return None
        tariff.is_active = False
        db.commit()
        return None


# ВРЕМЕННО: публичный endpoint для удаления
@router.delete("/{tariff_id}/public", status_code=status.HTTP_204_NO_CONTENT)
def delete_tariff_public(
    tariff_id: int,
    db: Session = Depends(get_db),
):
    """ВРЕМЕННЫЙ endpoint без авторизации для теста."""
    tariff = db.get(Tariff, tariff_id)
    if not tariff:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тариф не найден")

    in_meters = db.execute(
        select(func.count(ResidentMeter.id)).where(ResidentMeter.tariff_id == tariff_id)
    ).scalar_one() or 0
    in_readings = db.execute(
        select(func.count(MeterReading.id)).where(MeterReading.tariff_id == tariff_id)
    ).scalar_one() or 0

    if in_meters > 0 or in_readings > 0:
        tariff.is_active = False
        db.commit()
        return None

    try:
        db.delete(tariff)
        db.commit()
        return None
    except IntegrityError:
        db.rollback()
        tariff = db.get(Tariff, tariff_id)
        if not tariff:
            return None
        tariff.is_active = False
        db.commit()
        return None

