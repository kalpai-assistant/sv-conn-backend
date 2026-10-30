"""End-to-end guard for the seed pipeline (SVC-312).

Runs ``alembic upgrade head`` + ``seed_categories.py`` + ``seed_pricing_from_xlsx.py``
against a throwaway Postgres DB, then asserts the SVC-286 hierarchy is
intact:

* 7 rows with ``parent_id IS NULL`` in ``categories`` (the 6 L1 tiles +
  ``miscellaneous``).
* Every expected L1 slug is present.
* Every one of the 17 L2 slugs has a non-null ``parent_id`` that points
  at the correct L1.

Also runs the whole pipeline TWICE to prove re-runs are idempotent for
categories — the original bug (SVC-312) was that the second run of
``seed_pricing_from_xlsx.py`` wiped the L1 hierarchy on the way in.

Runs on top of the CI Postgres container (``sc_user@localhost:5432``,
which is superuser by default in the official Postgres images). Skipped
locally when no Postgres is reachable or when the pricing workbook isn't
checked in.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import pytest
from psycopg2 import OperationalError, sql

from app.seed_data.l1_hierarchy import (
    L1_TILES,
    L2_TO_L1,
    MISCELLANEOUS_L1_SLUG,
)
from app.seed_data.service_details import service_details_params

BACKEND_DIR = Path(__file__).resolve().parent.parent
WORKBOOK = BACKEND_DIR / "EazyServ_Pune_Pricing_List.xlsx"

# Derive from the same Postgres CI uses (see .github/workflows/ci.yml).
# Overridable via env for local runs against docker-compose.
BASE_SYNC_URL = os.environ.get(
    "SEED_TEST_BASE_URL",
    "postgresql+psycopg2://sc_user:sc_password@localhost:5432",
)
SEED_DB_NAME = "sc_test_seeds"


def _psycopg_dsn(url: str, dbname: str) -> dict:
    """Split a SQLAlchemy-style URL into psycopg2 keyword args."""
    parsed = urlparse(url.replace("postgresql+psycopg2://", "postgresql://"))
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "user": parsed.username,
        "password": parsed.password,
        "dbname": dbname,
    }


def _admin_dbname() -> str | None:
    """Return the first admin DB we can connect to as sc_user, or None.

    CI's postgres container creates the default ``postgres`` DB. Local
    Homebrew Postgres installs often don't, but always keep ``template1``.
    Try both so the test runs in either environment without local setup.
    """
    for candidate in ("postgres", "template1"):
        try:
            conn = psycopg2.connect(**_psycopg_dsn(BASE_SYNC_URL, candidate), connect_timeout=2)
            conn.close()
            return candidate
        except OperationalError:
            continue
    return None


ADMIN_DB = _admin_dbname()


pytestmark = [
    pytest.mark.skipif(
        ADMIN_DB is None,
        reason="Postgres not reachable at SEED_TEST_BASE_URL — skip locally without docker-compose up",
    ),
    pytest.mark.skipif(
        not WORKBOOK.exists(),
        reason=f"Pricing workbook missing at {WORKBOOK} — required for seed_pricing_from_xlsx.py",
    ),
]


def _admin_conn():
    assert ADMIN_DB is not None  # guarded by pytestmark
    conn = psycopg2.connect(**_psycopg_dsn(BASE_SYNC_URL, ADMIN_DB))
    conn.autocommit = True
    return conn


def _seed_conn():
    return psycopg2.connect(**_psycopg_dsn(BASE_SYNC_URL, SEED_DB_NAME))


def _run(cmd: list[str], env: dict) -> None:
    """Run a subprocess, surfacing stderr into the pytest failure diff."""
    result = subprocess.run(
        cmd,
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Command failed ({' '.join(cmd)}):\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )


@pytest.fixture(scope="module")
def seeded_db():
    """Create sc_test_seeds, migrate + seed it TWICE, tear it down.

    Second seed round proves idempotency: L1 rows must not be wiped and
    no dupes may be produced. See SVC-312.
    """
    async_url = (
        BASE_SYNC_URL.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        + f"/{SEED_DB_NAME}"
    )
    sync_url = f"{BASE_SYNC_URL}/{SEED_DB_NAME}"

    # (Re)create the scratch DB from a fresh state. psycopg2's `with conn`
    # opens an implicit transaction — CREATE/DROP DATABASE can't run inside
    # one — so we manage the connection lifecycle by hand here.
    admin = _admin_conn()
    try:
        with admin.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()"
                ),
                (SEED_DB_NAME,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(SEED_DB_NAME)))
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(SEED_DB_NAME)))
    finally:
        admin.close()

    # PostGIS + pgcrypto: the migration hierarchy uses gen_random_uuid()
    # (pgcrypto) and the schema declares Geography columns (postgis).
    seed = _seed_conn()
    seed.autocommit = True
    try:
        with seed.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    finally:
        seed.close()

    env = {
        **os.environ,
        "DATABASE_URL": async_url,
        "SYNC_DATABASE_URL": sync_url,
        "ALEMBIC_DATABASE_URL": sync_url,
    }

    # Run the ritual twice to exercise idempotency. The first pass fills
    # the DB; the second must not wipe or duplicate anything.
    for _ in range(2):
        _run([sys.executable, "-m", "alembic", "upgrade", "head"], env)
        _run([sys.executable, "scripts/seed_categories.py"], env)
        _run([sys.executable, "scripts/seed_pricing_from_xlsx.py"], env)

    yield sync_url

    admin = _admin_conn()
    try:
        with admin.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()"
                ),
                (SEED_DB_NAME,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(SEED_DB_NAME)))
    finally:
        admin.close()


def test_seven_l1_tiles(seeded_db) -> None:
    with _seed_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM categories WHERE parent_id IS NULL")
        (count,) = cur.fetchone()
    assert count == 7, f"Expected 7 L1 tiles (6 illustrated + miscellaneous), got {count}"


def test_expected_l1_slugs_present(seeded_db) -> None:
    expected = {slug for slug, _name, _order in L1_TILES} | {MISCELLANEOUS_L1_SLUG}
    with _seed_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT slug FROM categories WHERE parent_id IS NULL")
        actual = {row[0] for row in cur.fetchall()}
    assert (
        actual == expected
    ), f"L1 slug set mismatch. Missing: {expected - actual}. Extra: {actual - expected}."


def test_l2_slugs_link_to_correct_l1(seeded_db) -> None:
    with _seed_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT l2.slug, l1.slug
            FROM categories l2
            JOIN categories l1 ON l1.id = l2.parent_id
            WHERE l2.slug = ANY(%s)
            """,
            (list(L2_TO_L1.keys()),),
        )
        actual = dict(cur.fetchall())
    missing = set(L2_TO_L1) - set(actual)
    assert not missing, f"L2 slugs missing / unlinked: {sorted(missing)}"
    mismapped = {
        slug: (actual[slug], expected)
        for slug, expected in L2_TO_L1.items()
        if actual[slug] != expected
    }
    assert not mismapped, f"L2 slugs pointing at the wrong L1: {mismapped}"


def test_no_duplicate_categories(seeded_db) -> None:
    """The whole point of SVC-312 — the second seed pass must not dupe rows."""
    with _seed_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT slug, COUNT(*) FROM categories GROUP BY slug HAVING COUNT(*) > 1")
        dupes = cur.fetchall()
    assert not dupes, f"Duplicate category slugs after re-seed: {dupes}"


def test_service_details_survive_the_seed_ritual(seeded_db) -> None:
    """SVC-340/SVC-400: the migrations run before any service exists, and the
    second pricing pass deletes and recreates every service — so the content
    only exists here if the seed script re-applies it."""
    with _seed_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.slug, s.name, s.steps, s.exclusions, s.included_items, s.faqs
            FROM services s JOIN categories c ON c.id = s.category_id
            WHERE s.steps IS NOT NULL OR s.exclusions IS NOT NULL
               OR s.included_items IS NOT NULL OR s.faqs IS NOT NULL
            """
        )
        actual = {(slug, name): tuple(rest) for slug, name, *rest in cur}
    assert actual == {
        (p["category_slug"], p["name"]): tuple(
            json.loads(p[col]) for col in ("steps", "exclusions", "included_items", "faqs")
        )
        for p in service_details_params()
    }


def test_painting_total_survives_the_seed_ritual(seeded_db) -> None:
    """SVC-400 follow-up: the pricing pass recreates services at 60; the
    re-apply puts painting's one-day total back and leaves the rest alone."""
    with _seed_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.slug, s.name, s.duration_minutes
            FROM services s JOIN categories c ON c.id = s.category_id
            WHERE s.duration_minutes <> 60
            """
        )
        assert cur.fetchall() == [("painting", "Full room painting", 480)]
