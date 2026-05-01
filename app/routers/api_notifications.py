import json
from typing import List, Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import or_

from ..database import get_db
from ..models import (
    User, RoleEnum,
    Notification, NotificationStatus,
    AppealWorkflow,
    Resident, Block,
)
from ..deps import get_current_user
from ..services.push_service import send_push_to_users
from ..utils import get_user_locale_code, tr_locale
from fastapi import Request
from ..security import get_user_id_from_session


def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """Возвращает текущего пользователя, если он авторизован; иначе None."""
    try:
        user_id = get_user_id_from_session(request)
    except Exception:
        return None
    if not user_id:
        return None
    user = db.get(User, user_id)
    if not user or not user.is_active:
        return None
    return user


router = APIRouter(prefix="/api/notifications", tags=["notifications-api"])
ADMIN_NOTIFICATION_TYPES = {"APPEAL", "TARIFF_EXPIRED"}
# Персональные админ-уведомления: доставляются конкретному user_id
# (видны только адресату, независимо от его роли).
ADMIN_PERSONAL_NOTIFICATION_TYPES = {"CONTRACT_APPROVAL", "CONTRACT_DECISION"}


# Pydantic models
class NotificationOut(BaseModel):
    id: int
    user_id: int
    resident_id: Optional[int] = None
    message: str
    status: str  # "UNREAD" or "READ"
    created_at: datetime
    read_at: Optional[datetime] = None
    
    # Новые поля
    notification_type: Optional[str] = None  # INVOICE, NEWS, APPEAL
    related_id: Optional[int] = None  # ID счета, новости и т.д.
    
    # User info
    user_full_name: Optional[str] = None
    user_phone: Optional[str] = None
    user_email: Optional[str] = None
    
    # Resident info
    resident_code: Optional[str] = None  # "A / 205"
    resident_info: Optional[str] = None  # "Блок A, №205"
    block_name: Optional[str] = None
    unit_number: Optional[str] = None

    appeal_workflow: Optional[str] = None
    staff_message: Optional[str] = None
    workflow_updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class NotificationPatch(BaseModel):
    status: Optional[str] = None
    appeal_workflow: Optional[str] = None
    staff_message: Optional[str] = Field(None, max_length=4000)


def _is_appeal_notification(notif: Notification) -> bool:
    t = (notif.notification_type or "").upper()
    return t == "APPEAL" or t == ""


def _build_notification_out(notif: Notification) -> NotificationOut:
    resident_code = None
    resident_info = None
    block_name = None
    unit_number = None

    if notif.resident:
        block_name = notif.resident.block.name if notif.resident.block else None
        unit_number = notif.resident.unit_number
        if block_name and unit_number:
            resident_code = f"{block_name} / {unit_number}"
            resident_info = f"Блок {block_name}, №{unit_number}"

    return NotificationOut(
        id=notif.id,
        user_id=notif.user_id,
        resident_id=notif.resident_id,
        message=notif.message,
        status=notif.status.value,
        created_at=notif.created_at,
        read_at=notif.read_at,
        notification_type=notif.notification_type,
        related_id=notif.related_id,
        user_full_name=notif.user.full_name,
        user_phone=notif.user.phone,
        user_email=notif.user.email,
        resident_code=resident_code,
        resident_info=resident_info,
        block_name=block_name,
        unit_number=unit_number,
        appeal_workflow=notif.appeal_workflow,
        staff_message=notif.staff_message,
        workflow_updated_at=notif.workflow_updated_at,
    )


def _list_notifications_internal(
    db: Session,
    status_filter: Optional[str] = None,
    resident_id: Optional[int] = None,
    block_id: Optional[str] = None,
    q: Optional[str] = None,
    page: int = 1,
    per_page: int = 25,
    current_user: Optional[User] = None,
    scope: Optional[str] = None,
):
    """Внутренняя функция для получения списка уведомлений.

    ``scope``:
      * ``"appeals"`` — для раздела «Обращения жителей»: показываем только
        APPEAL (и исторические записи без типа). Контрактные и системные
        уведомления сюда попадать не должны.
      * по умолчанию (``None`` / ``"admin"``) — общий админ-список:
        * общие админ-уведомления (APPEAL, TARIFF_EXPIRED, исторические NULL),
        * персональные админ-уведомления (CONTRACT_APPROVAL, CONTRACT_DECISION) —
          только те, которые адресованы текущему пользователю.
    """
    from sqlalchemy import or_, and_
    scope_value = (scope or "").strip().lower()

    if scope_value == "appeals":
        type_clauses = [
            Notification.notification_type == "APPEAL",
            Notification.notification_type == None,
        ]
    else:
        type_clauses = [
            Notification.notification_type.in_(list(ADMIN_NOTIFICATION_TYPES)),
            Notification.notification_type == None,
        ]
        if current_user is not None:
            type_clauses.append(
                and_(
                    Notification.notification_type.in_(list(ADMIN_PERSONAL_NOTIFICATION_TYPES)),
                    Notification.user_id == current_user.id,
                )
            )

    query = db.query(Notification).join(User, User.id == Notification.user_id).filter(
        or_(*type_clauses)
    )
    
    # Join с Resident для фильтрации
    if resident_id or block_id or q:
        query = query.outerjoin(Resident, Resident.id == Notification.resident_id)
        if block_id:
            query = query.outerjoin(Block, Block.id == Resident.block_id)
    
    # Фильтр по статусу
    if status_filter and status_filter.upper() in {s.value for s in NotificationStatus}:
        status_enum = NotificationStatus(status_filter.upper())
        query = query.filter(Notification.status == status_enum)
    
    # Фильтр по резиденту
    if resident_id:
        query = query.filter(Notification.resident_id == resident_id)
    
    # Фильтр по блоку
    if block_id:
        query = query.filter(Block.name == block_id)
    
    # Поиск
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Notification.message.ilike(like),
                User.full_name.ilike(like),
                User.phone.ilike(like),
                User.email.ilike(like),
            )
        )
    
    total = query.count()
    last_page = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, last_page))
    
    notifications = query.order_by(Notification.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    
    result = [_build_notification_out(notif) for notif in notifications]
    
    return {
        "notifications": result,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "last_page": last_page,
        }
    }


def _get_notification_internal(
    db: Session,
    notification_id: int,
    mark_read: bool = True,
):
    """Внутренняя функция для получения одного уведомления."""
    notif = db.query(Notification).filter(Notification.id == notification_id).first()
    if not notif:
        return None

    if mark_read and notif.status == NotificationStatus.UNREAD:
        notif.status = NotificationStatus.READ
        notif.read_at = datetime.utcnow()
        db.commit()
        db.refresh(notif)

    return _build_notification_out(notif)


@router.get("/")
def list_notifications(
    status: Optional[str] = Query(None, alias="status"),
    resident_id: Optional[int] = Query(None),
    block: Optional[str] = Query(None, alias="block"),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    scope: Optional[str] = Query(None, description='"appeals" — только обращения жителей; по умолчанию — общий админ-список.'),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Получить список уведомлений (требует авторизации)."""
    try:
        return _list_notifications_internal(
            db=db,
            status_filter=status,
            resident_id=resident_id,
            block_id=block,
            q=q,
            page=page,
            per_page=per_page,
            current_user=current_user,
            scope=scope,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/public")
def list_notifications_public(
    status: Optional[str] = Query(None, alias="status"),
    resident_id: Optional[int] = Query(None),
    block: Optional[str] = Query(None, alias="block"),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    scope: Optional[str] = Query(None, description='"appeals" — только обращения жителей.'),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Получить список уведомлений. Endpoint не требует специальных прав,
    но использует текущую сессию, чтобы отфильтровать персональные уведомления
    (например, решения по договору) только для адресата."""
    try:
        return _list_notifications_internal(
            db=db,
            status_filter=status,
            resident_id=resident_id,
            block_id=block,
            q=q,
            page=page,
            per_page=per_page,
            current_user=current_user,
            scope=scope,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/unread-count")
def get_unread_count_public(
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Получить количество непрочитанных уведомлений для админ-панели."""
    from sqlalchemy import func, or_, and_
    type_clauses = [
        Notification.notification_type.in_(list(ADMIN_NOTIFICATION_TYPES)),
        Notification.notification_type == None,
    ]
    if current_user is not None:
        type_clauses.append(
            and_(
                Notification.notification_type.in_(list(ADMIN_PERSONAL_NOTIFICATION_TYPES)),
                Notification.user_id == current_user.id,
            )
        )
    count = db.query(func.count(Notification.id)).filter(
        Notification.status == NotificationStatus.UNREAD,
        or_(*type_clauses)
    ).scalar()
    return {"count": count or 0}


@router.get("/user/me")
def get_user_notifications(
    status: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Получить уведомления текущего пользователя (для резидентской панели)."""
    # ВАЖНО: В личном кабинете жителя НЕ показываем его собственные обращения (APPEAL),
    # так как они отображаются в отдельном разделе "Обращения".
    # Показываем только уведомления о счетах, новостях и сообщения от администрации.
    query = db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.notification_type != "APPEAL",
        Notification.notification_type != None # Исключаем старые обращения без типа
    )
    
    # Фильтр по статусу
    if status and status.upper() in {s.value for s in NotificationStatus}:
        status_enum = NotificationStatus(status.upper())
        query = query.filter(Notification.status == status_enum)
    
    total = query.count()
    last_page = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, last_page))
    
    notifications = query.order_by(Notification.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    
    result = []
    for notif in notifications:
        resident_code = None
        resident_info = None
        block_name = None
        unit_number = None
        
        if notif.resident:
            block_name = notif.resident.block.name if notif.resident.block else None
            unit_number = notif.resident.unit_number
            if block_name and unit_number:
                resident_code = f"{block_name} / {unit_number}"
                resident_info = f"Блок {block_name}, №{unit_number}"
        
        result.append(
            NotificationOut(
                id=notif.id,
                user_id=notif.user_id,
                resident_id=notif.resident_id,
                message=notif.message,
                status=notif.status.value,
                created_at=notif.created_at,
                read_at=notif.read_at,
                notification_type=notif.notification_type,
                related_id=notif.related_id,
                user_full_name=notif.user.full_name,
                user_phone=notif.user.phone,
                user_email=notif.user.email,
                resident_code=resident_code,
                resident_info=resident_info,
                block_name=block_name,
                unit_number=unit_number,
                appeal_workflow=notif.appeal_workflow,
                staff_message=notif.staff_message,
                workflow_updated_at=notif.workflow_updated_at,
            )
        )
    
    return {
        "notifications": result,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "last_page": last_page,
        }
    }


@router.get("/user/me/unread-count")
def get_user_unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Получить количество непрочитанных уведомлений текущего пользователя."""
    from sqlalchemy import func
    # Считаем только те уведомления, которые не являются обращениями самого жителя
    count = db.query(func.count(Notification.id)).filter(
        Notification.user_id == current_user.id,
        Notification.status == NotificationStatus.UNREAD,
        Notification.notification_type != "APPEAL",
        Notification.notification_type != None
    ).scalar()
    return {"count": count or 0}


@router.get("/{notification_id}")
def get_notification(
    notification_id: int,
    mark_read: bool = Query(True, description="Если false — не помечать прочитанным (для предпросмотра в админке)."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Получить одно уведомление по ID (требует авторизации)."""
    notif = _get_notification_internal(db, notification_id, mark_read=mark_read)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    return notif


@router.get("/{notification_id}/public")
def get_notification_public(
    notification_id: int,
    db: Session = Depends(get_db),
):
    """Получить одно уведомление по ID (публичный endpoint без авторизации)."""
    notif = _get_notification_internal(db, notification_id, mark_read=True)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    return notif


def _norm_optional_str(v: Optional[str]) -> Optional[str]:
    s = (v or "").strip()
    return s if s else None


@router.patch("/{notification_id}", response_model=NotificationOut)
def patch_notification(
    notification_id: int,
    data: NotificationPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Обновить обращение: стадия обработки, комментарий для жителя, статус прочтения."""
    notif = db.query(Notification).filter(Notification.id == notification_id).first()
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")

    snap_wf = notif.appeal_workflow
    snap_msg = notif.staff_message

    new_status = None
    if data.status is not None:
        su = data.status.upper()
        if su not in {s.value for s in NotificationStatus}:
            raise HTTPException(status_code=400, detail="Invalid status")
        new_status = NotificationStatus(su)

    wf_in = data.appeal_workflow
    if wf_in is not None:
        wf_in = wf_in.strip().upper()
        valid = {w.value for w in AppealWorkflow}
        if wf_in not in valid:
            raise HTTPException(status_code=400, detail="Invalid appeal_workflow")

    is_appeal = _is_appeal_notification(notif)

    # UNREAD + только workflow: считаем принятием в работу (READ + стадия)
    if is_appeal and notif.status == NotificationStatus.UNREAD and wf_in is not None and new_status is None:
        new_status = NotificationStatus.READ

    transitioning_to_read = (
        is_appeal
        and notif.status == NotificationStatus.UNREAD
        and new_status == NotificationStatus.READ
    )

    if transitioning_to_read and not wf_in:
        raise HTTPException(
            status_code=400,
            detail="appeal_workflow is required when marking an appeal as read",
        )

    if wf_in is not None:
        notif.appeal_workflow = wf_in
        notif.workflow_updated_at = datetime.utcnow()
    if data.staff_message is not None:
        msg = (data.staff_message or "").strip() or None
        notif.staff_message = msg
        notif.workflow_updated_at = datetime.utcnow()

    if new_status is not None:
        notif.status = new_status
        if new_status == NotificationStatus.READ and notif.read_at is None:
            notif.read_at = datetime.utcnow()
        if new_status == NotificationStatus.UNREAD:
            notif.read_at = None

    wf_changed = wf_in is not None and _norm_optional_str(snap_wf) != _norm_optional_str(notif.appeal_workflow)
    msg_changed = data.staff_message is not None and _norm_optional_str(snap_msg) != _norm_optional_str(notif.staff_message)
    has_wf = bool(_norm_optional_str(notif.appeal_workflow))
    has_staff = bool(_norm_optional_str(notif.staff_message))
    should_send_push = False
    push_body = ""
    if is_appeal and (wf_changed or msg_changed) and (has_wf or has_staff):
        payload = json.dumps(
            {
                "k": "appeal_update",
                "wf": notif.appeal_workflow or "",
                "m": notif.staff_message or "",
            },
            ensure_ascii=False,
        )
        # Один пуш жителю: при двойном клике / параллельных PATCH не плодим строки — обновляем недавнее
        cutoff = datetime.utcnow() - timedelta(seconds=12)
        recent_push = (
            db.query(Notification)
            .filter(
                Notification.user_id == notif.user_id,
                Notification.notification_type == "APPEAL_UPDATE",
                Notification.related_id == notif.id,
                Notification.created_at >= cutoff,
            )
            .order_by(Notification.created_at.desc())
            .first()
        )
        if recent_push:
            recent_push.message = payload
            recent_push.status = NotificationStatus.UNREAD
            recent_push.created_at = datetime.utcnow()
        else:
            db.add(
                Notification(
                    user_id=notif.user_id,
                    resident_id=notif.resident_id,
                    message=payload,
                    status=NotificationStatus.UNREAD,
                    notification_type="APPEAL_UPDATE",
                    related_id=notif.id,
                    created_at=datetime.utcnow(),
                )
            )
        if notif.staff_message:
            push_body = notif.staff_message[:180]
        should_send_push = True

    db.commit()
    if should_send_push:
        locale = get_user_locale_code(db, notif.user_id)
        push_title = tr_locale(
            locale,
            az="Muraciet yenilendi",
            en="Request updated",
            ru="Обращение обновлено",
        )
        fallback_body = tr_locale(
            locale,
            az="Muracietinizin statusu yenilendi",
            en="Your request status has been updated",
            ru="Статус вашего обращения обновлен",
        )
        send_push_to_users(
            db,
            user_ids=[notif.user_id],
            title=push_title,
            body=push_body if push_body.strip() else fallback_body,
            data={"type": "APPEAL_UPDATE", "notification_id": str(notif.id), "locale": locale},
        )
    db.refresh(notif)
    return _build_notification_out(notif)


@router.delete("/{notification_id}")
def delete_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Удалить уведомление (требует авторизации)."""
    notif = db.query(Notification).filter(Notification.id == notification_id).first()
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    
    db.delete(notif)
    db.commit()
    
    return {"ok": True, "message": "Notification deleted"}


@router.delete("/{notification_id}/public")
def delete_notification_public(
    notification_id: int,
    db: Session = Depends(get_db),
):
    """Удалить уведомление (публичный endpoint без авторизации)."""
    notif = db.query(Notification).filter(Notification.id == notification_id).first()
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    
    db.delete(notif)
    db.commit()
    
    return {"ok": True, "message": "Notification deleted"}

