from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from ..config import settings
from ..models import PushDeviceToken

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
except Exception:  # pragma: no cover
    firebase_admin = None
    credentials = None
    messaging = None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_locale(locale: str | None) -> str:
    code = (locale or "").strip().lower()
    if code in {"az", "en", "ru"}:
        return code
    return "az"


def is_push_available() -> bool:
    return bool(
        firebase_admin
        and (settings.FIREBASE_CREDENTIALS_PATH or settings.FIREBASE_CREDENTIALS_JSON)
    )


def _load_credentials_from_env_json(raw_json: str) -> dict:
    """
    Parses FIREBASE_CREDENTIALS_JSON from env.

    Supports:
    1) Valid JSON where private_key contains escaped newlines (\\n)
    2) Broken input where private_key contains literal newlines
    """
    text = (raw_json or "").strip()
    if not text:
        raise ValueError("Empty FIREBASE_CREDENTIALS_JSON")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to normalize literal newlines inside private_key value only.
        normalized = re.sub(
            r'("private_key"\s*:\s*")(.+?)(")',
            lambda m: m.group(1) + m.group(2).replace("\r\n", "\n").replace("\n", "\\n") + m.group(3),
            text,
            flags=re.DOTALL,
        )
        return json.loads(normalized)


def ensure_firebase_initialized() -> bool:
    if not is_push_available():
        return False
    try:
        if firebase_admin._apps:  # type: ignore[attr-defined]
            return True
        if settings.FIREBASE_CREDENTIALS_JSON:
            cred_data = _load_credentials_from_env_json(settings.FIREBASE_CREDENTIALS_JSON)
            cred = credentials.Certificate(cred_data)
        else:
            cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS_PATH)
        firebase_admin.initialize_app(cred, {"projectId": settings.FIREBASE_PROJECT_ID or None})
        return True
    except Exception as exc:
        print(f"FCM init failed: {exc}")
        return False


def register_device_token(
    db: Session,
    *,
    user_id: int,
    token: str,
    platform: str,
    device_id: str | None = None,
    device_name: str | None = None,
    app_version: str | None = None,
    os_version: str | None = None,
    locale: str | None = None,
) -> None:
    cleaned_token = (token or "").strip()
    if not cleaned_token:
        return
    token_hash = _sha256(cleaned_token)
    now = _now_utc()

    existing_by_hash = db.query(PushDeviceToken).filter(PushDeviceToken.token_hash == token_hash).first()
    if existing_by_hash:
        existing_by_hash.user_id = user_id
        existing_by_hash.token = cleaned_token
        existing_by_hash.platform = (platform or "android").lower()
        existing_by_hash.device_id = device_id
        existing_by_hash.device_name = device_name
        existing_by_hash.app_version = app_version
        existing_by_hash.os_version = os_version
        existing_by_hash.locale = _normalize_locale(locale)
        existing_by_hash.is_active = True
        existing_by_hash.invalidated_at = None
        existing_by_hash.invalidation_reason = None
        existing_by_hash.last_seen_at = now
        existing_by_hash.updated_at = now
        db.commit()
        return

    if device_id:
        stale = (
            db.query(PushDeviceToken)
            .filter(
                PushDeviceToken.user_id == user_id,
                PushDeviceToken.device_id == device_id,
                PushDeviceToken.platform == (platform or "android").lower(),
                PushDeviceToken.is_active == True,
            )
            .all()
        )
        for row in stale:
            row.is_active = False
            row.invalidated_at = now
            row.invalidation_reason = "REPLACED"
            row.updated_at = now

    db.add(
        PushDeviceToken(
            user_id=user_id,
            token=cleaned_token,
            token_hash=token_hash,
            platform=(platform or "android").lower(),
            device_id=device_id,
            device_name=device_name,
            app_version=app_version,
            os_version=os_version,
            locale=_normalize_locale(locale),
            is_active=True,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()


def unregister_device_token(db: Session, *, user_id: int, token: str | None = None, device_id: str | None = None) -> int:
    now = _now_utc()
    query = db.query(PushDeviceToken).filter(PushDeviceToken.user_id == user_id, PushDeviceToken.is_active == True)
    if token:
        query = query.filter(PushDeviceToken.token_hash == _sha256(token.strip()))
    if device_id:
        query = query.filter(PushDeviceToken.device_id == device_id)
    rows = query.all()
    for row in rows:
        row.is_active = False
        row.invalidated_at = now
        row.invalidation_reason = "LOGOUT"
        row.updated_at = now
    if rows:
        db.commit()
    return len(rows)


def _mark_invalid_tokens(db: Session, token_values: Iterable[str], reason: str) -> None:
    token_hashes = [_sha256(t.strip()) for t in token_values if (t or "").strip()]
    if not token_hashes:
        return
    now = _now_utc()
    rows = db.query(PushDeviceToken).filter(PushDeviceToken.token_hash.in_(token_hashes)).all()
    for row in rows:
        row.is_active = False
        row.invalidated_at = now
        row.invalidation_reason = reason
        row.last_error_at = now
        row.last_error_code = reason
        row.updated_at = now
    if rows:
        db.commit()


def send_push_to_users(
    db: Session,
    *,
    user_ids: list[int],
    title: str,
    body: str,
    data: dict[str, str] | None = None,
) -> None:
    if not user_ids:
        return
    if not ensure_firebase_initialized():
        return

    rows = (
        db.query(PushDeviceToken)
        .filter(PushDeviceToken.user_id.in_(user_ids), PushDeviceToken.is_active == True)
        .all()
    )
    tokens = [row.token for row in rows if (row.token or "").strip()]
    if not tokens:
        return

    now = _now_utc()
    try:
        chunk_size = 500
        for i in range(0, len(tokens), chunk_size):
            batch_tokens = tokens[i : i + chunk_size]
            message = messaging.MulticastMessage(
                notification=messaging.Notification(title=title, body=body),
                data={k: str(v) for k, v in (data or {}).items()},
                tokens=batch_tokens,
                android=messaging.AndroidConfig(priority="high"),
                apns=messaging.APNSConfig(
                    headers={"apns-priority": "10"},
                    payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")),
                ),
            )
            response = messaging.send_each_for_multicast(message)
            bad_tokens: list[str] = []
            for idx, res in enumerate(response.responses):
                token_value = batch_tokens[idx]
                if res.success:
                    continue
                err_code = getattr(getattr(res, "exception", None), "code", "") or ""
                if err_code in {"registration-token-not-registered", "invalid-argument"}:
                    bad_tokens.append(token_value)
            if bad_tokens:
                _mark_invalid_tokens(db, bad_tokens, reason="UNREGISTERED")

        for row in rows:
            row.last_sent_at = now
            row.updated_at = now
        db.commit()
    except Exception as exc:
        print(f"FCM send failed: {exc}")

