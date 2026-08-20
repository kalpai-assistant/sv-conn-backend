"""
Alembic async env — supports both online (async, for CI/migrations) and
offline (SQL output) modes. Imports all models so metadata is populated.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.models  # noqa: F401 — side-effect import registers all mappers
from alembic import context

# ---------------------------------------------------------------------------
# Populate Base.metadata by importing all models
# ---------------------------------------------------------------------------
from app.database import Base

# ---------------------------------------------------------------------------
# Alembic config object
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Prefer DATABASE_URL from environment for flexibility."""
    import os

    from app.config import get_settings

    settings = get_settings()
    # Alembic needs the psycopg2 sync URL for offline mode
    return os.getenv(
        "ALEMBIC_DATABASE_URL",
        settings.SYNC_DATABASE_URL
        or settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg2"),
    )


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table" and name in {
        "spatial_ref_sys",
        "topology",
        "layer",
        "faces",
        "county",
        "addr",
        "addrfeat",
        "street_type_lookup",
        "secondary_unit_lookup",
        "zip_lookup",
        "zip_state_loc",
        "pagc_gaz",
        "cousub",
        "pagc_lex",
        "loader_lookuptables",
        "direction_lookup",
        "geocode_settings",
        "zcta5",
        "pagc_rules",
        "tract",
        "tabblock20",
        "loader_variables",
        "tabblock",
        "zip_lookup_all",
        "loader_platform",
        "zip_lookup_base",
        "place_lookup",
        "geocode_settings_default",
        "edges",
        "node",
        "place",
        "zip_state",
        "bg",
        "state",
        "countysub_lookup",
        "featnames",
        "state_lookup",
        "county_lookup",
    }:
        return False
    return True


# ---------------------------------------------------------------------------
# Offline mode — generate SQL without a live DB connection
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode — run against a live async engine
# ---------------------------------------------------------------------------
def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    import os

    from app.config import get_settings

    settings = get_settings()

    # Prefer ALEMBIC_DATABASE_URL so operators can point a run at a specific
    # DB (beta, a Neon branch, a throwaway) without editing .env. Offline
    # mode has always respected this via get_url() — online mode used to
    # hard-code settings.DATABASE_URL, which meant `ALEMBIC_DATABASE_URL=... alembic
    # upgrade head` silently migrated whichever DB .env pointed at. Accept
    # a sync-form URL (psycopg2) too and convert to asyncpg for the engine.
    override = os.getenv("ALEMBIC_DATABASE_URL")
    if override:
        db_url = override.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
            "psycopg2://", "postgresql+asyncpg://"
        )
        if db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        # asyncpg uses `ssl=`, psycopg2 uses `sslmode=` — same semantics,
        # different key. Translate so an operator can paste either form.
        db_url = db_url.replace("sslmode=require", "ssl=require")
    else:
        db_url = settings.DATABASE_URL

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = db_url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
