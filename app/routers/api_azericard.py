from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import Invoice, OnlineTransaction, Payment, PaymentApplication, PaymentLog, PaymentMethod, Resident
from ..utils import now_baku
from ..services.azericard import (
    CALLBACK_SIGN_FIELDS,
    CREATE_SIGN_FIELDS,
    TERMINAL_CATEGORY_ADVANCE,
    TERMINAL_CATEGORY_MAINTENANCE,
    TERMINAL_CATEGORY_UTILITY,
    TERMINAL_GROUP_STANDARD,
    TERMINAL_GROUP_WALLET,
    _private_key,
    _terminal_group_from_data,
    _terminal_id_for,
    amount_to_gateway,
    build_nonce,
    build_order_id,
    build_timestamp,
    classify_invoice_amounts,
    generate_p_sign,
    terminal_group_for_online_trtype,
    verify_callback_signature,
)
from .api_payment_logic import apply_payment_to_invoice, apply_payment_to_invoices

router = APIRouter(prefix="/api/azericard", tags=["azericard-api"])


class InitiateRequest(BaseModel):
    resident_id: int
    amount: Decimal
    invoice_id: Optional[int] = None
    description: Optional[str] = None
    saved_card_id: Optional[int] = None
    terminal_category: Optional[str] = None
    wallet_provider: Optional[str] = None
    wallet_token: Optional[str] = None
    wallet_eci: Optional[str] = None
    wallet_tavv: Optional[str] = None


class CompleteRequest(BaseModel):
    order_id: str
    amount: Decimal
    currency: str = "AZN"
    rrn: str
    int_ref: str


@router.get("/wallet-config")
def wallet_config():
    gpay_merchant_name = (settings.AZERICARD_GPAY_MERCHANT_NAME or settings.AZERICARD_MERCH_NAME or "").strip()
    apple_merchant_name = (settings.AZERICARD_APPLE_MERCHANT_NAME or settings.AZERICARD_MERCH_NAME or "").strip()
    apple_merchant_id = (
        settings.AZERICARD_APPLE_MERCHANT_IDENTIFIER
        or settings.AZERICARD_APPLE_MERCHANT_ID
        or ""
    ).strip()
    return {
        "ok": True,
        "google_pay": {
            "environment": (settings.AZERICARD_GPAY_ENVIRONMENT or "TEST").strip().upper(),
            "gateway": (settings.AZERICARD_GPAY_GATEWAY or "azericardgpay").strip(),
            "gatewayMerchantId": (settings.AZERICARD_GPAY_GATEWAY_MERCHANT_ID or "").strip(),
            "merchantId": (settings.AZERICARD_GPAY_MERCHANT_ID or "").strip(),
            "merchantName": gpay_merchant_name,
            "merchantOrigin": (settings.AZERICARD_MERCH_URL or "").strip(),
            "currencyCode": (settings.AZERICARD_CURRENCY or "AZN").strip().upper(),
            "supported": bool((settings.AZERICARD_GPAY_GATEWAY_MERCHANT_ID or "").strip()),
        },
        "apple_pay": {
            "merchantIdentifier": apple_merchant_id,
            "merchantName": apple_merchant_name,
            "currencyCode": (settings.AZERICARD_CURRENCY or "AZN").strip().upper(),
            "supported": bool(apple_merchant_id),
        },
    }


def _ensure_gateway_config(category: Optional[str] = None, terminal_group: Optional[str] = None) -> None:
    tid = _terminal_id_for(category, terminal_group=terminal_group)
    if not tid:
        raise HTTPException(status_code=500, detail=f"AZERICARD_TERMINAL ({category or 'default'}) is not configured")
    _validate_public_url(settings.AZERICARD_CALLBACK_URL, "AZERICARD_CALLBACK_URL")
    _validate_public_url(settings.AZERICARD_SUCCESS_URL, "AZERICARD_SUCCESS_URL")
    _validate_public_url(settings.AZERICARD_FAIL_URL, "AZERICARD_FAIL_URL")


def _validate_public_url(value: str, env_name: str) -> None:
    raw = (value or "").strip()
    if not raw:
        raise HTTPException(status_code=500, detail=f"{env_name} is not configured")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=500,
            detail=f"{env_name} must be a full http/https URL (include host and port if non-standard)",
        )
    # Warn: Azericard cannot reach localhost/127.0.0.1 in production.
    hostname = (parsed.hostname or "").lower()
    if hostname in {"localhost", "127.0.0.1", "0.0.0.0"}:
        raise HTTPException(
            status_code=500,
            detail=(
                f"{env_name} is set to a localhost URL ({raw}). "
                "Azericard cannot deliver callbacks to localhost in production. "
                "Set a publicly reachable URL."
            ),
        )


def _ensure_signing_config(category: Optional[str] = None, terminal_group: Optional[str] = None) -> None:
    tid = _terminal_id_for(category, terminal_group=terminal_group)
    if not tid:
        raise HTTPException(status_code=500, detail=f"AZERICARD_TERMINAL ({category or 'default'}) is not configured")
    from ..services.azericard import _private_key_raw, _mpi_public_key
    raw = _private_key_raw(category, terminal_group=terminal_group)
    if not raw:
        raise HTTPException(status_code=500, detail=f"AZERICARD_PRIVATE_KEY ({category or 'default'}) is not configured")
    if "DUMMY_PRIVATE_KEY" in raw.upper():
        raise HTTPException(status_code=500, detail=f"AZERICARD_PRIVATE_KEY ({category or 'default'}) is still a dummy placeholder")
    try:
        _private_key(category, terminal_group=terminal_group)
    except Exception:
        raise HTTPException(status_code=500, detail=f"AZERICARD_PRIVATE_KEY ({category or 'default'}) has invalid PEM format")
    # MPI public key must be set for callback verification
    if not (settings.AZERICARD_MPI_PUBLIC_KEY or "").strip():
        raise HTTPException(status_code=500, detail="AZERICARD_MPI_PUBLIC_KEY is not configured (needed for callback verification)")
    try:
        _mpi_public_key()
    except Exception:
        raise HTTPException(status_code=500, detail="AZERICARD_MPI_PUBLIC_KEY has invalid PEM format")


def _pick(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        val = data.get(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return ""


def _wants_html(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept or "*/*" in accept


def _build_gateway_params(
    amount: Decimal,
    category: str,
    description: str,
    terminal_group: str = TERMINAL_GROUP_STANDARD,
    wallet_provider: Optional[str] = None,
    wallet_token: Optional[str] = None,
    wallet_eci: Optional[str] = None,
    wallet_tavv: Optional[str] = None,
) -> tuple[str, dict]:
    """Build signed gateway params for one terminal category. Returns (order_id, params)."""
    terminal_id = _terminal_id_for(category, terminal_group=terminal_group)
    order_id = build_order_id(terminal_id)
    amount_str = amount_to_gateway(amount)
    success_url = settings.AZERICARD_SUCCESS_URL
    fail_url = settings.AZERICARD_FAIL_URL

    req = {
        "AMOUNT": amount_str,
        "CURRENCY": settings.AZERICARD_CURRENCY,
        "ORDER": order_id,
        "DESC": description[:250],
        "MERCH_NAME": settings.AZERICARD_MERCH_NAME,
        # MERCH_URL must be the merchant's homepage, not the callback URL.
        # It is also the 7th field signed in P_SIGN per Azericard spec.
        "MERCH_URL": settings.AZERICARD_MERCH_URL,
        "TERMINAL": terminal_id,
        # Azericard test profile for this merchant accepts authorization TRTYPE=1.
        # Using TRTYPE=0 causes gateway "Giris qadagandir" rejection on this setup.
        "TRTYPE": "1",
        "TIMESTAMP": build_timestamp(),
        "NONCE": build_nonce(),
        "BACKREF": settings.AZERICARD_CALLBACK_URL,
        "COUNTRY": settings.AZERICARD_COUNTRY or "AZ",
        "MERCH_GMT": settings.AZERICARD_MERCH_GMT or "+4",
    }
    req["MERCH_URL_OK"] = success_url
    req["MERCH_URL_FAIL"] = fail_url
    if settings.AZERICARD_LANG:
        req["LANG"] = settings.AZERICARD_LANG

    # Wallet flows: Google Pay uses GPAYTOKEN (payment token JSON). Apple Pay may use
    # the same token field per MPI profile plus optional EXT_MPI_ECI / TAVV (see AzeriCard docs).
    if terminal_group == TERMINAL_GROUP_WALLET and wallet_provider:
        provider = wallet_provider.strip().lower()
        if provider == "google_pay" and wallet_token:
            req["GPAYTOKEN"] = wallet_token
        elif provider == "apple_pay":
            if wallet_token:
                req["GPAYTOKEN"] = wallet_token
            if wallet_eci:
                req["EXT_MPI_ECI"] = wallet_eci
            if wallet_tavv:
                req["TAVV"] = wallet_tavv

    req["P_SIGN"] = generate_p_sign(req, CREATE_SIGN_FIELDS, category, terminal_group=terminal_group)
    return order_id, req


@router.post("/initiate")
def initiate_payment(
    request: Request,
    payload: InitiateRequest,
    db: Session = Depends(get_db),
):
    resident = db.get(Resident, payload.resident_id)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")

    invoice = None
    if payload.invoice_id:
        invoice = db.get(Invoice, payload.invoice_id)
        if not invoice or invoice.resident_id != payload.resident_id:
            raise HTTPException(status_code=400, detail="Invoice does not belong to resident")
        paid_total = db.query(func.coalesce(func.sum(PaymentApplication.amount_applied), 0))\
                       .filter(PaymentApplication.invoice_id == invoice.id).scalar() or 0
        remaining = Decimal(str(invoice.amount_total or 0)) - Decimal(str(paid_total))
        if payload.amount > remaining and remaining > 0:
            raise HTTPException(
                status_code=400,
                detail=f"Amount {payload.amount} exceeds invoice remaining {remaining}",
            )

    description = payload.description or f"Resident #{payload.resident_id}"

    # Determine terminal category:
    #   - Frontend sends terminal_category explicitly (enforced by UI category lock)
    #   - No invoice → advance
    #   - Fallback → auto-classify from invoice lines
    VALID_CATEGORIES = {TERMINAL_CATEGORY_UTILITY, TERMINAL_CATEGORY_MAINTENANCE, TERMINAL_CATEGORY_ADVANCE}

    if not payload.invoice_id:
        category = TERMINAL_CATEGORY_ADVANCE
    elif payload.terminal_category and payload.terminal_category in VALID_CATEGORIES:
        category = payload.terminal_category
    else:
        # Fallback: auto-detect from invoice lines (pick the dominant category)
        classified = classify_invoice_amounts(db, payload.invoice_id)
        if classified:
            category = max(classified, key=classified.get)
        else:
            category = TERMINAL_CATEGORY_UTILITY

    wallet_provider = (payload.wallet_provider or "").strip().lower()
    is_wallet = wallet_provider in {"google_pay", "apple_pay"}
    terminal_group = TERMINAL_GROUP_WALLET if is_wallet else TERMINAL_GROUP_STANDARD

    _ensure_signing_config(category, terminal_group=terminal_group)

    order_id, params = _build_gateway_params(
        payload.amount,
        category,
        description,
        terminal_group=terminal_group,
        wallet_provider=wallet_provider,
        wallet_token=payload.wallet_token,
        wallet_eci=payload.wallet_eci,
        wallet_tavv=payload.wallet_tavv,
    )

    tx = OnlineTransaction(
        resident_id=payload.resident_id,
        invoice_id=payload.invoice_id,
        order_id=order_id,
        amount_total=payload.amount,
        currency=params["CURRENCY"],
        trtype=params.get("TRTYPE", "0"),
        terminal_category=category,
        gateway_status="INITIATED",
        request_payload=json.dumps(params, ensure_ascii=False),
    )
    db.add(tx)
    db.commit()

    return {
        "ok": True,
        "order_id": order_id,
        "gateway_url": settings.AZERICARD_GATEWAY_URL,
        "method": "POST",
        "params": params,
        "terminal_category": category,
    }


@router.post("/callback")
async def azericard_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    data: dict[str, str] = {}
    try:
        form = await request.form()
        data = {k: str(v) for k, v in form.items()}
    except Exception:
        data = {}
    if not data:
        try:
            body = await request.json()
            if isinstance(body, dict):
                data = {str(k): str(v) for k, v in body.items()}
        except Exception:
            data = {}
    order_id = _pick(data, "ORDER", "order")
    if not order_id:
        raise HTTPException(status_code=400, detail="ORDER is required")

    # Lock only the target row ID first to avoid PostgreSQL FOR UPDATE errors
    # on ORM-generated outer joins.
    tx_id = db.execute(
        select(OnlineTransaction.__table__.c.id)
        .where(OnlineTransaction.__table__.c.order_id == order_id)
        .with_for_update()
    ).scalar_one_or_none()
    if not tx_id:
        raise HTTPException(status_code=404, detail="Order not found")
    tx = db.get(OnlineTransaction, tx_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Order not found")
    if tx.payment_id:
        return {"ok": True, "order_id": order_id, "payment_id": tx.payment_id, "status": "ALREADY_PROCESSED"}

    tx.callback_payload = json.dumps(data, ensure_ascii=False)
    tx.rrn = _pick(data, "RRN", "rrn")
    tx.int_ref = _pick(data, "INT_REF", "int_ref")
    tx.approval = _pick(data, "APPROVAL", "approval")
    tx.action_code = _pick(data, "ACTION", "action")
    tx.rc = _pick(data, "RC", "rc")
    tx.trtype = _pick(data, "TRTYPE", "trtype") or tx.trtype

    category = tx.terminal_category
    callback_group = _terminal_group_from_data(data)
    _ensure_gateway_config(category, terminal_group=callback_group)

    signature_ok = verify_callback_signature(data, category=category, terminal_group=callback_group)
    action = _pick(data, "ACTION", "action")
    status_ok = action == "0"
    if not signature_ok:
        tx.gateway_status = "SIGNATURE_FAILED"
        db.commit()
        if _wants_html(request):
            return RedirectResponse(url=f"/api/azericard/fail?order_id={order_id}&reason=signature", status_code=302)
        raise HTTPException(status_code=400, detail="Invalid callback signature")

    if not status_ok:
        tx.gateway_status = "DECLINED"
        db.commit()
        if _wants_html(request):
            return RedirectResponse(url=f"/api/azericard/fail?order_id={order_id}&reason=declined", status_code=302)
        return {"ok": False, "order_id": order_id, "status": "DECLINED"}

    payment_id = _confirm_local_transaction_from_callback(
        db=db,
        tx=tx,
        callback_data=data,
        log_action="GATEWAY_CALLBACK",
    )
    db.commit()
    if _wants_html(request):
        return RedirectResponse(url=f"/api/azericard/success?order_id={order_id}", status_code=302)
    return {"ok": True, "order_id": order_id, "payment_id": payment_id}


def _try_save_card_token(db: Session, tx: OnlineTransaction, callback_data: dict) -> None:
    """If AzeriCard returns a card token in the callback, persist it as SavedCard."""
    from ..models import SavedCard, user_residents

    token = _pick(callback_data, "CARD_TOKEN", "card_token", "TOKEN", "token")
    if not token:
        return

    masked = _pick(callback_data, "CARD_MASK", "card_mask", "MASKED_PAN", "masked_pan", "PAN")
    if masked and len(masked) > 4:
        masked = "****" + masked[-4:]
    elif not masked:
        masked = "****"

    brand = _pick(callback_data, "CARD_BRAND", "card_brand", "CARD_TYPE", "card_type")

    if not tx.resident_id:
        return
    user_link = db.query(user_residents.c.user_id).filter(
        user_residents.c.resident_id == tx.resident_id
    ).first()
    if not user_link:
        return

    user_id = user_link[0]
    existing = db.query(SavedCard).filter(SavedCard.user_id == user_id, SavedCard.token_id == token).first()
    if existing:
        return

    card_count = db.query(SavedCard).filter(SavedCard.user_id == user_id).count()
    if card_count >= 5:
        return

    db.add(SavedCard(
        user_id=user_id,
        token_id=token,
        masked_pan=masked,
        card_brand=brand or None,
        is_default=(card_count == 0),
    ))
    db.flush()


def _confirm_local_transaction_from_callback(
    db: Session,
    tx: OnlineTransaction,
    callback_data: dict[str, str],
    *,
    log_action: str = "GATEWAY_CALLBACK",
) -> int:
    """Create local payment + applications from an already validated callback payload."""
    order_id = tx.order_id
    category = tx.terminal_category
    payment = Payment(
        resident_id=tx.resident_id,
        received_at=now_baku(),
        amount_total=tx.amount_total,
        method=PaymentMethod.ONLINE,
        reference=order_id,
        comment=f"AzeriCard online payment ({category or 'default'})",
        created_by_id=None,
    )
    db.add(payment)
    db.flush()

    tx.payment_id = payment.id
    tx.gateway_status = "CONFIRMED"

    applied_amount = Decimal("0")
    auto_applied_count = 0
    if tx.invoice_id:
        applied_amount = apply_payment_to_invoice(
            db=db,
            payment_id=payment.id,
            invoice_id=tx.invoice_id,
            reference=f"AZERICARD:{order_id}",
        )
        # For direct invoice payments, preserve existing behavior:
        # distribute any payment leftover across other open invoices.
        auto_applied_count = apply_payment_to_invoices(
            db=db,
            payment_id=payment.id,
            resident_id=tx.resident_id,
            scope="all",
        )
    # If invoice_id is empty, this is an advance top-up flow.
    # Keep the amount as resident advance (payment leftover) and do not auto-apply.

    _try_save_card_token(db, tx, callback_data)
    db.add(
        PaymentLog(
            payment_id=payment.id,
            resident_id=tx.resident_id,
            user_id=None,
            action=log_action,
            amount=float(tx.amount_total or 0),
            details=(
                f"AzeriCard confirmed ORDER={order_id} [{category}]; "
                f"invoice_applied={float(applied_amount)}; auto_applied_count={auto_applied_count}"
            ),
        )
    )
    return payment.id


@router.get("/status/{order_id}")
async def get_status(order_id: str, db: Session = Depends(get_db)):
    tx = db.query(OnlineTransaction).filter(OnlineTransaction.order_id == order_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Order not found")

    # Recovery path: older callbacks were marked SIGNATURE_FAILED due to a stricter
    # field layout assumption. Re-validate stored callback payload with current rules
    # and finalize locally if the callback was genuinely successful.
    if tx.gateway_status == "SIGNATURE_FAILED" and not tx.payment_id and tx.callback_payload:
        try:
            callback_data = json.loads(tx.callback_payload)
            if isinstance(callback_data, dict):
                callback_data = {str(k): str(v) for k, v in callback_data.items()}
            else:
                callback_data = {}
        except Exception:
            callback_data = {}

        if callback_data:
            callback_group = _terminal_group_from_data(callback_data)
            signature_ok = verify_callback_signature(
                callback_data,
                category=tx.terminal_category,
                terminal_group=callback_group,
            )
            action_ok = _pick(callback_data, "ACTION", "action") == "0"
            if signature_ok and action_ok:
                _confirm_local_transaction_from_callback(
                    db=db,
                    tx=tx,
                    callback_data=callback_data,
                    log_action="GATEWAY_CALLBACK_RECOVERED",
                )
                db.commit()

    category = tx.terminal_category
    group = terminal_group_for_online_trtype(tx.trtype)
    _ensure_gateway_config(category, terminal_group=group)

    terminal_id = _terminal_id_for(category, terminal_group=group)
    payload = {
        "ORDER": tx.order_id,
        "TERMINAL": terminal_id,
        "TRTYPE": "90",
        "TIMESTAMP": build_timestamp(),
        "NONCE": build_nonce(),
    }
    payload["P_SIGN"] = generate_p_sign(
        payload,
        ["ORDER", "TERMINAL", "TRTYPE", "TIMESTAMP", "NONCE"],
        category,
        terminal_group=group,
    )

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(settings.AZERICARD_API_URL, data=payload)
    return {
        "ok": response.status_code == 200,
        "http_status": response.status_code,
        "gateway_response": response.text,
        "local_status": tx.gateway_status,
    }


@router.post("/complete")
async def complete_payment(payload: CompleteRequest, db: Session = Depends(get_db)):
    tx = db.query(OnlineTransaction).filter(OnlineTransaction.order_id == payload.order_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Order not found")
    category = tx.terminal_category
    group = terminal_group_for_online_trtype(tx.trtype)
    _ensure_gateway_config(category, terminal_group=group)

    terminal_id = _terminal_id_for(category, terminal_group=group)
    req = {
        "ORDER": payload.order_id,
        "AMOUNT": amount_to_gateway(payload.amount),
        "CURRENCY": payload.currency,
        "RRN": payload.rrn,
        "INT_REF": payload.int_ref,
        "TERMINAL": terminal_id,
        "TRTYPE": "21",
        "TIMESTAMP": build_timestamp(),
        "NONCE": build_nonce(),
    }
    req["P_SIGN"] = generate_p_sign(req, CALLBACK_SIGN_FIELDS, category, terminal_group=group)

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(settings.AZERICARD_API_URL, data=req)
    return {"ok": response.status_code == 200, "http_status": response.status_code, "gateway_response": response.text}


# ---------------------------------------------------------------------------
# Saved Cards CRUD
# ---------------------------------------------------------------------------

@router.get("/saved-cards")
def list_saved_cards(request: Request, db: Session = Depends(get_db)):
    """List saved cards for the currently authenticated user."""
    from ..models import SavedCard
    user_id = _get_session_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    cards = (
        db.query(SavedCard)
        .filter(SavedCard.user_id == user_id)
        .order_by(SavedCard.is_default.desc(), SavedCard.created_at.desc())
        .all()
    )
    return [
        {
            "id": c.id,
            "masked_pan": c.masked_pan,
            "card_brand": c.card_brand,
            "expiry_month": c.expiry_month,
            "expiry_year": c.expiry_year,
            "is_default": c.is_default,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in cards
    ]


@router.delete("/saved-cards/{card_id}")
def delete_saved_card(card_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a saved card belonging to the current user."""
    from ..models import SavedCard
    user_id = _get_session_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    card = db.query(SavedCard).filter(SavedCard.id == card_id, SavedCard.user_id == user_id).first()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    was_default = card.is_default
    db.delete(card)
    db.flush()

    if was_default:
        next_card = db.query(SavedCard).filter(SavedCard.user_id == user_id).order_by(SavedCard.created_at.desc()).first()
        if next_card:
            next_card.is_default = True

    db.commit()
    return {"ok": True}


@router.post("/saved-cards/{card_id}/set-default")
def set_default_card(card_id: int, request: Request, db: Session = Depends(get_db)):
    """Set a saved card as the default for the current user."""
    from ..models import SavedCard
    user_id = _get_session_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    card = db.query(SavedCard).filter(SavedCard.id == card_id, SavedCard.user_id == user_id).first()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    db.query(SavedCard).filter(SavedCard.user_id == user_id).update({"is_default": False})
    card.is_default = True
    db.commit()
    return {"ok": True}


def _get_session_user_id(request: Request) -> Optional[int]:
    """Extract user_id from session (works with SessionMiddleware)."""
    session = getattr(request, "session", None)
    if session:
        uid = session.get("user_id")
        if uid is not None:
            try:
                return int(uid)
            except (ValueError, TypeError):
                pass
    return None


@router.get("/success", response_class=HTMLResponse)
def payment_success_page(order_id: str | None = None):
    order_suffix = f"&order_id={order_id}" if order_id else ""
    return f"""
    <html><body style="font-family:Arial,sans-serif;padding:24px;">
      <h2>Payment successful</h2>
      <p>Your payment has been confirmed.</p>
      <a href="/resident/invoices?ok=online_payment_success{order_suffix}">Back to invoices</a>
    </body></html>
    """


@router.get("/fail", response_class=HTMLResponse)
def payment_fail_page(order_id: str | None = None):
    order_suffix = f"&order_id={order_id}" if order_id else ""
    return f"""
    <html><body style="font-family:Arial,sans-serif;padding:24px;">
      <h2>Payment failed</h2>
      <p>The payment was declined or canceled.</p>
      <a href="/resident/invoices?ok=online_payment_failed{order_suffix}">Back to invoices</a>
    </body></html>
    """
