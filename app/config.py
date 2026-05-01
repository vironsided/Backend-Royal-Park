import os
from pathlib import Path
from pydantic import BaseModel
from dotenv import load_dotenv


BACKEND_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_ROOT / ".env")


class Settings(BaseModel):
    # Подключение к БД
    PG_HOST: str = os.getenv("PG_HOST", "")
    PG_PORT: int = int(os.getenv("PG_PORT", "5432"))
    PG_USER: str = os.getenv("PG_USER", "")
    PG_PASSWORD: str = os.getenv("PG_PASSWORD", "")
    PG_DB: str = os.getenv("PG_DB", "")

    # Секреты/куки
    SESSION_SECRET_KEY: str = os.getenv("SESSION_SECRET_KEY", "")
    COOKIE_NAME: str = os.getenv("COOKIE_NAME", "session_id")

    # ROOT-учётка
    ROOT_USERNAME: str = os.getenv("ROOT_USERNAME", "")
    ROOT_PASSWORD: str = os.getenv("ROOT_PASSWORD", "")
    FRONTEND_BASE_URL: str = os.getenv("FRONTEND_BASE_URL", "")

    # AzeriCard — general
    AZERICARD_GATEWAY_URL: str = os.getenv("AZERICARD_GATEWAY_URL", "")
    AZERICARD_API_URL: str = os.getenv("AZERICARD_API_URL", "")
    AZERICARD_MERCH_NAME: str = os.getenv("AZERICARD_MERCH_NAME", "")
    AZERICARD_MERCH_URL: str = os.getenv("AZERICARD_MERCH_URL", "")
    AZERICARD_CALLBACK_URL: str = os.getenv("AZERICARD_CALLBACK_URL", "")
    AZERICARD_SUCCESS_URL: str = os.getenv("AZERICARD_SUCCESS_URL", "")
    AZERICARD_FAIL_URL: str = os.getenv("AZERICARD_FAIL_URL", "")
    AZERICARD_CURRENCY: str = os.getenv("AZERICARD_CURRENCY", "AZN")
    AZERICARD_LANG: str = os.getenv("AZERICARD_LANG", "en")

    # AzeriCard — legacy single-terminal (fallback)
    AZERICARD_TERMINAL_ID: str = os.getenv("AZERICARD_TERMINAL_ID", "")
    AZERICARD_PRIVATE_KEY: str = os.getenv("AZERICARD_PRIVATE_KEY", "")
    AZERICARD_PUBLIC_KEY: str = os.getenv("AZERICARD_PUBLIC_KEY", "")

    # AzeriCard — per-category terminals (utility / maintenance / advance)
    AZERICARD_TERMINAL_UTILITY: str = os.getenv("AZERICARD_TERMINAL_UTILITY", "")
    AZERICARD_PRIVATE_KEY_UTILITY: str = os.getenv("AZERICARD_PRIVATE_KEY_UTILITY", "")
    AZERICARD_PUBLIC_KEY_UTILITY: str = os.getenv("AZERICARD_PUBLIC_KEY_UTILITY", "")

    AZERICARD_TERMINAL_MAINTENANCE: str = os.getenv("AZERICARD_TERMINAL_MAINTENANCE", "")
    AZERICARD_PRIVATE_KEY_MAINTENANCE: str = os.getenv("AZERICARD_PRIVATE_KEY_MAINTENANCE", "")
    AZERICARD_PUBLIC_KEY_MAINTENANCE: str = os.getenv("AZERICARD_PUBLIC_KEY_MAINTENANCE", "")

    AZERICARD_TERMINAL_ADVANCE: str = os.getenv("AZERICARD_TERMINAL_ADVANCE", "")
    AZERICARD_PRIVATE_KEY_ADVANCE: str = os.getenv("AZERICARD_PRIVATE_KEY_ADVANCE", "")
    AZERICARD_PUBLIC_KEY_ADVANCE: str = os.getenv("AZERICARD_PUBLIC_KEY_ADVANCE", "")

    # AzeriCard — dedicated wallet terminal (Apple Pay / Google Pay)
    AZERICARD_TERMINAL_WALLET: str = os.getenv("AZERICARD_TERMINAL_WALLET", "")
    AZERICARD_PRIVATE_KEY_WALLET: str = os.getenv("AZERICARD_PRIVATE_KEY_WALLET", "")
    AZERICARD_PUBLIC_KEY_WALLET: str = os.getenv("AZERICARD_PUBLIC_KEY_WALLET", "")
    AZERICARD_GPAY_ENVIRONMENT: str = os.getenv("AZERICARD_GPAY_ENVIRONMENT", "TEST")
    AZERICARD_GPAY_GATEWAY: str = os.getenv("AZERICARD_GPAY_GATEWAY", "azericardgpay")
    AZERICARD_GPAY_GATEWAY_MERCHANT_ID: str = os.getenv("AZERICARD_GPAY_GATEWAY_MERCHANT_ID", "")
    AZERICARD_GPAY_MERCHANT_ID: str = os.getenv("AZERICARD_GPAY_MERCHANT_ID", "")
    AZERICARD_GPAY_MERCHANT_NAME: str = os.getenv("AZERICARD_GPAY_MERCHANT_NAME", "")

    # Firebase Cloud Messaging (backend)
    FIREBASE_CREDENTIALS_PATH: str = os.getenv("FIREBASE_CREDENTIALS_PATH", "")
    FIREBASE_CREDENTIALS_JSON: str = os.getenv("FIREBASE_CREDENTIALS_JSON", "")
    FIREBASE_PROJECT_ID: str = os.getenv("FIREBASE_PROJECT_ID", "")


settings = Settings()
