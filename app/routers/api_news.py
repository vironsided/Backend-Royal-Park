"""
API endpoints for News management
Supports multilingual content (ru, az, en)
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime
import json

from ..database import get_db
from ..models import News, User, RoleEnum
from ..deps import get_current_user
from ..utils import create_news_notification


router = APIRouter(prefix="/api/news", tags=["news-api"])


class NewsTranslation(BaseModel):
    ru: str
    az: str
    en: str


class NewsCreate(BaseModel):
    title: NewsTranslation
    content: NewsTranslation
    icon: str = "info"
    icon_color: str = "#667eea"
    is_active: bool = True
    priority: int = 0
    published_at: Optional[str] = None  # Accept ISO string, convert to datetime
    expires_at: Optional[str] = None  # Accept ISO string, convert to datetime
    target_blocks: Optional[List[str]] = None


class NewsUpdate(BaseModel):
    title: Optional[NewsTranslation] = None
    content: Optional[NewsTranslation] = None
    icon: Optional[str] = None
    icon_color: Optional[str] = None
    is_active: Optional[bool] = None
    priority: Optional[int] = None
    published_at: Optional[str] = None  # Accept ISO string
    expires_at: Optional[str] = None  # Accept ISO string
    target_blocks: Optional[List[str]] = None


class NewsOut(BaseModel):
    id: int
    title: dict
    content: dict
    icon: str
    icon_color: str
    is_active: bool
    priority: int
    published_at: datetime
    expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    created_by_name: Optional[str] = None
    target_blocks: Optional[List[str]] = None

    class Config:
        from_attributes = True


class NewsListOut(BaseModel):
    items: List[NewsOut]
    total: int


# Public endpoint - get active news for users
@router.get("/public", response_model=NewsListOut)
def get_public_news(
    db: Session = Depends(get_db),
    lang: Optional[str] = Query("ru", regex="^(ru|az|en)$"),
    limit: int = Query(10, ge=1, le=100),
    blocks: Optional[str] = Query(None, description="Comma-separated block names"),
):
    """
    Get list of active news for users.
    Returns only active news that are published and not expired.
    """
    now = datetime.utcnow()
    
    query = db.query(News).filter(
        News.is_active == True,
        News.published_at <= now
    )
    
    # Filter by expiration
    from sqlalchemy import or_
    query = query.filter(
        or_(News.expires_at.is_(None), News.expires_at > now)
    )
    
    items = query.order_by(News.priority.desc(), News.published_at.desc()).all()

    block_filter = []
    if blocks:
        block_filter = [b.strip() for b in blocks.split(",") if b.strip()]
    
    result_items = []
    for item in items:
        # Parse JSON fields
        title_dict = json.loads(item.title) if isinstance(item.title, str) else item.title
        content_dict = json.loads(item.content) if isinstance(item.content, str) else item.content
        target_blocks = None
        if item.target_blocks:
            try:
                target_blocks = json.loads(item.target_blocks)
            except Exception:
                target_blocks = None

        if block_filter and target_blocks:
            if not any(b in target_blocks for b in block_filter):
                continue
        
        result_items.append(NewsOut(
            id=item.id,
            title=title_dict,
            content=content_dict,
            icon=item.icon,
            icon_color=item.icon_color,
            is_active=item.is_active,
            priority=item.priority,
            published_at=item.published_at,
            expires_at=item.expires_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
            created_by_name=item.created_by.full_name if item.created_by else None,
            target_blocks=target_blocks,
        ))
    
    result_items = result_items[:limit]
    return {"items": result_items, "total": len(result_items)}


# Admin endpoints - CRUD operations
@router.get("/admin", response_model=NewsListOut)
def list_news_admin(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
):
    """Get all news for admin panel with pagination."""
    query = db.query(News)
    total = query.count()
    
    items = query.order_by(News.priority.desc(), News.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    
    result_items = []
    for item in items:
        title_dict = json.loads(item.title) if isinstance(item.title, str) else item.title
        content_dict = json.loads(item.content) if isinstance(item.content, str) else item.content
        target_blocks = None
        if item.target_blocks:
            try:
                target_blocks = json.loads(item.target_blocks)
            except Exception:
                target_blocks = None
        
        result_items.append(NewsOut(
            id=item.id,
            title=title_dict,
            content=content_dict,
            icon=item.icon,
            icon_color=item.icon_color,
            is_active=item.is_active,
            priority=item.priority,
            published_at=item.published_at,
            expires_at=item.expires_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
            created_by_name=item.created_by.full_name if item.created_by else None,
            target_blocks=target_blocks,
        ))
    
    return {"items": result_items, "total": total}


@router.post("/admin", response_model=NewsOut, status_code=status.HTTP_201_CREATED)
def create_news(
    payload: NewsCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create new news item."""
    # Convert ISO string dates to datetime objects
    published_at_dt = None
    if payload.published_at:
        try:
            published_at_dt = datetime.fromisoformat(payload.published_at.replace('Z', '+00:00'))
        except:
            published_at_dt = datetime.utcnow()
    else:
        published_at_dt = datetime.utcnow()
    
    expires_at_dt = None
    if payload.expires_at:
        try:
            expires_at_dt = datetime.fromisoformat(payload.expires_at.replace('Z', '+00:00'))
        except:
            expires_at_dt = None
    
    news = News(
        title=json.dumps(payload.title.dict()),
        content=json.dumps(payload.content.dict()),
        icon=payload.icon,
        icon_color=payload.icon_color,
        is_active=payload.is_active,
        priority=payload.priority,
        published_at=published_at_dt,
        expires_at=expires_at_dt,
        created_by_id=user.id,
        target_blocks=json.dumps(payload.target_blocks) if payload.target_blocks else None,
    )
    
    db.add(news)
    db.commit()
    db.refresh(news)
    
    # Создаем уведомления для пользователей, если новость активна и опубликована
    # Функция create_news_notification сама проверит время и создаст уведомления
    if news.is_active and news.published_at:
        create_news_notification(db, news)
    
    title_dict = json.loads(news.title)
    content_dict = json.loads(news.content)
    
    return NewsOut(
        id=news.id,
        title=title_dict,
        content=content_dict,
        icon=news.icon,
        icon_color=news.icon_color,
        is_active=news.is_active,
        priority=news.priority,
        published_at=news.published_at,
        expires_at=news.expires_at,
        created_at=news.created_at,
        updated_at=news.updated_at,
        created_by_name=news.created_by.full_name if news.created_by else None,
        target_blocks=payload.target_blocks if payload.target_blocks else None,
    )


@router.get("/admin/{news_id}", response_model=NewsOut)
def get_news_admin(
    news_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get news by ID for admin."""
    news = db.get(News, news_id)
    if not news:
        raise HTTPException(status_code=404, detail="News not found")
    
    title_dict = json.loads(news.title) if isinstance(news.title, str) else news.title
    content_dict = json.loads(news.content) if isinstance(news.content, str) else news.content
    
    target_blocks = None
    if news.target_blocks:
        try:
            target_blocks = json.loads(news.target_blocks)
        except Exception:
            target_blocks = None

    return NewsOut(
        id=news.id,
        title=title_dict,
        content=content_dict,
        icon=news.icon,
        icon_color=news.icon_color,
        is_active=news.is_active,
        priority=news.priority,
        published_at=news.published_at,
        expires_at=news.expires_at,
        created_at=news.created_at,
        updated_at=news.updated_at,
        created_by_name=news.created_by.full_name if news.created_by else None,
        target_blocks=target_blocks,
    )


@router.put("/admin/{news_id}", response_model=NewsOut)
def update_news(
    news_id: int,
    payload: NewsUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update news item."""
    news = db.get(News, news_id)
    if not news:
        raise HTTPException(status_code=404, detail="News not found")
    
    if payload.title is not None:
        news.title = json.dumps(payload.title.dict())
    if payload.content is not None:
        news.content = json.dumps(payload.content.dict())
    if payload.icon is not None:
        news.icon = payload.icon
    if payload.icon_color is not None:
        news.icon_color = payload.icon_color
    if payload.is_active is not None:
        news.is_active = payload.is_active
    if payload.priority is not None:
        news.priority = payload.priority
    if payload.published_at is not None:
        try:
            news.published_at = datetime.fromisoformat(payload.published_at.replace('Z', '+00:00'))
        except:
            pass  # Keep existing value if parsing fails
    if payload.expires_at is not None:
        if payload.expires_at:
            try:
                news.expires_at = datetime.fromisoformat(payload.expires_at.replace('Z', '+00:00'))
            except:
                pass
        else:
            news.expires_at = None  # Clear expiration if empty string
    if payload.target_blocks is not None:
        news.target_blocks = json.dumps(payload.target_blocks) if payload.target_blocks else None
    
    news.updated_at = datetime.utcnow()
    
    db.commit()
    db.refresh(news)
    
    # Создаем уведомления для пользователей, если новость стала активной и опубликована
    # Функция create_news_notification сама проверит время и создаст уведомления
    # Создаем уведомления только если новость стала активной или была обновлена
    if news.is_active and news.published_at:
        create_news_notification(db, news)
    
    title_dict = json.loads(news.title) if isinstance(news.title, str) else news.title
    content_dict = json.loads(news.content) if isinstance(news.content, str) else news.content
    
    target_blocks = None
    if news.target_blocks:
        try:
            target_blocks = json.loads(news.target_blocks)
        except Exception:
            target_blocks = None

    return NewsOut(
        id=news.id,
        title=title_dict,
        content=content_dict,
        icon=news.icon,
        icon_color=news.icon_color,
        is_active=news.is_active,
        priority=news.priority,
        published_at=news.published_at,
        expires_at=news.expires_at,
        created_at=news.created_at,
        updated_at=news.updated_at,
        created_by_name=news.created_by.full_name if news.created_by else None,
        target_blocks=target_blocks,
    )


@router.delete("/admin/{news_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_news(
    news_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete news item."""
    news = db.get(News, news_id)
    if not news:
        raise HTTPException(status_code=404, detail="News not found")
    
    db.delete(news)
    db.commit()
    return None

