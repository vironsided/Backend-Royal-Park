import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from .config import settings
from .database import Base, engine, SessionLocal
from .models import User, RoleEnum
from .security import hash_password
from .routers import auth_routes, dashboard, api_users, api_blocks, api_tariffs, api_residents, api_readings, api_tenants, api_invoices, api_payments, api_notifications, api_dashboard, api_logs, api_qr, api_payment, api_resident_dashboard, api_news, api_azericard, api_sales, push_routes


def init_db():
    Base.metadata.create_all(bind=engine)
    run_bootstrap_schema()

    db: Session = SessionLocal()
    try:
        root = db.query(User).filter(User.username == settings.ROOT_USERNAME).first()
        if not root:
            root = User(
                username=settings.ROOT_USERNAME,
                password_hash=hash_password(settings.ROOT_PASSWORD),
                role=RoleEnum.ROOT,
                require_password_change=False,
                temp_password_plain=None,
            )
            db.add(root)
            db.commit()

        # Seed первичного продажника (Satish) — продажа вилл/домов в комплексе.
        # Создаётся один раз; дальше новых "продажников" можно заводить из админки.
        satish = db.query(User).filter(User.username == "satish").first()
        if not satish:
            satish_temp_password = "Satish123!"
            satish = User(
                username="satish",
                password_hash=hash_password(satish_temp_password),
                role=RoleEnum.SALES,
                full_name="Satish",
                require_password_change=True,
                temp_password_plain=satish_temp_password,
                created_by_id=root.id if root else None,
            )
            db.add(satish)
            db.commit()
    finally:
        db.close()

def run_bootstrap_schema():
    """
    Мягкие DDL: новые поля и таблицы — безопасно на каждом старте.
    """
    ddl_statements = [
        # (если раньше ещё не добавили эти поля, оставь — IF NOT EXISTS защитит)
                """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'payments'
              AND column_name = 'received_at'
              AND data_type = 'date'
          ) THEN
            ALTER TABLE payments
              ALTER COLUMN received_at TYPE TIMESTAMPTZ
              USING (received_at::timestamp AT TIME ZONE 'Asia/Baku');
          END IF;
        END $$;
        """,
        # Регистрация новой роли SALES в существующем enum-типе roleenum.
        # ALTER TYPE ... ADD VALUE IF NOT EXISTS поддерживается начиная с PostgreSQL 9.6.
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'roleenum') THEN
            ALTER TYPE roleenum ADD VALUE IF NOT EXISTS 'SALES';
          END IF;
        END $$;
        """,
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name varchar(200);",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone     varchar(50);",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email     varchar(120);",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS comment   varchar(500);",
        # НОВОЕ: путь к аватару
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_path varchar(255);",
        "ALTER TABLE payment_applications ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();",
        # Tariffs: фиксированная часть для ELECTRIC/GAS
        "ALTER TABLE tariffs ADD COLUMN IF NOT EXISTS stable_tariff NUMERIC(18,2) NOT NULL DEFAULT 0;",
        "ALTER TABLE tariffs ADD COLUMN IF NOT EXISTS use_multiplier BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE tariffs ADD COLUMN IF NOT EXISTS consumption_multiplier NUMERIC(12,4) NOT NULL DEFAULT 1;",
        # Meter readings: исторический snapshot stable_tariff (чтобы старые инвойсы не менялись при правке тарифа)
        "ALTER TABLE meter_readings ADD COLUMN IF NOT EXISTS stable_fee_net NUMERIC(18,2);",
        "ALTER TABLE meter_readings ADD COLUMN IF NOT EXISTS stable_fee_vat NUMERIC(18,2);",
        "ALTER TABLE meter_readings ADD COLUMN IF NOT EXISTS stable_fee_total NUMERIC(18,2);",
        # News table
        """
        CREATE TABLE IF NOT EXISTS news (
          id SERIAL PRIMARY KEY,
          title TEXT NOT NULL,
          content TEXT NOT NULL,
          icon VARCHAR(50) NOT NULL DEFAULT 'info',
          icon_color VARCHAR(50) NOT NULL DEFAULT '#667eea',
          target_blocks TEXT NULL,
          is_active BOOLEAN NOT NULL DEFAULT TRUE,
          priority INTEGER NOT NULL DEFAULT 0,
          published_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
          expires_at TIMESTAMP WITHOUT TIME ZONE NULL,
          created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
          updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
          created_by_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL
        );
        """,
        # Meter reading photos table
        """
        CREATE TABLE IF NOT EXISTS meter_reading_photos (
          id SERIAL PRIMARY KEY,
          meter_reading_id INTEGER NOT NULL UNIQUE REFERENCES meter_readings(id) ON DELETE CASCADE,
          file_path VARCHAR(255) NOT NULL,
          created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
          expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
          created_by_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL
        );
        """,
        # M2M таблица (если не создана)
        """
        CREATE TABLE IF NOT EXISTS user_residents (
          user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          resident_id INTEGER NOT NULL REFERENCES residents(id) ON DELETE CASCADE,
          PRIMARY KEY (user_id, resident_id)
        );
        """
        # ... (твои предыдущие DDL тут могут быть)
        """
           CREATE TABLE IF NOT EXISTS resident_services (
             id SERIAL PRIMARY KEY,
             resident_id INTEGER NOT NULL REFERENCES residents(id) ON DELETE CASCADE,
             service_type VARCHAR(16) NOT NULL,
             amount NUMERIC(18,2) NOT NULL DEFAULT 0,
             vat_percent INTEGER NOT NULL DEFAULT 0,
             is_active BOOLEAN NOT NULL DEFAULT TRUE,
             created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
             created_by_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL
           );
           """
        "ALTER TABLE invoice_lines ALTER COLUMN meter_reading_id DROP NOT NULL;",
        "ALTER TABLE tariff_steps ADD COLUMN IF NOT EXISTS from_date DATE;",
        "ALTER TABLE tariff_steps ADD COLUMN IF NOT EXISTS to_date DATE;",
        "ALTER TABLE tariff_steps ALTER COLUMN from_value DROP NOT NULL;",
        "ALTER TABLE tariff_steps ALTER COLUMN to_value DROP NOT NULL;",
        "ALTER TABLE tariff_steps ALTER COLUMN price TYPE NUMERIC(18,6);",
        # WATER: канализация как % от суммы воды (хранится в tariffs)
        "ALTER TABLE tariffs ADD COLUMN IF NOT EXISTS sewerage_percent NUMERIC(5,2) NOT NULL DEFAULT 0;",
        "ALTER TABLE payment_applications ADD COLUMN IF NOT EXISTS reference VARCHAR(100);",
        # Убираем уникальное ограничение uq_payment_invoice, чтобы разрешить несколько применений одного платежа к одному счету
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_payment_invoice') THEN ALTER TABLE payment_applications DROP CONSTRAINT uq_payment_invoice; END IF; END $$;",
        # Унифицируем номер счета по новому каноническому формату:
        # INV-(resident_id)/(tenant_id)/(YYYY-MM)
        """
        UPDATE invoices i
        SET number = CONCAT(
          'INV-',
          i.resident_id::text,
          '/',
          COALESCE(
            (
              SELECT MIN(ur.user_id)
              FROM user_residents ur
              JOIN users u ON u.id = ur.user_id
              WHERE ur.resident_id = i.resident_id
                AND u.role = 'RESIDENT'
            ),
            (
              SELECT MIN(ur2.user_id)
              FROM user_residents ur2
              WHERE ur2.resident_id = i.resident_id
            ),
            0
          )::text,
          '/',
          i.period_year::text,
          '-',
          LPAD(i.period_month::text, 2, '0')
        )
        WHERE i.resident_id IS NOT NULL
          AND i.period_year IS NOT NULL
          AND i.period_month IS NOT NULL;
        """,
        # Для существующих дублей invoice number оставляем аудиторный хвост, чтобы можно было
        # безопасно включить БД-ограничение уникальности номера.
        """
        WITH dup AS (
          SELECT id, number, ROW_NUMBER() OVER (PARTITION BY number ORDER BY id) AS rn
          FROM invoices
          WHERE number IS NOT NULL AND BTRIM(number) <> ''
        )
        UPDATE invoices i
        SET number = CONCAT(i.number, '-DUP-', i.id::text)
        FROM dup d
        WHERE i.id = d.id AND d.rn > 1;
        """,
        # Жесткая защита в БД: номер счета должен быть уникальным.
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_invoices_number') THEN ALTER TABLE invoices ADD CONSTRAINT uq_invoices_number UNIQUE (number); END IF; END $$;",
        # QR Tokens table
        """
        CREATE TABLE IF NOT EXISTS qr_tokens (
          id SERIAL PRIMARY KEY,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          token VARCHAR(128) NOT NULL UNIQUE,
          is_used BOOLEAN NOT NULL DEFAULT FALSE,
          created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
          used_at TIMESTAMP WITHOUT TIME ZONE NULL
        );
        """,
        # Payment Logs table
        """
        CREATE TABLE IF NOT EXISTS payment_logs (
          id SERIAL PRIMARY KEY,
          payment_id INTEGER NULL REFERENCES payments(id) ON DELETE SET NULL,
          resident_id INTEGER NULL REFERENCES residents(id) ON DELETE SET NULL,
          user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
          action VARCHAR(50) NOT NULL,
          amount NUMERIC(12,2) NOT NULL,
          details TEXT NULL,
          created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        );
        """,
        # Новые поля для уведомлений
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS notification_type VARCHAR(20);",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS related_id INTEGER;",
        # Обращения: стадия обработки и сообщение для жителя
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS appeal_workflow VARCHAR(40);",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS staff_message TEXT;",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS workflow_updated_at TIMESTAMP WITHOUT TIME ZONE;",
        "ALTER TABLE news ADD COLUMN IF NOT EXISTS target_blocks TEXT;",
        # Multi-terminal support for AzeriCard
        "ALTER TABLE online_transactions ADD COLUMN IF NOT EXISTS terminal_category VARCHAR(32);",
        # Saved cards for card-on-file / tokenization
        """CREATE TABLE IF NOT EXISTS saved_cards (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_id VARCHAR(256) NOT NULL,
            masked_pan VARCHAR(32) NOT NULL DEFAULT '****',
            card_brand VARCHAR(32),
            expiry_month INTEGER,
            expiry_year INTEGER,
            is_default BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_saved_cards_user_token UNIQUE (user_id, token_id)
        );""",
        """CREATE TABLE IF NOT EXISTS payment_application_lines (
            id SERIAL PRIMARY KEY,
            application_id INTEGER NOT NULL REFERENCES payment_applications(id) ON DELETE CASCADE,
            invoice_line_id INTEGER NOT NULL REFERENCES invoice_lines(id) ON DELETE CASCADE,
            amount NUMERIC(12, 2) NOT NULL
        );""",
        """
        CREATE TABLE IF NOT EXISTS push_device_tokens (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token TEXT NOT NULL,
            token_hash CHAR(64) NOT NULL,
            platform VARCHAR(16) NOT NULL,
            device_id VARCHAR(128) NULL,
            device_name VARCHAR(128) NULL,
            app_version VARCHAR(32) NULL,
            os_version VARCHAR(32) NULL,
            locale VARCHAR(16) NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            invalidated_at TIMESTAMPTZ NULL,
            invalidation_reason VARCHAR(64) NULL,
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_sent_at TIMESTAMPTZ NULL,
            last_error_at TIMESTAMPTZ NULL,
            last_error_code VARCHAR(64) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_push_device_tokens_token_hash ON push_device_tokens(token_hash);",
        "CREATE INDEX IF NOT EXISTS idx_push_device_tokens_user_active ON push_device_tokens(user_id, is_active);",
        "CREATE INDEX IF NOT EXISTS idx_push_device_tokens_invalidated_at ON push_device_tokens(invalidated_at);",
        # ==========================================================
        #  Модуль продаж (SALES): договора купли-продажи вилл/домов
        # ==========================================================
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'sales_contract_type') THEN
            CREATE TYPE sales_contract_type AS ENUM ('FULL', 'INSTALLMENT');
          END IF;
        END $$;
        """,
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'sales_contract_status') THEN
            CREATE TYPE sales_contract_status AS ENUM (
              'DRAFT', 'PENDING_APPROVAL', 'VIEWED', 'APPROVED', 'REJECTED', 'PRINTED'
            );
          END IF;
        END $$;
        """,
        """
        CREATE TABLE IF NOT EXISTS sales_contracts (
          id SERIAL PRIMARY KEY,
          contract_type sales_contract_type NOT NULL DEFAULT 'FULL',
          status sales_contract_status NOT NULL DEFAULT 'DRAFT',

          contract_number VARCHAR(100),
          contract_year INTEGER,
          contract_date DATE,
          city VARCHAR(100) DEFAULT 'Bakı şəhəri',

          buyer_full_name VARCHAR(200),
          buyer_id_series VARCHAR(20),
          buyer_id_number VARCHAR(50),
          buyer_fin VARCHAR(30),
          buyer_phone VARCHAR(50),
          buyer_email VARCHAR(120),
          buyer_address TEXT,

          house_number VARCHAR(50),
          area_m2 NUMERIC(10,2),
          price_per_m2_usd NUMERIC(12,2),
          total_price_usd NUMERIC(14,2),

          initial_payment_usd NUMERIC(14,2),
          remaining_usd NUMERIC(14,2),
          months_count INTEGER,
          monthly_payment_usd NUMERIC(14,2),

          created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          approval_requested_at TIMESTAMP WITHOUT TIME ZONE,
          viewed_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          viewed_at TIMESTAMP WITHOUT TIME ZONE,
          reviewed_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          reviewed_at TIMESTAMP WITHOUT TIME ZONE,
          review_comment TEXT,
          printed_at TIMESTAMP WITHOUT TIME ZONE,
          printed_count INTEGER NOT NULL DEFAULT 0,

          created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
          updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS sales_contract_installments (
          id SERIAL PRIMARY KEY,
          contract_id INTEGER NOT NULL REFERENCES sales_contracts(id) ON DELETE CASCADE,
          month_no INTEGER NOT NULL,
          payment_date DATE,
          amount_usd NUMERIC(14,2),
          created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_sales_contracts_status ON sales_contracts(status);",
        "CREATE INDEX IF NOT EXISTS idx_sales_contracts_created_by ON sales_contracts(created_by_id);",
        # Backfill: for existing PaymentApplications without line distributions,
        # compute proportional splits and insert them.
        """
        INSERT INTO payment_application_lines (application_id, invoice_line_id, amount)
        SELECT
            pa.id,
            il.id,
            ROUND(pa.amount_applied * il.amount_total / NULLIF(inv.amount_total, 0), 2)
        FROM payment_applications pa
        JOIN invoices inv ON inv.id = pa.invoice_id
        JOIN invoice_lines il ON il.invoice_id = inv.id
        WHERE NOT EXISTS (
            SELECT 1 FROM payment_application_lines pal WHERE pal.application_id = pa.id
        )
        AND inv.amount_total > 0;
        """,
    ]
    from .database import engine
    with engine.begin() as conn:
        for sql in ddl_statements:
            conn.exec_driver_sql(sql)

    # Гарантируем папку для аватаров
    os.makedirs("uploads/avatars", exist_ok=True)
    os.makedirs("uploads/meter_readings", exist_ok=True)



def create_app() -> FastAPI:
    app = FastAPI(title="FastAPI Admin (Dark)")
    app.add_middleware(SessionMiddleware, secret_key=settings.SESSION_SECRET_KEY, session_cookie=settings.COOKIE_NAME)

    # Разрешаем запросы с фронта (для разработки разрешаем все localhost origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ],
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

    app.include_router(auth_routes.router)
    app.include_router(dashboard.router)
    app.include_router(api_users.router)
    app.include_router(api_blocks.router)
    app.include_router(api_tariffs.router)
    app.include_router(api_residents.router)
    app.include_router(api_readings.router)
    app.include_router(api_tenants.router)
    app.include_router(api_invoices.router)
    app.include_router(api_payments.router)
    app.include_router(api_notifications.router)
    app.include_router(api_dashboard.router)
    app.include_router(api_logs.router)
    app.include_router(api_qr.router)
    app.include_router(api_payment.router)
    app.include_router(api_resident_dashboard.router)
    app.include_router(api_news.router)
    app.include_router(api_azericard.router)
    app.include_router(api_sales.router)
    app.include_router(push_routes.router)
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # /favicon.ico обслуживает Express (public/favicon.ico — брендовое дерево Royal Park).
    # Бэкенд-редирект на Bootstrap CDN удалён: он перебивал наш логотип.

    @app.on_event("startup")
    def _start_auto_advance_scheduler():
        from .services.auto_advance_scheduler import start_auto_advance_scheduler
        start_auto_advance_scheduler()

    @app.on_event("shutdown")
    def _stop_auto_advance_scheduler():
        from .services.auto_advance_scheduler import stop_auto_advance_scheduler
        stop_auto_advance_scheduler()

    return app


init_db()
app = create_app()
