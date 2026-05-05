from typing import List, Optional
import pathlib

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Request, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User, RoleEnum
from ..deps import get_current_user, can_manage_user
from ..security import hash_password, verify_password, get_user_id_from_session
from ..utils import generate_temp_password, to_baku_datetime


router = APIRouter(prefix="/api/users", tags=["users-api"])


class UserOut(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    avatar_path: Optional[str] = None
    role: RoleEnum
    is_active: bool
    require_password_change: bool
    temp_password_plain: Optional[str] = None
    last_login_at: Optional[datetime] = None

    class Config:
        from_attributes = True
    
    @classmethod
    def from_orm_with_tz(cls, obj: User):
        """Создает UserOut с конвертацией времени в часовой пояс Баку"""
        data = {
            "id": obj.id,
            "username": obj.username,
            "full_name": obj.full_name,
            "phone": obj.phone,
            "email": obj.email,
            "avatar_path": obj.avatar_path,
            "role": obj.role,
            "is_active": obj.is_active,
            "require_password_change": obj.require_password_change,
            "temp_password_plain": obj.temp_password_plain,
            "last_login_at": to_baku_datetime(obj.last_login_at) if obj.last_login_at else None,
        }
        return cls(**data)


class UserCreate(BaseModel):
    username: str
    role: RoleEnum = RoleEnum.OPERATOR
    full_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None


class UserUpdate(BaseModel):
    username: Optional[str] = None
    full_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = None
    role: Optional[RoleEnum] = None


class UserListOut(BaseModel):
    items: List[UserOut]
    total: int
    page: int
    per_page: int
    last_page: int


def _ensure_user_admin_access(actor: User) -> None:
    """Only ROOT and ADMIN can manage/list users."""
    if actor.role not in (RoleEnum.ROOT, RoleEnum.ADMIN):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")


@router.get("/", response_model=UserListOut)
def list_users_api(
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
):
    """
    JSON-список пользователей для SPA-админки с пагинацией.
    """
    _ensure_user_admin_access(actor)
    query = db.query(User)
    total = query.count()
    last_page = max(1, (total + per_page - 1) // per_page)
    if page > last_page:
        page = last_page
    
    users = query.order_by(User.id.asc()).offset((page - 1) * per_page).limit(per_page).all()
    
    return {
        "items": [UserOut.from_orm_with_tz(u) for u in users],
        "total": total,
        "page": page,
        "per_page": per_page,
        "last_page": last_page,
    }


@router.post("/", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user_api(
    payload: UserCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """
    Создание пользователя из SPA.
    root может создавать любых, admin — только operator/resident
    (роли root/admin/sales admin создавать не имеет права).
    """
    _ensure_user_admin_access(actor)

    if actor.role == RoleEnum.ADMIN and payload.role in (RoleEnum.ROOT, RoleEnum.ADMIN, RoleEnum.SALES):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")

    # Генерируем уникальный сложный временный пароль
    temp_password = generate_temp_password(12)

    user = User(
        username=payload.username,
        password_hash=hash_password(temp_password),
        role=payload.role,
        full_name=payload.full_name,
        phone=payload.phone,
        email=payload.email,
        require_password_change=True,
        temp_password_plain=temp_password,
        created_by_id=actor.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut.from_orm_with_tz(user)


# Функция для сохранения аватара
def _save_avatar(file: UploadFile, user_id: int) -> str | None:
    if not file:
        return None
    if file.content_type not in ("image/jpeg", "image/png", "image/webp"):
        return None
    ext = ".jpg" if file.content_type == "image/jpeg" else (".png" if file.content_type == "image/png" else ".webp")
    base_dir = pathlib.Path("uploads/avatars") / str(user_id)
    base_dir.mkdir(parents=True, exist_ok=True)
    path = base_dir / f"avatar{ext}"
    with open(path, "wb") as f:
        f.write(file.file.read())
    rel_path = f"/uploads/avatars/{user_id}/avatar{ext}"
    return rel_path


def _delete_avatar_files(user_id: int) -> None:
    """Delete stored avatar files for a user, if they exist."""
    try:
        base_dir = pathlib.Path("uploads/avatars") / str(user_id)
        if not base_dir.exists():
            return
        for path in base_dir.glob("avatar.*"):
            try:
                if path.is_file():
                    path.unlink()
            except Exception:
                # Non-fatal cleanup failure must not break profile update.
                pass
    except Exception:
        pass


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


# Важно: маршруты /me должны быть ПЕРЕД маршрутами /{user_id}, 
# чтобы FastAPI правильно обрабатывал запросы
@router.get("/me", response_model=UserOut)
def get_current_user_profile(
    user: User = Depends(get_current_user),
):
    """Получение профиля текущего пользователя."""
    return UserOut.from_orm_with_tz(user)


@router.put("/me", response_model=UserOut)
async def update_current_user_profile(
    full_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    remove_avatar: str = Form("0"),
    avatar: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Обновление профиля текущего пользователя."""
    # Обрабатываем данные как в resident_portal.py
    user.full_name = full_name.strip() if full_name.strip() else None
    user.phone = phone.strip() if phone.strip() else None
    
    # Валидация и обработка email
    email_clean = email.strip() if email.strip() else None
    if email_clean and '@' not in email_clean:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неверный формат email"
        )
    user.email = email_clean
    
    remove_avatar_flag = str(remove_avatar or "").strip().lower() in {"1", "true", "yes", "on"}
    if remove_avatar_flag:
        _delete_avatar_files(user.id)
        user.avatar_path = None

    # Обработка аватара
    if avatar and avatar.filename:
        _delete_avatar_files(user.id)
        rel = _save_avatar(avatar, user.id)
        if rel:
            user.avatar_path = rel
    
    db.commit()
    db.refresh(user)
    return UserOut.from_orm_with_tz(user)


@router.post("/me/change-password", response_model=dict)
def change_current_user_password(
    payload: ChangePasswordRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Изменение пароля текущего пользователя."""
    if payload.new_password != payload.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Пароли не совпадают"
        )
    
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неверный текущий пароль"
        )
    
    if len(payload.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Пароль должен быть не менее 6 символов"
        )
    
    user.password_hash = hash_password(payload.new_password)
    user.require_password_change = False
    user.temp_password_plain = None
    user.last_password_change_at = datetime.utcnow()
    db.commit()
    
    return {"success": True, "message": "Пароль успешно изменён"}


# Маршруты для управления пользователями (должны быть ПОСЛЕ маршрутов /me)
@router.put("/{user_id}", response_model=UserOut)
def update_user_api(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """
    Обновление полей пользователя (ФИО, телефон, email, статус, роль).
    """
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    if not can_manage_user(user, actor):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")

    if payload.username is not None:
        # проверяем дубликаты
        exists = db.query(User).filter(User.username == payload.username, User.id != user.id).first()
        if exists:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Логин уже используется")
        user.username = payload.username
    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.phone is not None:
        user.phone = payload.phone
    if payload.email is not None:
        user.email = payload.email
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.role is not None:
        # admin не может повышать до root/admin
        if actor.role == RoleEnum.ADMIN and payload.role in (RoleEnum.ROOT, RoleEnum.ADMIN):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав для смены роли")
        user.role = payload.role

    db.commit()
    db.refresh(user)
    return UserOut.from_orm_with_tz(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user_api(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """
    Удаление пользователя.
    """
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    if target.id == actor.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Нельзя удалить себя")

    if not can_manage_user(target, actor):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")

    db.delete(target)
    db.commit()
    return None


@router.post("/{user_id}/reset", response_model=UserOut)
def reset_password_api(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """
    Сброс пароля с генерацией временного пароля.
    """
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    if not can_manage_user(target, actor):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")

    # Сложный временный пароль как у жителей (только буквы, случайный)
    temp_pass = generate_temp_password(10)
    target.password_hash = hash_password(temp_pass)
    target.require_password_change = True
    target.temp_password_plain = temp_pass
    db.commit()
    db.refresh(target)
    return UserOut.from_orm_with_tz(target)


