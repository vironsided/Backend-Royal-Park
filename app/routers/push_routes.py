from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import User
from ..services.push_service import register_device_token, unregister_device_token

router = APIRouter(prefix="/api/push", tags=["push-api"])


class PushTokenRegisterRequest(BaseModel):
    token: str = Field(..., min_length=10)
    platform: str = Field(..., pattern="^(android|ios|web)$")
    device_id: str | None = None
    device_name: str | None = None
    app_version: str | None = None
    os_version: str | None = None
    locale: str | None = None


class PushTokenUnregisterRequest(BaseModel):
    token: str | None = None
    device_id: str | None = None


@router.post("/register-token")
def register_token(
    payload: PushTokenRegisterRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    register_device_token(
        db,
        user_id=user.id,
        token=payload.token,
        platform=payload.platform,
        device_id=payload.device_id,
        device_name=payload.device_name,
        app_version=payload.app_version,
        os_version=payload.os_version,
        locale=payload.locale,
    )
    return {"ok": True}


@router.post("/unregister-token")
def unregister_token(
    payload: PushTokenUnregisterRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    affected = unregister_device_token(
        db,
        user_id=user.id,
        token=payload.token,
        device_id=payload.device_id,
    )
    return {"ok": True, "affected": affected}

