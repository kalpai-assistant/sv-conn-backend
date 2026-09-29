"""
Migration round trips on a scratch database (SVC-319).

The rest of the suite builds its schema with ``Base.metadata.create_all``, and CI
only ever runs ``alembic upgrade head`` — so a migration's ``downgrade()`` is not
exercised anywhere else. These tests run the real migration files against a
throwaway database and check that upgrading and then downgrading leaves the table
exactly as it was.

Alembic runs in a subprocess, for two reasons: ``alembic/env.py`` drives
migrations through ``asyncio.run``, which must not share pytest's event loop; and
its ``fileConfig`` call would reconfigure logging for every test that runs after
this one. The scratch database reaches it through ``ALEMBIC_DATABASE_URL``,
which ``env.py`` honours in online mode.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

from tests.conftest import TEST_DATABASE_URL

REPO_ROOT = Path(__file__).resolve().parent.parent

# SVC-319 and the revision it builds on. Pinned rather than "head" / "-1" so this
# keeps checking the SVC-319 migration after later ones land on top of it.
SVC_319 = "fb1430a83256"
BEFORE_SVC_319 = "b4e0a7c19d52"


def _url(database: str) -> URL:
    """The test server's URL, sync driver, pointed at ``database``."""
    return make_url(TEST_DATABASE_URL).set(drivername="postgresql+psycopg2", database=database)


@pytest.fixture
def scratch_db():
    """An empty database with PostGIS, dropped afterwards even if the test fails."""
    name = f"sc_migration_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        # The tables use geography columns, but no migration creates the PostGIS
        # extension — deployed databases get it from the PostGIS image. A new
        # database made from template1 does not, so add it here.
        engine = create_engine(_url(name))
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        finally:
            engine.dispose()
        yield _url(name)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _alembic(url: URL, *args: str) -> None:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": url.render_as_string(hide_password=False)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr}"


def _shape(url: URL, table: str) -> dict:
    """Everything about ``table`` a migration could change, in comparable form."""
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        return {
            "columns": sorted(
                (c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns(table)
            ),
            "indexes": sorted(
                (
                    i["name"],
                    tuple(i["column_names"]),
                    bool(i["unique"]),
                    str(i.get("dialect_options", {}).get("postgresql_where")),
                )
                for i in insp.get_indexes(table)
            ),
            "foreign_keys": sorted(
                (
                    fk["name"],
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    tuple(fk["referred_columns"]),
                    (fk.get("options") or {}).get("ondelete"),
                )
                for fk in insp.get_foreign_keys(table)
            ),
            "unique_constraints": sorted(
                (u["name"], tuple(u["column_names"])) for u in insp.get_unique_constraints(table)
            ),
            "primary_key": insp.get_pk_constraint(table)["constrained_columns"],
        }
    finally:
        engine.dispose()


def test_svc_319_upgrade_then_downgrade_leaves_provider_profiles_unchanged(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_319)
    before = _shape(scratch_db, "provider_profiles")
    assert "parent_provider_id" not in {name for name, _, _ in before["columns"]}

    _alembic(scratch_db, "upgrade", SVC_319)
    upgraded = _shape(scratch_db, "provider_profiles")

    columns = {name: nullable for name, _, nullable in upgraded["columns"]}
    assert columns.get("parent_provider_id") is True, "column missing or NOT NULL"

    foreign_keys = {fk[0]: fk[1:] for fk in upgraded["foreign_keys"]}
    assert foreign_keys.get("fk_provider_profiles_parent") == (
        ("parent_provider_id",),
        "provider_profiles",
        ("user_id",),
        "SET NULL",
    )

    indexes = {i[0]: i[1:] for i in upgraded["indexes"]}
    assert "ix_provider_profiles_parent" in indexes
    columns_indexed, unique, where = indexes["ix_provider_profiles_parent"]
    assert columns_indexed == ("parent_provider_id",)
    assert unique is False
    assert "parent_provider_id IS NOT NULL" in where, "index must be partial"

    _alembic(scratch_db, "downgrade", BEFORE_SVC_319)
    assert _shape(scratch_db, "provider_profiles") == before
