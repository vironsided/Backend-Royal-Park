from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key

from ..config import settings


CREATE_SIGN_FIELDS = ["AMOUNT", "CURRENCY", "TERMINAL", "TRTYPE", "TIMESTAMP", "NONCE", "BACKREF"]
CALLBACK_SIGN_FIELDS = ["AMOUNT", "CURRENCY", "TERMINAL", "TRTYPE", "ORDER", "RRN", "INT_REF"]

UTILITY_METER_TYPES = {"ELECTRIC", "GAS", "WATER", "SEWERAGE"}
MAINTENANCE_METER_TYPES = {"SERVICE", "RENT", "CONSTRUCTION"}

TERMINAL_CATEGORY_UTILITY = "utility"
TERMINAL_CATEGORY_MAINTENANCE = "maintenance"
TERMINAL_CATEGORY_ADVANCE = "advance"
TERMINAL_GROUP_STANDARD = "standard"
TERMINAL_GROUP_WALLET = "wallet"


def terminal_group_for_online_trtype(trtype: Optional[str]) -> str:
    """Gateway wallet purchases use TRTYPE 1; status/complete must use the same terminal keys."""
    if (trtype or "").strip() == "1":
        return TERMINAL_GROUP_WALLET
    return TERMINAL_GROUP_STANDARD


# ---------------------------------------------------------------------------
# PEM helpers
# ---------------------------------------------------------------------------

def _as_pem(raw: str, key_kind: str) -> str:
    value = (raw or "").strip().replace("\\n", "\n")
    if not value:
        return value
    if "BEGIN" in value:
        return value
    clean = "".join(value.split())
    if key_kind == "private":
        return (
            "-----BEGIN PRIVATE KEY-----\n"
            f"{clean}\n"
            "-----END PRIVATE KEY-----"
        )
    return (
        "-----BEGIN PUBLIC KEY-----\n"
        f"{clean}\n"
        "-----END PUBLIC KEY-----"
    )


# ---------------------------------------------------------------------------
# Per-category terminal credentials with fallback to legacy single terminal
# ---------------------------------------------------------------------------

def _normalize_terminal_id(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _wallet_terminal_id_for(category: Optional[str]) -> str:
    if category == TERMINAL_CATEGORY_UTILITY and settings.AZERICARD_TERMINAL_WALLET_UTILITY:
        return settings.AZERICARD_TERMINAL_WALLET_UTILITY
    if category == TERMINAL_CATEGORY_MAINTENANCE and settings.AZERICARD_TERMINAL_WALLET_MAINTENANCE:
        return settings.AZERICARD_TERMINAL_WALLET_MAINTENANCE
    if category == TERMINAL_CATEGORY_ADVANCE and settings.AZERICARD_TERMINAL_WALLET_ADVANCE:
        return settings.AZERICARD_TERMINAL_WALLET_ADVANCE
    return settings.AZERICARD_TERMINAL_WALLET


def _wallet_private_key_raw(category: Optional[str]) -> str:
    if category == TERMINAL_CATEGORY_UTILITY and settings.AZERICARD_PRIVATE_KEY_WALLET_UTILITY:
        return settings.AZERICARD_PRIVATE_KEY_WALLET_UTILITY
    if category == TERMINAL_CATEGORY_MAINTENANCE and settings.AZERICARD_PRIVATE_KEY_WALLET_MAINTENANCE:
        return settings.AZERICARD_PRIVATE_KEY_WALLET_MAINTENANCE
    if category == TERMINAL_CATEGORY_ADVANCE and settings.AZERICARD_PRIVATE_KEY_WALLET_ADVANCE:
        return settings.AZERICARD_PRIVATE_KEY_WALLET_ADVANCE
    return settings.AZERICARD_PRIVATE_KEY_WALLET


def _wallet_public_key_raw(category: Optional[str]) -> str:
    if category == TERMINAL_CATEGORY_UTILITY and settings.AZERICARD_PUBLIC_KEY_WALLET_UTILITY:
        return settings.AZERICARD_PUBLIC_KEY_WALLET_UTILITY
    if category == TERMINAL_CATEGORY_MAINTENANCE and settings.AZERICARD_PUBLIC_KEY_WALLET_MAINTENANCE:
        return settings.AZERICARD_PUBLIC_KEY_WALLET_MAINTENANCE
    if category == TERMINAL_CATEGORY_ADVANCE and settings.AZERICARD_PUBLIC_KEY_WALLET_ADVANCE:
        return settings.AZERICARD_PUBLIC_KEY_WALLET_ADVANCE
    return settings.AZERICARD_PUBLIC_KEY_WALLET


def _all_wallet_terminal_ids_normalized() -> frozenset[str]:
    ids: set[str] = set()
    for raw in (
        settings.AZERICARD_TERMINAL_WALLET_UTILITY,
        settings.AZERICARD_TERMINAL_WALLET_MAINTENANCE,
        settings.AZERICARD_TERMINAL_WALLET_ADVANCE,
        settings.AZERICARD_TERMINAL_WALLET,
    ):
        n = _normalize_terminal_id(raw)
        if n:
            ids.add(n)
    return frozenset(ids)


def _terminal_id_for(category: Optional[str] = None, terminal_group: Optional[str] = None) -> str:
    if terminal_group == TERMINAL_GROUP_WALLET:
        return _wallet_terminal_id_for(category)
    if category == TERMINAL_CATEGORY_UTILITY and settings.AZERICARD_TERMINAL_UTILITY:
        return settings.AZERICARD_TERMINAL_UTILITY
    if category == TERMINAL_CATEGORY_MAINTENANCE and settings.AZERICARD_TERMINAL_MAINTENANCE:
        return settings.AZERICARD_TERMINAL_MAINTENANCE
    if category == TERMINAL_CATEGORY_ADVANCE and settings.AZERICARD_TERMINAL_ADVANCE:
        return settings.AZERICARD_TERMINAL_ADVANCE
    return settings.AZERICARD_TERMINAL_ID


def _private_key_raw(category: Optional[str] = None, terminal_group: Optional[str] = None) -> str:
    if terminal_group == TERMINAL_GROUP_WALLET:
        return _wallet_private_key_raw(category)
    if category == TERMINAL_CATEGORY_UTILITY and settings.AZERICARD_PRIVATE_KEY_UTILITY:
        return settings.AZERICARD_PRIVATE_KEY_UTILITY
    if category == TERMINAL_CATEGORY_MAINTENANCE and settings.AZERICARD_PRIVATE_KEY_MAINTENANCE:
        return settings.AZERICARD_PRIVATE_KEY_MAINTENANCE
    if category == TERMINAL_CATEGORY_ADVANCE and settings.AZERICARD_PRIVATE_KEY_ADVANCE:
        return settings.AZERICARD_PRIVATE_KEY_ADVANCE
    return settings.AZERICARD_PRIVATE_KEY


def _public_key_raw(category: Optional[str] = None, terminal_group: Optional[str] = None) -> str:
    if terminal_group == TERMINAL_GROUP_WALLET:
        return _wallet_public_key_raw(category)
    if category == TERMINAL_CATEGORY_UTILITY and settings.AZERICARD_PUBLIC_KEY_UTILITY:
        return settings.AZERICARD_PUBLIC_KEY_UTILITY
    if category == TERMINAL_CATEGORY_MAINTENANCE and settings.AZERICARD_PUBLIC_KEY_MAINTENANCE:
        return settings.AZERICARD_PUBLIC_KEY_MAINTENANCE
    if category == TERMINAL_CATEGORY_ADVANCE and settings.AZERICARD_PUBLIC_KEY_ADVANCE:
        return settings.AZERICARD_PUBLIC_KEY_ADVANCE
    return settings.AZERICARD_PUBLIC_KEY


def _private_key(category: Optional[str] = None, terminal_group: Optional[str] = None) -> RSAPrivateKey:
    key_pem = _as_pem(_private_key_raw(category, terminal_group), "private").encode("utf-8")
    return load_pem_private_key(key_pem, password=None)


def _public_key(category: Optional[str] = None, terminal_group: Optional[str] = None) -> RSAPublicKey:
    key_pem = _as_pem(_public_key_raw(category, terminal_group), "public").encode("utf-8")
    return load_pem_public_key(key_pem)


# ---------------------------------------------------------------------------
# Signature generation / verification
# ---------------------------------------------------------------------------

def build_signature_content(data: dict, fields: Iterable[str]) -> str:
    return ";".join(str(data.get(name, "") or "") for name in fields)


def generate_p_sign(
    data: dict,
    fields: Iterable[str],
    category: Optional[str] = None,
    terminal_group: Optional[str] = None,
) -> str:
    content = build_signature_content(data, fields).encode("utf-8")
    signature = _private_key(category, terminal_group).sign(content, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def _terminal_group_from_data(data: dict) -> str:
    callback_tid = _normalize_terminal_id(str(data.get("TERMINAL", "") or ""))
    if not callback_tid:
        return TERMINAL_GROUP_STANDARD
    if callback_tid in _all_wallet_terminal_ids_normalized():
        return TERMINAL_GROUP_WALLET
    return TERMINAL_GROUP_STANDARD


def verify_callback_signature(
    data: dict,
    signature_field: str = "P_SIGN",
    category: Optional[str] = None,
    terminal_group: Optional[str] = None,
) -> bool:
    signature_b64 = str(data.get(signature_field, "") or "").strip()
    if not signature_b64:
        return False
    try:
        signature = base64.b64decode(signature_b64)
        content = build_signature_content(data, CALLBACK_SIGN_FIELDS).encode("utf-8")
        resolved_group = terminal_group or _terminal_group_from_data(data)
        _public_key(category, resolved_group).verify(signature, content, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Order / amount / timestamp helpers
# ---------------------------------------------------------------------------

def build_order_id(terminal_id: str | None = None) -> str:
    tid = "".join(ch for ch in str(terminal_id or settings.AZERICARD_TERMINAL_ID or "") if ch.isdigit())
    date_part = datetime.now(timezone.utc).strftime("%y%m%d")
    rnd = secrets.token_hex(5)
    rnd_digits = "".join(str(int(ch, 16) % 10) for ch in rnd)
    order = f"{tid[:6]}{date_part}{rnd_digits}"
    order = "".join(ch for ch in order if ch.isdigit())
    if len(order) < 6:
        order = (order + "0" * 6)[:6]
    if len(order) > 32:
        order = order[:32]
    return order


def amount_to_gateway(amount: Decimal | float | int | str) -> str:
    val = Decimal(str(amount or "0")).quantize(Decimal("0.01"))
    return format(val, "f")


def build_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def build_nonce() -> str:
    return hashlib.md5(secrets.token_bytes(24)).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Invoice line → terminal category classifier
# ---------------------------------------------------------------------------

def _classify_meter_type(meter_type_str: str | None) -> str:
    """Map a MeterType enum value to a terminal category."""
    mt = (meter_type_str or "").upper().strip()
    if mt in UTILITY_METER_TYPES:
        return TERMINAL_CATEGORY_UTILITY
    if mt in MAINTENANCE_METER_TYPES:
        return TERMINAL_CATEGORY_MAINTENANCE
    return TERMINAL_CATEGORY_UTILITY


def _classify_description(description: str) -> str:
    """Fallback heuristic when no meter reading is linked."""
    low = (description or "").lower()
    if any(kw in low for kw in ("электр", "electric", "газ", "gas", "вода", "water", "канализац", "sewerage", "стабиль")):
        return TERMINAL_CATEGORY_UTILITY
    if any(kw in low for kw in ("серв", "service", "аренд", "rent", "строител", "construction")):
        return TERMINAL_CATEGORY_MAINTENANCE
    return TERMINAL_CATEGORY_UTILITY


def classify_invoice_amounts(db, invoice_id: int) -> dict[str, Decimal]:
    """Return {"utility": Decimal, "maintenance": Decimal} for an invoice's lines."""
    from ..models import InvoiceLine, MeterReading, ResidentMeter

    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice_id).all()
    totals: dict[str, Decimal] = {
        TERMINAL_CATEGORY_UTILITY: Decimal("0"),
        TERMINAL_CATEGORY_MAINTENANCE: Decimal("0"),
    }

    for line in lines:
        category = TERMINAL_CATEGORY_UTILITY
        if line.meter_reading_id:
            reading = db.get(MeterReading, line.meter_reading_id)
            if reading and reading.resident_meter_id:
                meter = db.get(ResidentMeter, reading.resident_meter_id)
                if meter and meter.meter_type:
                    category = _classify_meter_type(str(meter.meter_type.value if hasattr(meter.meter_type, "value") else meter.meter_type))
        else:
            category = _classify_description(line.description)

        totals[category] = totals[category] + Decimal(str(line.amount_total or 0))

    return {k: v for k, v in totals.items() if v > 0}
