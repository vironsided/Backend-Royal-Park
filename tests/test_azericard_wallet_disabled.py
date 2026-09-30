from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.routers import api_azericard


def _request(**overrides):
    values = {
        "resident_id": 1,
        "amount": Decimal("1.00"),
    }
    values.update(overrides)
    return api_azericard.InitiateRequest(**values)


def test_wallet_config_reports_wallets_unsupported_when_disabled(monkeypatch):
    monkeypatch.setattr(api_azericard.settings, "AZERICARD_WALLET_ENABLED", False)
    monkeypatch.setattr(
        api_azericard.settings,
        "AZERICARD_GPAY_GATEWAY_MERCHANT_ID",
        "configured-google-merchant",
    )
    monkeypatch.setattr(
        api_azericard.settings,
        "AZERICARD_APPLE_MERCHANT_IDENTIFIER",
        "merchant.az.royalpark",
    )

    result = api_azericard.wallet_config()

    assert result["wallet_enabled"] is False
    assert result["google_pay"]["supported"] is False
    assert result["apple_pay"]["supported"] is False


@pytest.mark.parametrize(
    "wallet_fields",
    [
        {"terminal_group": "wallet"},
        {"wallet_provider": "google_pay"},
        {"wallet_provider": "apple_pay"},
        {"wallet_token": "token"},
        {"wallet_eci": "eci"},
        {"wallet_tavv": "tavv"},
    ],
)
def test_disabled_wallet_requests_are_rejected(monkeypatch, wallet_fields):
    monkeypatch.setattr(api_azericard.settings, "AZERICARD_WALLET_ENABLED", False)

    with pytest.raises(HTTPException) as exc_info:
        api_azericard._ensure_wallet_request_allowed(_request(**wallet_fields))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Wallet payments are disabled"


def test_standard_card_request_remains_allowed_when_wallets_disabled(monkeypatch):
    monkeypatch.setattr(api_azericard.settings, "AZERICARD_WALLET_ENABLED", False)

    api_azericard._ensure_wallet_request_allowed(_request())


def test_wallet_config_reports_configured_wallets_when_enabled(monkeypatch):
    monkeypatch.setattr(api_azericard.settings, "AZERICARD_WALLET_ENABLED", True)
    monkeypatch.setattr(
        api_azericard.settings,
        "AZERICARD_GPAY_GATEWAY_MERCHANT_ID",
        "configured-google-merchant",
    )
    monkeypatch.setattr(
        api_azericard.settings,
        "AZERICARD_APPLE_MERCHANT_IDENTIFIER",
        "merchant.az.royalpark",
    )

    result = api_azericard.wallet_config()

    assert result["wallet_enabled"] is True
    assert result["google_pay"]["supported"] is True
    assert result["apple_pay"]["supported"] is True


def test_wallet_request_is_allowed_when_enabled(monkeypatch):
    monkeypatch.setattr(api_azericard.settings, "AZERICARD_WALLET_ENABLED", True)

    api_azericard._ensure_wallet_request_allowed(
        _request(terminal_group="wallet", wallet_provider="google_pay")
    )
