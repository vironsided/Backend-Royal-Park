"""Shared invoice-line selection and payment allocation rules."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal


def _money2(value: Decimal) -> Decimal:
    return (value or Decimal("0")).quantize(Decimal("0.01"))


def parse_selected_line_ids(reference: str | None) -> list[int]:
    if not reference:
        return []
    marker = "LINESEL:"
    idx = reference.find(marker)
    if idx < 0:
        return []
    raw = reference[idx + len(marker):].split("|")[0]
    result: list[int] = []
    for token in raw.split(","):
        try:
            value = int(token.strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            result.append(value)
    return result


def is_water_line_description(description: str | None) -> bool:
    low = (description or "").lower()
    return ("вода" in low) or ("water" in low) or ("meter_cold_water" in low)


def is_sewerage_line_description(description: str | None) -> bool:
    low = (description or "").lower()
    return (
        ("канализац" in low)
        or ("kanaliz" in low)
        or ("sewerage" in low)
        or ("meter_sewerage" in low)
    )


def normalize_selected_line_ids_for_water_sewer(
    selected_ids: list[int],
    line_desc_by_id: dict[int, str],
) -> list[int]:
    """Treat water + sewerage as one bundle, ordered sewerage first."""
    unique_ids: list[int] = []
    for raw_value in selected_ids or []:
        try:
            line_id = int(raw_value)
        except (TypeError, ValueError):
            continue
        if line_id > 0 and line_id not in unique_ids:
            unique_ids.append(line_id)
    if not unique_ids:
        return []

    selected_has_bundle = any(
        is_water_line_description(line_desc_by_id.get(line_id))
        or is_sewerage_line_description(line_desc_by_id.get(line_id))
        for line_id in unique_ids
    )
    if not selected_has_bundle:
        return unique_ids

    sewer_ids = [
        line_id for line_id, description in line_desc_by_id.items()
        if is_sewerage_line_description(description)
    ]
    water_ids = [
        line_id for line_id, description in line_desc_by_id.items()
        if is_water_line_description(description)
    ]
    if not sewer_ids or not water_ids:
        return unique_ids

    for line_id in sewer_ids + water_ids:
        if line_id not in unique_ids:
            unique_ids.append(line_id)

    original_position = {line_id: index for index, line_id in enumerate(unique_ids)}

    def sort_key(line_id: int) -> tuple[int, int]:
        if line_id in sewer_ids:
            return (0, original_position[line_id])
        if line_id in water_ids:
            return (1, original_position[line_id])
        return (2, original_position[line_id])

    return sorted(unique_ids, key=sort_key)


def build_invoice_line_payment_map(lines: list, applications: list) -> dict[int, dict]:
    """Allocate application amounts to invoice lines for UI/status reporting."""
    sorted_lines = sorted(lines, key=lambda line: line.id or 0)
    descriptions = {
        int(line.id): (line.description or "")
        for line in sorted_lines
        if line.id is not None
    }
    totals = {
        int(line.id): Decimal(str(line.amount_total or 0))
        for line in sorted_lines
        if line.id is not None
    }
    paid = {line_id: Decimal("0") for line_id in totals}

    def allocate(amount: Decimal, line_ids: list[int]) -> None:
        remaining = Decimal(str(amount or 0))
        for line_id in line_ids:
            if remaining <= 0:
                break
            if line_id not in totals:
                continue
            capacity = max(totals[line_id] - paid[line_id], Decimal("0"))
            if capacity <= 0:
                continue
            applied = min(capacity, remaining)
            paid[line_id] += applied
            remaining -= applied

    ordered_applications = sorted(
        applications or [],
        key=lambda application: (
            application.created_at or datetime.min,
            application.id or 0,
        ),
    )
    default_order = list(totals.keys())
    for application in ordered_applications:
        amount = Decimal(str(getattr(application, "amount_applied", 0) or 0))
        if amount <= 0:
            continue
        selected_ids = parse_selected_line_ids(getattr(application, "reference", None))
        if selected_ids:
            selected_ids = normalize_selected_line_ids_for_water_sewer(
                selected_ids,
                descriptions,
            )
            allocate(amount, selected_ids)
        else:
            allocate(amount, default_order)

    result: dict[int, dict] = {}
    for line_id, total in totals.items():
        paid_amount = _money2(paid.get(line_id, Decimal("0")))
        remaining_amount = _money2(max(total - paid_amount, Decimal("0")))
        if remaining_amount <= Decimal("0.0001"):
            status = "Оплачена"
        elif paid_amount > Decimal("0.0001"):
            status = "Частично"
        else:
            status = "Не оплачена"
        result[line_id] = {
            "paid": paid_amount,
            "remaining": remaining_amount,
            "status": status,
        }
    return result
