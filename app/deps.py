from fastapi import Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from .database import get_db
from .models import User, RoleEnum
from .security import get_user_id_from_session


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def require_any_role(*roles: RoleEnum):
    def _checker(user: User = Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return user
    return _checker


def require_root(user: User = Depends(get_current_user)):
    if user.role != RoleEnum.ROOT:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user


def can_manage_user(target: User, actor: User) -> bool:
    if actor.role == RoleEnum.ROOT:
        return True
    if actor.role == RoleEnum.ADMIN:
        return target.role in (RoleEnum.OPERATOR, RoleEnum.RESIDENT, RoleEnum.SALES)
    return False