"""
API для модуля продаж (SALES).

Работа с договорами купли-продажи вилл/домов в комплексе.
Роли:
  - SALES (менеджер продаж, "Satish") — создаёт и заполняет договор, отправляет на согласование.
  - ROOT — получает push, просматривает, одобряет/отклоняет.
  - ADMIN — видит все договора (read-only + может одобрять/отклонять как root? — НЕТ: одобряет только ROOT).
"""

import json
from datetime import datetime, date
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import (
    Notification,
    NotificationStatus,
    RoleEnum,
    SalesContract,
    SalesContractInstallment,
    SalesContractStatus,
    SalesContractType,
    User,
)


router = APIRouter(prefix="/api/sales", tags=["sales"])


# =========================================================
#  Pydantic schemas
# =========================================================


class InstallmentIn(BaseModel):
    month_no: int
    payment_date: Optional[date] = None
    amount_usd: Optional[Decimal] = None


class InstallmentOut(InstallmentIn):
    id: int

    class Config:
        from_attributes = True


class SalesContractBase(BaseModel):
    contract_type: SalesContractType = SalesContractType.FULL

    contract_number: Optional[str] = None
    contract_year: Optional[int] = None
    contract_date: Optional[date] = None
    city: Optional[str] = "Bakı şəhəri"

    buyer_full_name: Optional[str] = None
    buyer_id_series: Optional[str] = None
    buyer_id_number: Optional[str] = None
    buyer_fin: Optional[str] = None
    buyer_phone: Optional[str] = None
    buyer_email: Optional[str] = None
    buyer_address: Optional[str] = None

    house_number: Optional[str] = None
    area_m2: Optional[Decimal] = None
    price_per_m2_usd: Optional[Decimal] = None
    total_price_usd: Optional[Decimal] = None

    initial_payment_usd: Optional[Decimal] = None
    remaining_usd: Optional[Decimal] = None
    months_count: Optional[int] = None
    monthly_payment_usd: Optional[Decimal] = None


class SalesContractCreate(SalesContractBase):
    installments: List[InstallmentIn] = []


class SalesContractUpdate(SalesContractBase):
    installments: Optional[List[InstallmentIn]] = None


class SalesContractOut(SalesContractBase):
    id: int
    status: SalesContractStatus

    created_by_id: Optional[int] = None
    created_by_username: Optional[str] = None

    approval_requested_at: Optional[datetime] = None

    viewed_by_id: Optional[int] = None
    viewed_by_username: Optional[str] = None
    viewed_at: Optional[datetime] = None

    reviewed_by_id: Optional[int] = None
    reviewed_by_username: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_comment: Optional[str] = None

    printed_at: Optional[datetime] = None
    printed_count: int = 0

    created_at: datetime
    updated_at: datetime

    installments: List[InstallmentOut] = []

    class Config:
        from_attributes = True


class SalesContractListOut(BaseModel):
    items: List[SalesContractOut]
    total: int
    page: int
    per_page: int
    last_page: int


class ReviewPayload(BaseModel):
    comment: Optional[str] = None


# =========================================================
#  Helpers
# =========================================================


def _can_view_contracts(actor: User) -> bool:
    return actor.role in (RoleEnum.ROOT, RoleEnum.ADMIN, RoleEnum.SALES)


def _can_edit_contract(contract: SalesContract, actor: User) -> bool:
    """Редактировать может автор (SALES) либо ROOT/ADMIN (на всякий случай)."""
    if actor.role == RoleEnum.ROOT:
        return True
    if actor.role == RoleEnum.ADMIN:
        return True
    if actor.role == RoleEnum.SALES:
        return contract.created_by_id == actor.id
    return False


def _ensure_can_view(actor: User) -> None:
    if not _can_view_contracts(actor):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")


def _contract_to_out(c: SalesContract) -> SalesContractOut:
    return SalesContractOut(
        id=c.id,
        contract_type=c.contract_type,
        status=c.status,
        contract_number=c.contract_number,
        contract_year=c.contract_year,
        contract_date=c.contract_date,
        city=c.city,
        buyer_full_name=c.buyer_full_name,
        buyer_id_series=c.buyer_id_series,
        buyer_id_number=c.buyer_id_number,
        buyer_fin=c.buyer_fin,
        buyer_phone=c.buyer_phone,
        buyer_email=c.buyer_email,
        buyer_address=c.buyer_address,
        house_number=c.house_number,
        area_m2=c.area_m2,
        price_per_m2_usd=c.price_per_m2_usd,
        total_price_usd=c.total_price_usd,
        initial_payment_usd=c.initial_payment_usd,
        remaining_usd=c.remaining_usd,
        months_count=c.months_count,
        monthly_payment_usd=c.monthly_payment_usd,
        created_by_id=c.created_by_id,
        created_by_username=(c.created_by.username if c.created_by else None),
        approval_requested_at=c.approval_requested_at,
        viewed_by_id=c.viewed_by_id,
        viewed_by_username=(c.viewed_by.username if c.viewed_by else None),
        viewed_at=c.viewed_at,
        reviewed_by_id=c.reviewed_by_id,
        reviewed_by_username=(c.reviewed_by.username if c.reviewed_by else None),
        reviewed_at=c.reviewed_at,
        review_comment=c.review_comment,
        printed_at=c.printed_at,
        printed_count=c.printed_count,
        created_at=c.created_at,
        updated_at=c.updated_at,
        installments=[
            InstallmentOut(
                id=i.id,
                month_no=i.month_no,
                payment_date=i.payment_date,
                amount_usd=i.amount_usd,
            )
            for i in (c.installments or [])
        ],
    )


def _notify_roots_on_request(db: Session, contract: SalesContract, actor: User) -> None:
    """Создаёт push-уведомления для всех активных ROOT-пользователей.

    Текст сохраняется как JSON с параметрами, чтобы фронт смог отрендерить
    сообщение на выбранном языке. Для совместимости при неудачном парсинге
    фронт покажет сырой JSON — поэтому ключи должны быть стабильными.
    """
    roots = db.query(User).filter(User.role == RoleEnum.ROOT, User.is_active.is_(True)).all()
    payload = {
        "kind": "contract_approval_request",
        "contract_number": contract.contract_number or str(contract.id),
        "house": contract.house_number or "—",
        "buyer": contract.buyer_full_name or "—",
        "actor": actor.full_name or actor.username,
    }
    message = json.dumps(payload, ensure_ascii=False)
    now = datetime.utcnow()
    for r in roots:
        db.add(
            Notification(
                user_id=r.id,
                message=message,
                status=NotificationStatus.UNREAD,
                notification_type="CONTRACT_APPROVAL",
                related_id=contract.id,
                created_at=now,
            )
        )


def _notify_author_on_decision(db: Session, contract: SalesContract, decision: str, actor: User) -> None:
    """Отправляет уведомление автору договора о решении root.

    Как и для запроса на одобрение, текст сохраняется как JSON — локализацию
    выполняет фронт на основе текущего языка пользователя.
    """
    if not contract.created_by_id:
        return
    payload = {
        "kind": "contract_decision",
        "decision": "APPROVED" if decision == "APPROVED" else "REJECTED",
        "contract_number": contract.contract_number or str(contract.id),
        "buyer": contract.buyer_full_name or "—",
        "actor": actor.full_name or actor.username,
        "comment": contract.review_comment or "",
    }
    db.add(
        Notification(
            user_id=contract.created_by_id,
            message=json.dumps(payload, ensure_ascii=False),
            status=NotificationStatus.UNREAD,
            notification_type="CONTRACT_DECISION",
            related_id=contract.id,
            created_at=datetime.utcnow(),
        )
    )


def _apply_base_fields(contract: SalesContract, payload: SalesContractBase) -> None:
    data = payload.dict(exclude_unset=True, exclude={"installments"})
    for k, v in data.items():
        setattr(contract, k, v)


def _replace_installments(db: Session, contract: SalesContract, rows: List[InstallmentIn]) -> None:
    # Удаляем старые и добавляем новые — проще и надёжнее, чем diff
    for old in list(contract.installments or []):
        db.delete(old)
    for row in rows or []:
        db.add(
            SalesContractInstallment(
                contract_id=contract.id,
                month_no=row.month_no,
                payment_date=row.payment_date,
                amount_usd=row.amount_usd,
            )
        )


# =========================================================
#  Endpoints
# =========================================================


@router.get("/contracts", response_model=SalesContractListOut)
def list_contracts(
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    status_filter: Optional[SalesContractStatus] = Query(None, alias="status"),
    q: Optional[str] = Query(None, description="поиск по покупателю / номеру дома"),
):
    _ensure_can_view(actor)

    query = db.query(SalesContract)

    # SALES видит только свои договора
    if actor.role == RoleEnum.SALES:
        query = query.filter(SalesContract.created_by_id == actor.id)

    if status_filter:
        query = query.filter(SalesContract.status == status_filter)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (SalesContract.buyer_full_name.ilike(like))
            | (SalesContract.house_number.ilike(like))
            | (SalesContract.contract_number.ilike(like))
        )

    total = query.count()
    last_page = max(1, (total + per_page - 1) // per_page)
    if page > last_page:
        page = last_page

    items = (
        query.order_by(SalesContract.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return SalesContractListOut(
        items=[_contract_to_out(c) for c in items],
        total=total,
        page=page,
        per_page=per_page,
        last_page=last_page,
    )


@router.get("/contracts/{contract_id}", response_model=SalesContractOut)
def get_contract(
    contract_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    _ensure_can_view(actor)
    c = db.get(SalesContract, contract_id)
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Договор не найден")

    if actor.role == RoleEnum.SALES and c.created_by_id != actor.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Чужой договор")

    # Авто-отметка "просмотрено" для ROOT на стадии PENDING_APPROVAL
    if actor.role == RoleEnum.ROOT and c.status == SalesContractStatus.PENDING_APPROVAL:
        c.status = SalesContractStatus.VIEWED
        c.viewed_by_id = actor.id
        c.viewed_at = datetime.utcnow()
        db.commit()
        db.refresh(c)

    return _contract_to_out(c)


@router.post("/contracts", response_model=SalesContractOut, status_code=status.HTTP_201_CREATED)
def create_contract(
    payload: SalesContractCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    if actor.role not in (RoleEnum.SALES, RoleEnum.ADMIN, RoleEnum.ROOT):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")

    c = SalesContract(
        status=SalesContractStatus.DRAFT,
        created_by_id=actor.id,
    )
    _apply_base_fields(c, payload)
    db.add(c)
    db.flush()
    _replace_installments(db, c, payload.installments or [])
    db.commit()
    db.refresh(c)
    return _contract_to_out(c)


@router.put("/contracts/{contract_id}", response_model=SalesContractOut)
def update_contract(
    contract_id: int,
    payload: SalesContractUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    c = db.get(SalesContract, contract_id)
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Договор не найден")

    if not _can_edit_contract(c, actor):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")

    # Редактировать можно только DRAFT или REJECTED (вернули на доработку).
    if c.status not in (SalesContractStatus.DRAFT, SalesContractStatus.REJECTED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Договор нельзя редактировать в текущем статусе",
        )

    _apply_base_fields(c, payload)
    if payload.installments is not None:
        _replace_installments(db, c, payload.installments)

    # После правки REJECTED возвращаем в DRAFT, чтобы продажник мог снова отправить на одобрение
    if c.status == SalesContractStatus.REJECTED:
        c.status = SalesContractStatus.DRAFT
        c.review_comment = None
        c.reviewed_at = None
        c.reviewed_by_id = None
        c.viewed_at = None
        c.viewed_by_id = None
        c.approval_requested_at = None

    db.commit()
    db.refresh(c)
    return _contract_to_out(c)


@router.delete("/contracts/{contract_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_contract(
    contract_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    c = db.get(SalesContract, contract_id)
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Договор не найден")

    if not _can_edit_contract(c, actor):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")

    # Удалять можно только DRAFT или REJECTED
    if c.status not in (SalesContractStatus.DRAFT, SalesContractStatus.REJECTED) and actor.role != RoleEnum.ROOT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя удалить договор в текущем статусе",
        )

    db.delete(c)
    db.commit()
    return None


@router.post("/contracts/{contract_id}/request-approval", response_model=SalesContractOut)
def request_approval(
    contract_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """Продажник отправляет договор на одобрение root-у."""
    c = db.get(SalesContract, contract_id)
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Договор не найден")

    if not _can_edit_contract(c, actor):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")

    if c.status not in (SalesContractStatus.DRAFT, SalesContractStatus.REJECTED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Договор уже отправлен или одобрен",
        )

    # Минимальная валидация перед отправкой
    missing = []
    if not c.buyer_full_name:
        missing.append("ФИО покупателя")
    if not c.buyer_fin:
        missing.append("FIN покупателя")
    if not c.house_number:
        missing.append("номер дома")
    if c.total_price_usd is None:
        missing.append("общая стоимость")
    if c.contract_type == SalesContractType.INSTALLMENT:
        if c.initial_payment_usd is None or c.monthly_payment_usd is None or not c.months_count:
            missing.append("параметры рассрочки (первый взнос / ежемесячно / кол-во месяцев)")
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Заполните обязательные поля: " + ", ".join(missing),
        )

    c.status = SalesContractStatus.PENDING_APPROVAL
    c.approval_requested_at = datetime.utcnow()
    c.viewed_at = None
    c.viewed_by_id = None
    c.reviewed_at = None
    c.reviewed_by_id = None
    c.review_comment = None

    _notify_roots_on_request(db, c, actor)

    db.commit()
    db.refresh(c)
    return _contract_to_out(c)


@router.post("/contracts/{contract_id}/approve", response_model=SalesContractOut)
def approve_contract(
    contract_id: int,
    payload: ReviewPayload = ReviewPayload(),
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    if actor.role != RoleEnum.ROOT:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Одобрять может только ROOT")

    c = db.get(SalesContract, contract_id)
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Договор не найден")

    if c.status not in (SalesContractStatus.PENDING_APPROVAL, SalesContractStatus.VIEWED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Договор не находится в стадии ожидания одобрения",
        )

    now = datetime.utcnow()
    c.status = SalesContractStatus.APPROVED
    c.reviewed_by_id = actor.id
    c.reviewed_at = now
    if payload and payload.comment is not None:
        c.review_comment = payload.comment
    if not c.viewed_at:
        c.viewed_at = now
        c.viewed_by_id = actor.id

    _notify_author_on_decision(db, c, "APPROVED", actor)

    db.commit()
    db.refresh(c)
    return _contract_to_out(c)


@router.post("/contracts/{contract_id}/reject", response_model=SalesContractOut)
def reject_contract(
    contract_id: int,
    payload: ReviewPayload,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    if actor.role != RoleEnum.ROOT:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Отклонять может только ROOT")

    c = db.get(SalesContract, contract_id)
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Договор не найден")

    if c.status not in (SalesContractStatus.PENDING_APPROVAL, SalesContractStatus.VIEWED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Договор не находится в стадии ожидания одобрения",
        )

    now = datetime.utcnow()
    c.status = SalesContractStatus.REJECTED
    c.reviewed_by_id = actor.id
    c.reviewed_at = now
    c.review_comment = (payload.comment or "").strip() or None
    if not c.viewed_at:
        c.viewed_at = now
        c.viewed_by_id = actor.id

    _notify_author_on_decision(db, c, "REJECTED", actor)

    db.commit()
    db.refresh(c)
    return _contract_to_out(c)


@router.post("/contracts/{contract_id}/mark-printed", response_model=SalesContractOut)
def mark_printed(
    contract_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    c = db.get(SalesContract, contract_id)
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Договор не найден")

    if not _can_edit_contract(c, actor):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")

    if c.status not in (SalesContractStatus.APPROVED, SalesContractStatus.PRINTED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Печать доступна только после одобрения",
        )

    c.status = SalesContractStatus.PRINTED
    c.printed_at = datetime.utcnow()
    c.printed_count = (c.printed_count or 0) + 1
    db.commit()
    db.refresh(c)
    return _contract_to_out(c)
