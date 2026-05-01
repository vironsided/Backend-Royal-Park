import secrets
import string
from datetime import datetime, date, time, timedelta, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

# Основная таймзона проекта (GMT+4, Asia/Baku). Если нет tzdata — используем фиксированный UTC+4.
def _load_baku_tz():
    if not ZoneInfo:
        return timezone(timedelta(hours=4), name="UTC+4")
    try:
        return ZoneInfo("Asia/Baku")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=4), name="UTC+4")

BAKU_TZ = _load_baku_tz()


def generate_temp_password(length: int = 10) -> str:
    """
    Генерирует случайный пароль заданной длины.
    Использует только буквы (верхний и нижний регистр) и цифры.
    Примеры: fCMLqhzVSb, Kx9mP2vLq, F8nR5wT3Y
    """
    # Используем только буквы и цифры (без специальных символов)
    alphabet = string.ascii_letters + string.digits
    
    # Генерируем случайный пароль из букв и цифр
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def now_baku() -> datetime:
    """
    Текущее время в часовой зоне Азербайджана (GMT+4).
    При отсутствии ZoneInfo используется системное время.
    """
    if BAKU_TZ:
        return datetime.now(BAKU_TZ)
    return datetime.now()


def _resolve_invoice_tenant_id(db, resident_id: int) -> int:
    """
    Возвращает tenant_id для номера счёта.
    Берём минимальный user_id среди пользователей с ролью RESIDENT,
    связанных с resident_id. Если таких нет — fallback на минимальный
    user_id из связки user_residents.
    """
    from sqlalchemy import func
    from .models import User, RoleEnum, user_residents

    tenant_id = (
        db.query(func.min(User.id))
        .join(user_residents, User.id == user_residents.c.user_id)
        .filter(
            user_residents.c.resident_id == resident_id,
            User.role == RoleEnum.RESIDENT,
        )
        .scalar()
    )
    if tenant_id is None:
        tenant_id = (
            db.query(func.min(user_residents.c.user_id))
            .filter(user_residents.c.resident_id == resident_id)
            .scalar()
        )
    return int(tenant_id or 0)


def build_invoice_number(db, resident_id: int, period_year: int, period_month: int) -> str:
    """
    Канонический номер счёта:
    INV-(resident_id)/(tenant_id)/(YYYY-MM)
    """
    rid = int(resident_id or 0)
    tenant_id = _resolve_invoice_tenant_id(db, rid)
    yyyy = int(period_year or 0)
    mm = int(period_month or 0)
    return f"INV-{rid}/{tenant_id}/{yyyy}-{mm:02d}"


def to_baku_datetime(value: Any) -> datetime:
    """
    Приводит значение к datetime в зоне Asia/Baku.
    Поддерживаются: datetime, date, ISO-строка, None.
    Считает naive datetimes (без часового пояса) как UTC.
    """
    if value is None:
        return now_baku()

    if isinstance(value, datetime):
        if value.tzinfo is None:
            # Считаем, что наивные объекты в БД хранятся в UTC (utcnow)
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(BAKU_TZ)

    if isinstance(value, date):
        # date приводим к началу дня в Baku
        base_time = time.min
        if BAKU_TZ:
            return datetime.combine(value, base_time, tzinfo=BAKU_TZ)
        return datetime.combine(value, base_time)

    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(BAKU_TZ)
        except Exception:
            return now_baku()

    return now_baku()


def normalize_locale_code(code: str | None) -> str:
    normalized = (code or "").strip().lower()
    if normalized in {"az", "en", "ru"}:
        return normalized
    return "az"


def get_user_locale_code(db, user_id: int) -> str:
    from .models import PushDeviceToken
    row = (
        db.query(PushDeviceToken)
        .filter(
            PushDeviceToken.user_id == user_id,
            PushDeviceToken.is_active == True,
        )
        .order_by(PushDeviceToken.last_seen_at.desc(), PushDeviceToken.updated_at.desc())
        .first()
    )
    if not row:
        return "az"
    return normalize_locale_code(getattr(row, "locale", None))


def tr_locale(code: str, *, az: str, en: str, ru: str) -> str:
    locale = normalize_locale_code(code)
    if locale == "en":
        return en
    if locale == "ru":
        return ru
    return az


def create_invoice_notification(db, invoice, created_by_user_id=None):
    """
    Создает уведомления для всех пользователей, связанных с резидентом,
    когда выставлен счет с due_date (статус ISSUED).
    """
    from .models import Notification, NotificationStatus, NotificationType, User, user_residents, Invoice, InvoiceStatus
    
    # Проверяем, что счет выставлен (ISSUED)
    if invoice.status.value != "ISSUED":
        return
    
    # Получаем всех пользователей, связанных с резидентом через M2M таблицу
    # Фильтруем только тех, у кого роль RESIDENT, чтобы админы не получали эти уведомления
    from .models import RoleEnum
    users = db.query(User).join(
        user_residents, User.id == user_residents.c.user_id
    ).filter(
        user_residents.c.resident_id == invoice.resident_id,
        User.is_active == True,
        User.role == RoleEnum.RESIDENT
    ).all()
    
    if not users:
        return
    
    # Получаем информацию о резиденте и блоке
    resident = invoice.resident
    block = resident.block if resident else None
    block_name = block.name if block else ""
    unit_number = resident.unit_number if resident else ""
    
    # Получаем сумму счета
    amount_total = float(invoice.amount_total or 0)
    amount_str = f"{amount_total:.2f} ₼"
    
    # Находим последний счет для этого резидента (с более ранним периодом)
    from sqlalchemy import or_, and_
    last_invoice = db.query(Invoice).filter(
        Invoice.resident_id == invoice.resident_id,
        Invoice.id != invoice.id,
        Invoice.status == InvoiceStatus.ISSUED,
        # Период должен быть раньше текущего
        or_(
            Invoice.period_year < invoice.period_year,
            and_(
                Invoice.period_year == invoice.period_year,
                Invoice.period_month < invoice.period_month
            )
        )
    ).order_by(
        Invoice.period_year.desc(),
        Invoice.period_month.desc()
    ).first()
    
    # Формируем строку периода
    current_period_str = f"{invoice.period_month:02d}.{invoice.period_year}"
    if last_invoice:
        last_period_str = f"{last_invoice.period_month:02d}.{last_invoice.period_year}"
        period_str = f"{last_period_str} - {current_period_str}"
    else:
        period_str = current_period_str
    
    # Форматируем дату для сообщения
    today_str = now_baku().strftime("%d.%m.%Y")
    due_date_str = invoice.due_date.strftime("%d.%m.%Y") if invoice.due_date else None

    # Создаем уведомления для каждого пользователя с персональной локалью
    localized_push_payloads: list[tuple[int, str, str, str]] = []
    for user in users:
        # ЖЕСТКАЯ ПРОВЕРКА: Только если роль пользователя — RESIDENT
        # Мы проверяем и через строку, и через enum на всякий случай
        user_role = str(user.role.value) if hasattr(user.role, 'value') else str(user.role)
        if user_role != "RESIDENT":
            continue

        locale = get_user_locale_code(db, user.id)
        house_info = tr_locale(
            locale,
            az=f"Blok {block_name}, №{unit_number}" if block_name else f"№{unit_number}",
            en=f"Block {block_name}, #{unit_number}" if block_name else f"#{unit_number}",
            ru=f"Блок {block_name}, №{unit_number}" if block_name else f"№{unit_number}",
        )
        due_to = due_date_str or tr_locale(
            locale,
            az="gosterilmeyib",
            en="not specified",
            ru="не указан",
        )
        message = tr_locale(
            locale,
            az=f"{house_info} ucun hesab kesildi. Mebleg: {amount_str}. Period: {period_str}. Odenis muddeti: {today_str} - {due_to}",
            en=f"An invoice has been issued for {house_info}. Amount: {amount_str}. Period: {period_str}. Payment window: {today_str} - {due_to}",
            ru=f"Выставлен счет для {house_info}. Сумма: {amount_str}. Период: {period_str}. Оплатить с {today_str} по {due_to}",
        )
        push_title = tr_locale(
            locale,
            az="Yeni hesab",
            en="New invoice",
            ru="Новый счет",
        )

        notification = Notification(
            user_id=user.id,
            resident_id=invoice.resident_id,
            message=message,
            status=NotificationStatus.UNREAD,
            notification_type="INVOICE", # Строка для надежности
            related_id=invoice.id,
            created_at=datetime.utcnow()
        )
        db.add(notification)
        localized_push_payloads.append((user.id, push_title, message, locale))
    
    try:
        db.commit()
        if localized_push_payloads:
            from .services.push_service import send_push_to_users
            for user_id, title, body, locale in localized_push_payloads:
                send_push_to_users(
                    db,
                    user_ids=[user_id],
                    title=title,
                    body=body,
                    data={"type": "INVOICE", "invoice_id": str(invoice.id), "locale": locale},
                )
    except Exception as e:
        db.rollback()
        print(f"Error creating invoice notifications: {e}")


def create_news_notification(db, news):
    """
    Создает уведомления для всех активных пользователей-резидентов
    когда публикуется новая новость.
    """
    from .models import Notification, NotificationStatus, NotificationType, User, RoleEnum
    import json
    
    # Проверяем, что новость активна
    if not news.is_active:
        print(f"News {news.id} is not active, skipping notifications")
        return
    
    # Проверяем, что published_at установлена
    now = datetime.utcnow()
    if not news.published_at:
        print(f"News {news.id} has no published_at, skipping notifications")
        return
    
    # Если published_at в будущем более чем на 5 минут, не создаем уведомления
    # (для запланированных новостей - уведомления создадутся позже)
    time_diff = (news.published_at - now).total_seconds()
    if time_diff > 300:  # 5 минут вместо 1 секунды
        print(f"News {news.id} is scheduled for future (diff: {time_diff}s), skipping notifications")
        return
    
    target_blocks = None
    if getattr(news, "target_blocks", None):
        try:
            target_blocks = json.loads(news.target_blocks)
        except Exception:
            target_blocks = None

    # Получаем всех активных пользователей-резидентов
    if target_blocks:
        from .models import Resident, Block, user_residents
        users = (
            db.query(User)
            .join(user_residents, User.id == user_residents.c.user_id)
            .join(Resident, Resident.id == user_residents.c.resident_id)
            .join(Block, Block.id == Resident.block_id)
            .filter(
                User.role == RoleEnum.RESIDENT,
                User.is_active == True,
                Block.name.in_(target_blocks),
            )
            .distinct()
            .all()
        )
    else:
        users = db.query(User).filter(
            User.role == RoleEnum.RESIDENT,
            User.is_active == True
        ).all()
    
    if not users:
        print(f"No active resident users found for news {news.id}")
        return
    
    # Парсим заголовок новости по локали пользователя
    try:
        title_data = json.loads(news.title) if isinstance(news.title, str) else (news.title or {})
    except:
        title_data = {}
    
    # Создаем уведомления для каждого пользователя
    notifications_created = 0
    localized_push_payloads: list[tuple[int, str, str, str]] = []
    for user in users:
        locale = get_user_locale_code(db, user.id)
        title_locale = (
            title_data.get(locale)
            or title_data.get("az")
            or title_data.get("en")
            or title_data.get("ru")
            or tr_locale(locale, az="Yeni xeber", en="New update", ru="Новая новость")
        )
        message = tr_locale(
            locale,
            az=f"Yeni xeber paylasildi: {title_locale}",
            en=f"New update published: {title_locale}",
            ru=f"Опубликована новость: {title_locale}",
        )
        push_title = tr_locale(
            locale,
            az="Yeni xeber",
            en="New update",
            ru="Новая новость",
        )

        # Проверяем, нет ли уже такого уведомления
        existing = db.query(Notification).filter(
            Notification.user_id == user.id,
            Notification.notification_type == NotificationType.NEWS.value,
            Notification.related_id == news.id,
            Notification.status == NotificationStatus.UNREAD
        ).first()
        
        if not existing:
            notification = Notification(
                user_id=user.id,
                resident_id=None,  # Новости не привязаны к конкретному резиденту
                message=message,
                status=NotificationStatus.UNREAD,
                notification_type=NotificationType.NEWS.value,
                related_id=news.id,
                created_at=datetime.utcnow()
            )
            db.add(notification)
            notifications_created += 1
            localized_push_payloads.append((user.id, push_title, message, locale))
    
    try:
        db.commit()
        if localized_push_payloads:
            from .services.push_service import send_push_to_users
            for user_id, title, body, locale in localized_push_payloads:
                send_push_to_users(
                    db,
                    user_ids=[user_id],
                    title=title,
                    body=body,
                    data={"type": "NEWS", "news_id": str(news.id), "locale": locale},
                )
        print(f"Created {notifications_created} notifications for news {news.id} ({len(users)} total users)")
    except Exception as e:
        db.rollback()
        print(f"Error creating news notifications: {e}")
