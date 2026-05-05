# FastAPI Admin (Dark)

## Подготовка БД
Создай БД `fastapi_admin` в PostgreSQL:
```sql
CREATE DATABASE fastapi_admin;
```

## Переменные окружения (.env)
Все конфигурации и секреты вынесены в `Backend/.env`.

1. Скопируй шаблон:
```bash
cp .env.example .env
```
2. Заполни `.env` своими значениями (БД, ROOT, AzeriCard, Firebase).

## Запуск (Windows / Linux)
```bash
python -m venv .venv
# Windows:
. .venv/Scripts/activate
# Linux/macOS:
# source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Открой http://127.0.0.1:8000/login

## Запуск через Docker
Из корня проекта `RoyalPark`:

```bash
docker compose up --build
```

После старта:
- Backend: http://127.0.0.1:8000/login
- Healthcheck: http://127.0.0.1:8000/healthz

Важно для Docker:
- Бэкенд подключается к БД по `PG_HOST=db` (имя сервиса в `docker-compose.yml`).
- Для локального запуска без Docker можно оставить `PG_HOST=127.0.0.1`.

Вход ROOT берётся из `.env`:
- `ROOT_USERNAME`
- `ROOT_PASSWORD`

## Политика ролей
- ROOT: полный доступ, может создавать/редактировать/удалять всех, видеть/сбрасывать временные пароли.
- ADMIN: может управлять только OPERATOR и RESIDENT. Не может редактировать/удалять ADMIN/ROOT.
- OPERATOR, RESIDENT: без прав управления пользователями.

## Особенности
- Для новых пользователей генерируется временный пароль (видно на странице пользователeй).
- При первом входе (или после сброса) — принудительная смена пароля.
- Для root смена не требуется по умолчанию.

## AzeriCard Apple Pay / Google Pay
- Для отдельного wallet-терминала заполните в `.env`:
  - `AZERICARD_TERMINAL_WALLET`
  - `AZERICARD_PRIVATE_KEY_WALLET`
  - `AZERICARD_PUBLIC_KEY_WALLET`
- `AZERICARD_CALLBACK_URL` должен быть публичным full URL (не `localhost`) и используется как `BACKREF`.
- Настройка типа авторизации:
  - `AZERICARD_AUTH_TRTYPE=1` — обычная авторизация (рекомендуемый default).
  - `AZERICARD_AUTH_TRTYPE=0` — preauth (требует последующего `TRTYPE=21` confirm).
- Для status-запросов (`TRTYPE=90`) можно передавать/зафиксировать `TRAN_TRTYPE`:
  - env: `AZERICARD_STATUS_TRAN_TRTYPE` (обычно `1`)
  - API: `GET /api/azericard/status/{order_id}?tran_trtype=1`
- `POST /api/azericard/initiate` принимает дополнительные поля для wallet-потока:
  - `wallet_provider`: `google_pay` или `apple_pay`
  - `wallet_token`: токен кошелька (для Google Pay передается как `GPAYTOKEN`)
  - `wallet_eci`, `wallet_tavv`: 3DS-поля для Apple Pay (если требуются вашим сценарием)
- Конфиг Google Pay для фронта читается из `GET /api/azericard/wallet-config`.
- Post-auth операции gateway:
  - `POST /api/azericard/complete` (`TRTYPE=21`)
  - `POST /api/azericard/reversal?trtype=22|24`
  - `POST /api/azericard/operation` с телом `{ trtype: "21"|"22"|"24", order_id, amount, currency, rrn, int_ref }`
- Для Google Pay обязательно заполните:
  - `AZERICARD_GPAY_GATEWAY` (обычно `azericardgpay`)
  - `AZERICARD_GPAY_GATEWAY_MERCHANT_ID` (выдаётся Azericard; без него Google Pay выдаёт OR_BIBED_06)
- Дополнительно:
  - `AZERICARD_GPAY_ENVIRONMENT` (`TEST` или `PRODUCTION`)
  - `AZERICARD_GPAY_MERCHANT_ID` (обычно обязателен в PRODUCTION)
  - `AZERICARD_GPAY_MERCHANT_NAME`
- Никогда не передавайте партнёрам private key. Для подключения выдаётся только public key (RSA 2048) и callback URL.
