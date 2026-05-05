from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_

from ..database import get_db
from ..deps import get_current_user
from ..models import User, RoleEnum, Block, Resident
from ..security import hash_password
from ..utils import generate_temp_password, to_baku_datetime

router = APIRouter(prefix="/api/tenants", tags=["tenants-api"])


# Pydantic models
class TenantOut(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    last_login: Optional[datetime] = None
    require_password_change: bool
    temp_password: Optional[str] = None  # Временный пароль, если есть
    comment: Optional[str] = None
    resident_ids: List[int] = []
    homes: List[dict] = []  # [{block_name, unit_number}]

    class Config:
        from_attributes = True


class TenantCreate(BaseModel):
    username: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    comment: Optional[str] = None
    resident_ids: List[int] = []


class TenantUpdate(BaseModel):
    username: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    comment: Optional[str] = None
    resident_ids: List[int] = []


def _list_tenants_internal(
    db: Session,
    q: Optional[str] = None,
    block_id: Optional[int] = None,
    unit_number: Optional[str] = None,
    page: int = 1,
    per_page: int = 25,
):
    """
    Внутренняя функция для получения списка жителей (без авторизации).
    """
    query = db.query(User).filter(User.role == RoleEnum.RESIDENT)

    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (User.username.ilike(like))
            | (User.full_name.ilike(like))
            | (User.phone.ilike(like))
            | (User.email.ilike(like))
        )

    if block_id:
        query = query.join(User.resident_links).filter(Resident.block_id == block_id)
    
    if unit_number:
        if not block_id:
            query = query.join(User.resident_links)
        
        unit_number = unit_number.strip()
        from sqlalchemy import func
        if "-" in unit_number:
            parts = unit_number.split("-")
            if len(parts) == 2:
                try:
                    start = int(parts[0].strip())
                    end = int(parts[1].strip())
                    query = query.filter(
                        func.cast(Resident.unit_number, func.Integer).between(start, end)
                    )
                except (ValueError, Exception):
                    query = query.filter(Resident.unit_number.ilike(f"%{unit_number}%"))
            else:
                query = query.filter(Resident.unit_number.ilike(f"%{unit_number}%"))
        else:
            query = query.filter(Resident.unit_number.ilike(f"%{unit_number}%"))

    if block_id or unit_number:
        query = query.distinct()

    total = query.count()
    last_page = max(1, (total + per_page - 1) // per_page)
    if page > last_page:
        page = last_page

    # Новые жители (тенанты) должны отображаться первыми,
    # поэтому сортируем по id по убыванию.
    tenants = (
        query.options(joinedload(User.resident_links))
        .order_by(User.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    blocks = db.query(Block).order_by(Block.name.asc()).all()
    residents = db.query(Resident).order_by(Resident.block_id.asc(), Resident.unit_number.asc()).all()

    result = []
    for u in tenants:
        homes = []
        if u.resident_links:
            for r in u.resident_links:
                homes.append({
                    "block_name": r.block.name if r.block else "",
                    "unit_number": r.unit_number,
                })

        result.append({
            "id": u.id,
            "username": u.username,
            "full_name": u.full_name or "",
            "phone": u.phone or "",
            "email": u.email or "",
            "last_login": to_baku_datetime(u.last_login_at) if u.last_login_at else None,
            "require_password_change": u.require_password_change,
            "temp_password": u.temp_password_plain or None,
            "comment": u.comment or "",
            "resident_ids": [r.id for r in (u.resident_links or [])],
            "homes": homes,
        })

    return {
        "tenants": result,
        "blocks": [{"id": b.id, "name": b.name} for b in blocks],
        "residents": [{"id": r.id, "block_name": r.block.name if r.block else "", "unit_number": r.unit_number} for r in residents],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "last_page": last_page,
        },
    }


@router.get("")
def list_tenants_api(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: Optional[str] = Query(None),
    block_id: Optional[int] = Query(None),
    unit_number: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    """
    Список жителей (пользователей с ролью RESIDENT).
    """
    return _list_tenants_internal(db, q, block_id, unit_number, page, per_page)


@router.get("/public")
def list_tenants_public(
    db: Session = Depends(get_db),
    q: Optional[str] = Query(None),
    block_id: Optional[int] = Query(None),
    unit_number: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
):
    """Public endpoint for testing."""
    try:
        return _list_tenants_internal(db, q, block_id, unit_number, page, per_page)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"Error in list_tenants_public: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


def _get_tenant_internal(
    tenant_id: int,
    db: Session,
):
    """
    Внутренняя функция для получения данных жителя (без авторизации).
    """
    u = db.query(User).options(joinedload(User.resident_links)).filter(
        User.id == tenant_id,
        User.role == RoleEnum.RESIDENT
    ).first()

    if not u:
        raise HTTPException(status_code=404, detail="Tenant not found")

    homes = []
    if u.resident_links:
        for r in u.resident_links:
            homes.append({
                "block_name": r.block.name if r.block else "",
                "unit_number": r.unit_number,
            })

    return {
        "id": u.id,
        "username": u.username,
        "full_name": u.full_name or "",
        "phone": u.phone or "",
        "email": u.email or "",
        "comment": u.comment or "",
        "resident_ids": [r.id for r in (u.resident_links or [])],
        "homes": homes,
    }


@router.get("/{tenant_id}")
def get_tenant_api(
    tenant_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Получить данные жителя для редактирования.
    """
    return _get_tenant_internal(tenant_id, db)


@router.get("/{tenant_id}/public")
def get_tenant_public(
    tenant_id: int,
    db: Session = Depends(get_db),
):
    """Public endpoint for testing."""
    return _get_tenant_internal(tenant_id, db)


def _create_tenant_internal(
    data: TenantCreate,
    db: Session,
):
    """
    Внутренняя функция для создания жителя (без авторизации).
    """
    username = (data.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    exists = db.query(User).filter(User.username == username).first()
    if exists:
        raise HTTPException(status_code=400, detail="Username already exists")

    temp = generate_temp_password(10)
    u = User(
        username=username,
        password_hash=hash_password(temp),
        role=RoleEnum.RESIDENT,
        require_password_change=True,
        temp_password_plain=temp,
        full_name=data.full_name or None,
        phone=data.phone or None,
        email=data.email,
        comment=data.comment or None,
    )
    db.add(u)
    db.flush()

    if data.resident_ids:
        objs = db.query(Resident).filter(Resident.id.in_(data.resident_ids)).all()
        u.resident_links = objs

    db.commit()
    db.refresh(u)

    homes = []
    if u.resident_links:
        for r in u.resident_links:
            homes.append({
                "block_name": r.block.name if r.block else "",
                "unit_number": r.unit_number,
            })

    return {
        "id": u.id,
        "username": u.username,
        "full_name": u.full_name or "",
        "phone": u.phone or "",
        "email": u.email or "",
        "last_login": u.last_login_at,
        "require_password_change": u.require_password_change,
        "temp_password": u.temp_password_plain or None,
        "comment": u.comment or "",
        "resident_ids": [r.id for r in (u.resident_links or [])],
        "homes": homes,
    }


@router.post("/public", status_code=status.HTTP_201_CREATED)
def create_tenant_public(
    data: TenantCreate,
    db: Session = Depends(get_db),
):
    """Public endpoint for testing."""
    try:
        return _create_tenant_internal(data, db)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"Error in create_tenant_public: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.post("", status_code=status.HTTP_201_CREATED)
def create_tenant_api(
    data: TenantCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Создать нового жителя.
    """
    return _create_tenant_internal(data, db)


@router.put("/{tenant_id}")
def update_tenant_api(
    tenant_id: int,
    data: TenantUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Обновить данные жителя.
    """
    u = db.query(User).options(joinedload(User.resident_links)).filter(
        User.id == tenant_id,
        User.role == RoleEnum.RESIDENT
    ).first()

    if not u:
        raise HTTPException(status_code=404, detail="Tenant not found")

    username = (data.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    exists = db.query(User).filter(User.id != u.id, User.username == username).first()
    if exists:
        raise HTTPException(status_code=400, detail="Username already exists")

    u.username = username
    u.full_name = data.full_name or None
    u.phone = data.phone or None
    u.email = data.email
    u.comment = data.comment or None

    objs = db.query(Resident).filter(Resident.id.in_(data.resident_ids or [])).all()
    u.resident_links = objs

    db.commit()
    db.refresh(u)

    homes = []
    if u.resident_links:
        for r in u.resident_links:
            homes.append({
                "block_name": r.block.name if r.block else "",
                "unit_number": r.unit_number,
            })

    return {
        "id": u.id,
        "username": u.username,
        "full_name": u.full_name or "",
        "phone": u.phone or "",
        "email": u.email or "",
        "last_login": u.last_login_at,
        "require_password_change": u.require_password_change,
        "temp_password": u.temp_password_plain or None,
        "comment": u.comment or "",
        "resident_ids": [r.id for r in (u.resident_links or [])],
        "homes": homes,
    }


@router.put("/{tenant_id}/public")
def update_tenant_public(
    tenant_id: int,
    data: TenantUpdate,
    db: Session = Depends(get_db),
):
    """Public endpoint for testing."""
    return update_tenant_api(tenant_id, data, db, None)


def _reset_tenant_password_internal(
    tenant_id: int,
    db: Session,
):
    """
    Внутренняя функция для сброса пароля жителя (без авторизации).
    """
    u = db.get(User, tenant_id)
    if not u or u.role != RoleEnum.RESIDENT:
        raise HTTPException(status_code=404, detail="Tenant not found")

    temp = generate_temp_password(10)
    u.password_hash = hash_password(temp)
    u.require_password_change = True
    u.temp_password_plain = temp

    db.commit()

    return {"success": True, "message": "Password reset successfully"}


@router.post("/{tenant_id}/reset")
def reset_tenant_password_api(
    tenant_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Сбросить пароль жителя.
    """
    return _reset_tenant_password_internal(tenant_id, db)


@router.post("/{tenant_id}/reset/public")
def reset_tenant_password_public(
    tenant_id: int,
    db: Session = Depends(get_db),
):
    """Public endpoint for testing."""
    return _reset_tenant_password_internal(tenant_id, db)


def _delete_tenant_internal(
    tenant_id: int,
    db: Session,
):
    """
    Внутренняя функция для удаления жителя (без авторизации).
    """
    u = db.get(User, tenant_id)
    if not u or u.role != RoleEnum.RESIDENT:
        raise HTTPException(status_code=404, detail="Tenant not found")

    db.delete(u)
    db.commit()

    return {"success": True, "message": "Tenant deleted successfully"}


@router.delete("/{tenant_id}")
def delete_tenant_api(
    tenant_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Удалить жителя.
    """
    return _delete_tenant_internal(tenant_id, db)


@router.delete("/{tenant_id}/public")
def delete_tenant_public(
    tenant_id: int,
    db: Session = Depends(get_db),
):
    """Public endpoint for testing."""
    return _delete_tenant_internal(tenant_id, db)

