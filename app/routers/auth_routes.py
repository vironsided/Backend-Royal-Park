from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from starlette import status
from pydantic import BaseModel
from ..database import get_db
from ..models import User, RoleEnum
from ..security import verify_password, set_session, clear_session
from ..deps import get_current_user
from ..frontend import redirect_frontend, redirect_admin

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    success: bool
    role: str
    username: str
    message: str
    require_password_change: bool = False


@router.get("/login")
def login_form():
    return redirect_frontend("/")


@router.post("/login")
def login(request: Request, db: Session = Depends(get_db),
          username: str = Form(...), password: str = Form(...)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash) or not user.is_active:
        return redirect_frontend("/", {"error": "invalid_credentials"})

    user.last_login_at = datetime.utcnow()
    db.commit()

    if user.role == RoleEnum.RESIDENT:
        resp = redirect_frontend("/user/dashboard.html", status_code=status.HTTP_302_FOUND)
    else:
        resp = redirect_admin("/dashboard", status_code=status.HTTP_302_FOUND)
    set_session(resp, user.id)

    if user.require_password_change:
        resp = redirect_frontend("/qr-password-setup.html", {"from_login": "true"}, status_code=status.HTTP_302_FOUND)
        set_session(resp, user.id)
    return resp


@router.post("/api/auth/login", response_model=LoginResponse)
async def api_login(login_data: LoginRequest, db: Session = Depends(get_db)):
    """API endpoint для логина, возвращает роль пользователя и устанавливает сессию"""
    user = db.query(User).filter(User.username == login_data.username).first()
    
    if not user or not verify_password(login_data.password, user.password_hash) or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверное имя пользователя или пароль"
        )
    
    user.last_login_at = datetime.utcnow()
    db.commit()
    
    # Создаем JSONResponse с данными
    response_data = LoginResponse(
        success=True,
        role=user.role.value,
        username=user.username,
        message="Вход выполнен успешно",
        require_password_change=user.require_password_change
    )
    
    # Создаем Response и устанавливаем cookie
    response = JSONResponse(content=response_data.dict())
    set_session(response, user.id)
    
    return response


class PasswordChangeRequest(BaseModel):
    new_password: str
    confirm_password: str
    full_name: str
    phone: str
    email: str


@router.post("/api/auth/force-change-password")
async def api_force_change_password(
    payload: PasswordChangeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """API endpoint для принудительной смены пароля (после первого входа)"""
    if not user.require_password_change:
        return JSONResponse(content={"success": True, "message": "Пароль уже изменен"})

    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Пароли не совпадают")

    if len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="Пароль должен быть не менее 6 символов")

    full_name = (payload.full_name or "").strip()
    phone = (payload.phone or "").strip()
    email = (payload.email or "").strip()
    if not full_name:
        raise HTTPException(status_code=400, detail="ФИО обязательно")
    if not phone:
        raise HTTPException(status_code=400, detail="Телефон обязателен")
    if not email:
        raise HTTPException(status_code=400, detail="Email обязателен")

    from ..security import hash_password
    user.password_hash = hash_password(payload.new_password)
    user.full_name = full_name
    user.phone = phone
    user.email = email
    user.require_password_change = False
    user.temp_password_plain = None
    user.last_password_change_at = datetime.utcnow()
    db.commit()

    return JSONResponse(content={"success": True, "message": "Пароль успешно изменен"})


@router.get("/logout")
def logout():
    resp = redirect_frontend("/", status_code=status.HTTP_302_FOUND)
    clear_session(resp)
    return resp


@router.post("/api/auth/logout")
def api_logout():
    """API endpoint for logout - clears the session cookie"""
    response = JSONResponse(content={"success": True, "message": "Logged out successfully"})
    clear_session(response)
    return response


@router.get("/api/auth/check")
def check_session(user: User = Depends(get_current_user)):
    """Check if current session is valid"""
    return JSONResponse(content={
        "authenticated": True,
        "user_id": user.id,
        "username": user.username,
        "role": user.role.value,
        "require_password_change": user.require_password_change,
        "full_name": user.full_name,
        "phone": user.phone,
        "email": user.email,
    })


@router.get("/force-change-password")
def force_change_password_form(user: User = Depends(get_current_user)):
    if not user.require_password_change:
        if user.role == RoleEnum.RESIDENT:
            return redirect_frontend("/user/dashboard.html", status_code=status.HTTP_302_FOUND)
        return redirect_admin("/dashboard", status_code=status.HTTP_302_FOUND)
    return redirect_frontend("/qr-password-setup.html", {"from_login": "true"}, status_code=status.HTTP_302_FOUND)


@router.post("/force-change-password")
def force_change_password(request: Request, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user),
                          new_password: str = Form(...), new_password2: str = Form(...)):
    if new_password != new_password2:
        return redirect_frontend("/qr-password-setup.html", {"error": "password_mismatch"}, status_code=302)

    from ..security import hash_password
    user.password_hash = hash_password(new_password)
    user.require_password_change = False
    user.temp_password_plain = None
    user.last_password_change_at = datetime.utcnow()
    db.commit()

    if user.role == RoleEnum.RESIDENT:
        return redirect_frontend("/user/dashboard.html", status_code=302)
    return redirect_admin("/dashboard", status_code=302)
