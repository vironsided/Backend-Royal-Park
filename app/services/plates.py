import re

_KEEP = re.compile(r"[^A-Za-z0-9]")


def normalize_plate(raw: str | None) -> str:
    """Canonical plate form: uppercase, only A-Z and 0-9 (spaces, dashes, dots dropped).
    Used by BOTH manual guard entry and the Phase-2 camera OCR so they always match."""
    if not raw:
        return ""
    return _KEEP.sub("", str(raw)).upper()
