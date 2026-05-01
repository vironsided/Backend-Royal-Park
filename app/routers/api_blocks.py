from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, select

from ..database import get_db
from ..models import Block
from ..deps import get_current_user, require_any_role
from ..models import User, RoleEnum

router = APIRouter(prefix="/api/blocks", tags=["blocks-api"])


class BlockOut(BaseModel):
    id: int
    name: str
    is_active: bool
    created_at: datetime
    created_by_id: Optional[int] = None

    class Config:
        from_attributes = True


class BlockCreate(BaseModel):
    name: str


class BlockUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("/", response_model=List[BlockOut])
def list_blocks_api(
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """
    JSON-список блоков для SPA-админки.
    """
    blocks = db.execute(select(Block).order_by(Block.id.asc())).scalars().all()
    return blocks


@router.post("/", response_model=BlockOut, status_code=status.HTTP_201_CREATED)
def create_block_api(
    payload: BlockCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """
    Создание блока из SPA.
    """
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Название не может быть пустым")

    # Проверка уникальности
    exists = db.execute(
        select(Block.id).where(func.lower(Block.name) == func.lower(name))
    ).first()
    if exists:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Блок с таким названием уже существует")

    block = Block(
        name=name,
        created_by_id=actor.id,
    )
    db.add(block)
    db.commit()
    db.refresh(block)
    return block


@router.put("/{block_id}", response_model=BlockOut)
def update_block_api(
    block_id: int,
    payload: BlockUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """
    Обновление блока (переименование, изменение статуса).
    """
    block = db.get(Block, block_id)
    if not block:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Блок не найден")

    if payload.name is not None:
        new_name = (payload.name or "").strip()
        if not new_name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Название не может быть пустым")
        
        # Проверка уникальности
        exists = db.execute(
            select(Block.id).where(
                func.lower(Block.name) == func.lower(new_name),
                Block.id != block_id,
            )
        ).first()
        if exists:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Блок с таким названием уже существует")
        
        block.name = new_name

    if payload.is_active is not None:
        block.is_active = payload.is_active

    db.commit()
    db.refresh(block)
    return block


@router.delete("/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_block_api(
    block_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    """
    Удаление блока.
    """
    block = db.get(Block, block_id)
    if not block:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Блок не найден")

    db.delete(block)
    db.commit()
    return None

