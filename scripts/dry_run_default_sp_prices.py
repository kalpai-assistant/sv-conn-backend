"""
Dry run of the SVC-418 data migration (471bb682ac1b): what it would write, without
writing anything.

    uv run python scripts/dry_run_default_sp_prices.py
    uv run python scripts/dry_run_default_sp_prices.py --database-url postgresql+psycopg2://...

Run it before ``alembic upgrade`` and review the numbers. It reads the same frozen
SQL as the migration (``app/seed_data/svc_418_default_prices.py``) inside a READ
ONLY transaction that is rolled back, so it can't change the database. The URL
defaults to ``ALEMBIC_DATABASE_URL``, then ``SYNC_DATABASE_URL``, the same order
alembic uses.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.seed_data.svc_418_default_prices import default_price_counts  # noqa: E402


def default_url() -> str:
    override = os.getenv("ALEMBIC_DATABASE_URL")
    if override:
        return override
    from app.config import get_settings

    settings = get_settings()
    return settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace(
        "postgresql+asyncpg", "postgresql+psycopg2"
    )


def collect(url: str) -> dict[str, int]:
    """The dry-run counts for the database at ``url``. Writes nothing."""
    engine = create_engine(make_url(url).set(drivername="postgresql+psycopg2"))
    try:
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            try:
                return default_price_counts(conn)
            finally:
                conn.rollback()
    finally:
        engine.dispose()


def report(counts: dict[str, int]) -> str:
    return "\n".join(
        [
            "SVC-418 dry run (nothing written)",
            f"  subscriptions (provider_services):     {counts['subscriptions']}",
            f"  ... to an active service:              {counts['active_subscriptions']}",
            f"  SPs with such a subscription:          {counts['sps']}",
            f"  price rows due (active variants):      {counts['candidates']}",
            f"  ... already present (kept as is):      {counts['already_present']}",
            f"  ... to insert:                         {counts['to_insert']}",
        ]
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry run of the SVC-418 data migration.")
    parser.add_argument("--database-url", help="Defaults to ALEMBIC_DATABASE_URL / .env")
    args = parser.parse_args()
    url = args.database_url or default_url()
    print(f"database: {make_url(url).render_as_string(hide_password=True)}")
    print(report(collect(url)))
