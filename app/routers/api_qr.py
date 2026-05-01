from typing import Optional
from datetime import datetime
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User, QRToken
from ..security import hash_password


router = APIRouter(prefix="/api/qr", tags=["qr-api"])


class QRTokenGenerateResponse(BaseModel):
    token: str
    user_id: int
    username: str


class QRTokenVerifyResponse(BaseModel):
    valid: bool
    user_id: int
    username: str
    temp_password: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    token: str
    new_password: str
    confirm_password: str
    full_name: str
    phone: str
    email: str


def generate_secure_token() -> str:
    """Генерирует безопасный токен для QR-кода"""
    return secrets.token_urlsafe(32)


@router.post("/users/{user_id}/qr-token", response_model=QRTokenGenerateResponse)
def generate_qr_token(
    user_id: int,
    db: Session = Depends(get_db),
):
    """Генерирует одноразовый QR-токен для пользователя"""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")
    
    # Проверяем, что у пользователя есть временный пароль
    if not user.require_password_change or not user.temp_password_plain:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="У пользователя должен быть временный пароль для генерации QR-кода"
        )
    
    # Генерируем новый токен
    token = generate_secure_token()
    
    # Сохраняем токен в БД
    qr_token = QRToken(
        user_id=user.id,
        token=token,
        is_used=False
    )
    db.add(qr_token)
    db.commit()
    db.refresh(qr_token)
    
    return QRTokenGenerateResponse(
        token=token,
        user_id=user.id,
        username=user.username
    )


# ВРЕМЕННО: публичный endpoint без авторизации
@router.post("/users/{user_id}/qr-token/public", response_model=QRTokenGenerateResponse)
def generate_qr_token_public(
    user_id: int,
    db: Session = Depends(get_db),
):
    """ВРЕМЕННЫЙ endpoint без авторизации для генерации QR-токена"""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")
    
    if not user.require_password_change or not user.temp_password_plain:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="У пользователя должен быть временный пароль для генерации QR-кода"
        )
    
    token = generate_secure_token()
    
    qr_token = QRToken(
        user_id=user.id,
        token=token,
        is_used=False
    )
    db.add(qr_token)
    db.commit()
    db.refresh(qr_token)
    
    return QRTokenGenerateResponse(
        token=token,
        user_id=user.id,
        username=user.username
    )


@router.get("/verify/{token}", response_model=QRTokenVerifyResponse)
def verify_qr_token(
    token: str,
    db: Session = Depends(get_db),
):
    """Проверяет QR-токен и возвращает данные пользователя"""
    qr_token = db.query(QRToken).filter(QRToken.token == token).first()
    
    if not qr_token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Недействительный токен"
        )
    
    if qr_token.is_used:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Токен уже использован"
        )
    
    user = db.get(User, qr_token.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")
    
    if not user.require_password_change or not user.temp_password_plain:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Пароль уже установлен"
        )
    
    return QRTokenVerifyResponse(
        valid=True,
        user_id=user.id,
        username=user.username,
        temp_password=user.temp_password_plain,
        full_name=user.full_name,
        phone=user.phone,
        email=user.email,
    )


@router.post("/change-password", status_code=status.HTTP_200_OK)
def change_password_via_qr(
    payload: ChangePasswordRequest,
    db: Session = Depends(get_db),
):
    """Изменяет пароль пользователя через QR-токен"""
    # Проверяем токен
    qr_token = db.query(QRToken).filter(QRToken.token == payload.token).first()
    
    if not qr_token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Недействительный токен"
        )
    
    if qr_token.is_used:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Токен уже использован"
        )
    
    # Проверяем пароли
    if payload.new_password != payload.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Пароли не совпадают"
        )
    
    if len(payload.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Пароль должен быть не менее 6 символов"
        )
    
    user = db.get(User, qr_token.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    # Проверяем профильные поля
    full_name = (payload.full_name or "").strip()
    phone = (payload.phone or "").strip()
    email = (payload.email or "").strip()
    if not full_name:
        raise HTTPException(status_code=400, detail="ФИО обязательно")
    if not phone:
        raise HTTPException(status_code=400, detail="Телефон обязателен")
    if not email:
        raise HTTPException(status_code=400, detail="Email обязателен")
    
    # Устанавливаем новый пароль
    user.password_hash = hash_password(payload.new_password)
    user.full_name = full_name
    user.phone = phone
    user.email = email
    user.require_password_change = False
    user.temp_password_plain = None
    user.last_password_change_at = datetime.utcnow()
    
    # Помечаем токен как использованный
    qr_token.is_used = True
    qr_token.used_at = datetime.utcnow()
    
    db.commit()
    
    return {
        "success": True,
        "message": "Пароль успешно изменен"
    }

