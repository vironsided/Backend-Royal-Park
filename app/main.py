import os
import logging
import pathlib
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

logger = logging.getLogger("royalpark")
from sqlalchemy.orm import Session
from .config import settings
from .database import Base, engine, SessionLocal
from .models import User, RoleEnum
from .security import hash_password, get_user_id_from_session
from .deps import require_any_role
from .routers import auth_routes, dashboard, api_users, api_blocks, api_tariffs, api_residents, api_readings, api_tenants, api_invoices, api_payments, api_notifications, api_dashboard, api_logs, api_qr, api_payment, api_resident_dashboard, api_news, api_azericard, api_sales, push_routes, api_access


def _run_alembic():
    """Schema versioning (Alembic). The legacy bootstrap (create_all + soft DDL)
    still runs first and brings ANY existing DB to the current schema; alembic
    is the source of truth for all FUTURE changes:
    - a DB without alembic_version but with tables -> stamp head (schema is
      already current thanks to the bootstrap),
    - then upgrade head applies any new migration files.
    New schema changes go into alembic/versions/, NOT into run_bootstrap_schema.
    """
    from pathlib import Path
    from sqlalchemy import inspect as sa_inspect
    from alembic.config import Config
    from alembic import command

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    insp = sa_inspect(engine)
    if not insp.has_table("alembic_version") and insp.has_table("users"):
        command.stamp(cfg, "head")
    command.upgrade(cfg, "head")


def init_db():
    Base.metadata.create_all(bind=engine)
    run_bootstrap_schema()
    try:
        _run_alembic()
    except Exception as _e:
        # never block boot on versioning bookkeeping; the bootstrap above
        # already guarantees a working schema
        logger.error(f"[alembic] upgrade failed: {_e}")

    # ALTER TYPE ... ADD VALUE must run/commit OUTSIDE a transaction block before the
    # new value can be inserted. Ensure the GUARD role value exists on the existing
    # roleenum (no-op if already present). Postgres only; harmless if it fails.
    try:
        with engine.connect() as _conn:
            _conn.execution_options(isolation_level="AUTOCOMMIT").exec_driver_sql(
                "ALTER TYPE roleenum ADD VALUE IF NOT EXISTS 'GUARD'"
            )
        # Drop pooled connections so the seed below opens fresh ones that see the new
        # enum value (avoids a first-boot race on a DB where roleenum predates GUARD).
        engine.dispose()
    except Exception as _e:
        logger.warning(f"[bootstrap] roleenum GUARD ensure skipped: {_e}")

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
            # audit 7.11: no hardcoded password in source. Use SATISH_PASSWORD env or
            # a random temp; require_password_change=True forces a reset on first login.
            import secrets
            satish_temp_password = os.getenv("SATISH_PASSWORD") or secrets.token_urlsafe(9)
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

        # Seed охранника КПП (vehicle-access). Локальный известный пароль для теста.
        guard = db.query(User).filter(User.username == "guard").first()
        if not guard:
            guard_pw = os.getenv("GUARD_PASSWORD", "guard123")
            guard = User(
                username="guard",
                password_hash=hash_password(guard_pw),
                role=RoleEnum.GUARD,
                full_name="Aydın Quliyev",
                require_password_change=False,
                temp_password_plain=guard_pw,
                created_by_id=root.id if root else None,
            )
            db.add(guard)
            db.commit()
            logger.info(f"[seed] guard user created (login: guard / {guard_pw})")
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
        # session revocation: tokens carry the version they were issued with
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0;",
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
        """,
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
           """,
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
        # Payment history indexes for resident portal filters
        "CREATE INDEX IF NOT EXISTS idx_payments_resident_received_at ON payments(resident_id, received_at DESC);",
        # audit 7.13: hot-path SUM(amount_applied) joins/filters on these columns.
        "CREATE INDEX IF NOT EXISTS idx_payment_applications_payment_id ON payment_applications(payment_id);",
        "CREATE INDEX IF NOT EXISTS idx_payment_applications_invoice_id ON payment_applications(invoice_id);",
        "CREATE INDEX IF NOT EXISTS idx_online_tx_resident_created_at ON online_transactions(resident_id, created_at DESC);",
        "CREATE INDEX IF NOT EXISTS idx_payment_apps_created_at ON payment_applications(created_at DESC);",
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
    os.makedirs("uploads/gate", exist_ok=True)



def _is_prod_like() -> bool:
    # Same heuristic as security._use_cross_site_cookie: an https non-localhost
    # FRONTEND_BASE_URL means this instance serves a real deployment.
    frontend = (settings.FRONTEND_BASE_URL or "").strip().lower()
    if not frontend.startswith("https://"):
        return False
    return "localhost" not in frontend and "127.0.0.1" not in frontend


def _assert_camera_key_safe():
    # The ANPR camera key authorizes barrier opening (/api/access/camera/detect).
    # An unset or default key in production would let anyone open the gate.
    if _is_prod_like() and settings.ACCESS_CAMERA_KEY in ("", "rp-camera-dev-key"):
        raise RuntimeError(
            "ACCESS_CAMERA_KEY must be set to a strong unique secret in production "
            "(refusing to start with the dev default)"
        )


def create_app() -> FastAPI:
    _assert_camera_key_safe()
    app = FastAPI(title="FastAPI Admin (Dark)")
    # SessionMiddleware removed: nothing reads request.session — auth uses the
    # signed cookie set manually in security.set_session.

    allow_origins = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]
    if settings.FRONTEND_BASE_URL:
        allow_origins.append(settings.FRONTEND_BASE_URL.rstrip("/"))

    # Разрешаем запросы с фронта: localhost + явный FRONTEND_BASE_URL + Railway домены.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        # audit F-07: previously trusted ANY *.up.railway.app (shared platform) with
        # credentials. Now only localhost (dev) + the explicit FRONTEND_BASE_URL in
        # allow_origins above. Set FRONTEND_BASE_URL in prod to your frontend domain.
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # --- Security gate: legacy "/public" backdoor routes (audit F-01..F-04) ---
    # The codebase shipped dozens of `*/public` endpoints with NO authentication
    # (anonymous CRUD over residents/payments/tariffs/tenants/readings, password
    # reset, QR token generation). The frontend never calls those CRUD clones —
    # only the admin notifications feed and the public news feed legitimately use
    # a "public" path. This centralized middleware blocks any request whose path
    # contains a "public" segment unless (a) it is on the anonymous allowlist, or
    # (b) it carries a valid STAFF session. Closes the whole class in one place.
    ANON_PUBLIC_ALLOW = {"/api/news/public"}
    STAFF_ROLES = {RoleEnum.ROOT, RoleEnum.ADMIN, RoleEnum.OPERATOR, RoleEnum.SALES}

    @app.middleware("http")
    async def block_legacy_public_routes(request: Request, call_next):
        if request.method != "OPTIONS":
            path = request.url.path.rstrip("/") or "/"
            if "public" in path.split("/") and path not in ANON_PUBLIC_ALLOW:
                allowed = False
                uid = get_user_id_from_session(request)
                if uid is not None:
                    db = SessionLocal()
                    try:
                        u = db.get(User, uid)
                        allowed = bool(u and u.is_active and u.role in STAFF_ROLES)
                    finally:
                        db.close()
                if not allowed:
                    return JSONResponse(status_code=401, content={"detail": "Authentication required"})
        return await call_next(request)

    # --- Security gate: a temp-password account must SET a real password first ---
    # On first login (require_password_change=True) a session is issued, but the
    # account must not reach any panel/data until it sets a permanent password and
    # fills its profile. This middleware blocks every /api/* request from such a
    # session EXCEPT the endpoints needed to set the password (login/check/logout/
    # force-change-password and the QR setup endpoints). Applies to ALL roles.
    PWD_PENDING_ALLOW = (
        "/api/auth/login",
        "/api/auth/check",
        "/api/auth/logout",
        "/api/auth/force-change-password",
        "/api/qr/verify",
        "/api/qr/change-password",
    )

    @app.middleware("http")
    async def block_until_password_set(request: Request, call_next):
        if request.method != "OPTIONS":
            path = request.url.path
            if path.startswith("/api/") and not any(path.startswith(p) for p in PWD_PENDING_ALLOW):
                uid = get_user_id_from_session(request)
                if uid is not None:
                    db = SessionLocal()
                    try:
                        u = db.get(User, uid)
                        if u and u.is_active and getattr(u, "require_password_change", False):
                            return JSONResponse(status_code=403, content={"detail": "password_change_required"})
                    finally:
                        db.close()
        return await call_next(request)

    # audit F-15: never leak internal error text / tracebacks to clients. Any 5xx
    # (incl. explicit HTTPException(500, detail=str(e)) scattered in routers) is
    # logged server-side and returned to the client as a generic message. 4xx keep
    # their (intentional) detail.
    @app.exception_handler(StarletteHTTPException)
    async def _sanitize_http_exception(request: Request, exc: StarletteHTTPException):
        if exc.status_code >= 500:
            logger.error("5xx on %s %s: %s", request.method, request.url.path, exc.detail)
            return JSONResponse(status_code=exc.status_code, content={"detail": "Internal server error"})
        return await http_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def _sanitize_unhandled(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    # /uploads was a public StaticFiles mount. The genuinely sensitive new data is
    # gate/* — КПП snapshots (photos of people and cars / licence plates) — so those
    # now require a guard/staff session. avatars/ and meter_readings/ stay publicly
    # readable as before: clients (incl. the Flutter app via bare NetworkImage URLs)
    # load them without a session, and full gating broke mobile avatars.
    uploads_root = pathlib.Path("uploads").resolve()
    gate_view_roles = {RoleEnum.GUARD, RoleEnum.ADMIN, RoleEnum.ROOT, RoleEnum.OPERATOR}

    @app.get("/uploads/{file_path:path}")
    def serve_upload(file_path: str, request: Request):
        target = (uploads_root / file_path).resolve()
        if not target.is_relative_to(uploads_root) or not target.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        # Only КПП gate snapshots are access-controlled.
        if target.relative_to(uploads_root).parts[0] == "gate":
            uid = get_user_id_from_session(request)
            if uid is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            db = SessionLocal()
            try:
                u = db.get(User, uid)
                if not (u and u.is_active and u.role in gate_view_roles):
                    raise HTTPException(status_code=403, detail="Forbidden")
            finally:
                db.close()
        return FileResponse(target)

    # Staff-only routers (audit F-08): these manage all residents/billing data and
    # are NEVER called by the resident panel (resident data flows through
    # api_resident_dashboard). Gate the whole router so a logged-in RESIDENT can't
    # enumerate/mutate other people's data. Any non-resident staff role is allowed.
    staff_only = [Depends(require_any_role(
        RoleEnum.ROOT, RoleEnum.ADMIN, RoleEnum.OPERATOR, RoleEnum.SALES))]

    app.include_router(auth_routes.router)
    app.include_router(dashboard.router)
    app.include_router(api_users.router)
    app.include_router(api_blocks.router)
    app.include_router(api_tariffs.router, dependencies=staff_only)
    app.include_router(api_residents.router, dependencies=staff_only)
    app.include_router(api_readings.router, dependencies=staff_only)
    app.include_router(api_tenants.router, dependencies=staff_only)
    app.include_router(api_invoices.router, dependencies=staff_only)
    app.include_router(api_payments.router, dependencies=staff_only)
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
    app.include_router(api_access.router)
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
