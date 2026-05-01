from fastapi import APIRouter, Depends
from ..deps import get_current_user
from ..models import RoleEnum
from ..frontend import redirect_frontend, redirect_admin

router = APIRouter()


@router.get("/")
def home(user=Depends(get_current_user)):
    if user.role == RoleEnum.RESIDENT:
        return redirect_frontend("/user/dashboard.html")
    return redirect_admin("/dashboard")
