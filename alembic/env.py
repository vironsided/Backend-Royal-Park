import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Make the app package importable when alembic runs from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import engine, Base  # noqa: E402
import app.models  # noqa: F401,E402  — register all tables on Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
