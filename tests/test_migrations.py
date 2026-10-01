"""
Migration round trips on a scratch database (SVC-319, SVC-339).

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

import importlib.util
import json
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
# SVC-339 (two-entity model) and the revision it builds on, pinned the same way.
SVC_339 = "b000e0a06330"
BEFORE_SVC_339 = "f43a6075cfbb"
SVC_339_FILE = (
    REPO_ROOT / "alembic" / "versions" / "b000e0a06330_svc_339_add_professionals_table_booking_.py"
)


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


# --- SVC-339: professionals table + Pro-for-self backfill -----------------------


def _svc_339_sql(name: str) -> str:
    """A SQL constant from the SVC-339 migration, so the re-run test executes the
    statement the migration did rather than a copy that could drift from it."""
    spec = importlib.util.spec_from_file_location("svc_339_migration", SVC_339_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


def _seed_before_svc_339(url: URL) -> dict:
    """Rows shaped like beta's, in the schema as it stood before SVC-339.

    Three providers cover the ``display_name`` fallbacks: a name; no name but a
    business name; a blank name and no business name. Three bookings cover the
    backfill: accepted by a provider; still unassigned; ``provider_id`` naming a
    user with no provider profile, so there is no Pro to point at.
    """
    keys = (
        "consumer",
        "admin",
        "address",
        "sunita",
        "unnamed",
        "blank",
        "no_profile",
        "booked",
        "unassigned",
        "orphaned",
    )
    ids = {key: uuid.uuid4() for key in keys}
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for key, role, name in (
                ("consumer", "CONSUMER", "Consumer"),
                ("admin", "ADMIN", "Admin"),
                ("sunita", "PROVIDER", "Sunita"),
                ("unnamed", "PROVIDER", None),
                ("blank", "PROVIDER", "   "),
                ("no_profile", "PROVIDER", "No Profile"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO users (id, phone, name, role, is_active) "
                        "VALUES (:id, :phone, :name, :role, true)"
                    ),
                    {
                        "id": ids[key],
                        "phone": f"+9{ids[key].int % 10**11:011d}",
                        "name": name,
                        "role": role,
                    },
                )

            def profile(key: str, **extra) -> None:
                columns = {
                    "id": uuid.uuid4(),
                    "user_id": ids[key],
                    "service_area_radius_km": 10,
                    "avg_rating": 0.0,
                    "total_completed_jobs": 0,
                    "kyc_status": "PENDING",
                    "is_online": False,
                    **extra,
                }
                values = ", ".join(
                    f"CAST(:{c} AS jsonb)" if c == "kyc_docs" else f":{c}" for c in columns
                )
                conn.execute(
                    text(f"INSERT INTO provider_profiles ({', '.join(columns)}) VALUES ({values})"),
                    columns,
                )

            profile(
                "sunita",
                kyc_status="VERIFIED",
                kyc_docs=json.dumps({"aadhaar_front": "kyc/sunita/front.jpg"}),
                kyc_reviewed_by=ids["admin"],
                is_online=True,
                profile_photo_url="https://example.test/sunita.jpg",
            )
            profile("unnamed", business_name="Sparkle Cleaners")
            profile("blank")

            conn.execute(
                text(
                    "INSERT INTO addresses (id, user_id, label, full_address, is_default) "
                    "VALUES (:id, :user_id, 'Home', '1 Test Road', true)"
                ),
                {"id": ids["address"], "user_id": ids["consumer"]},
            )
            for key, provider, status in (
                ("booked", ids["sunita"], "COMPLETED"),
                ("unassigned", None, "REQUESTED"),
                ("orphaned", ids["no_profile"], "ACCEPTED"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO bookings (id, consumer_id, provider_id, consumer_address_id, "
                        "status, service_amount, platform_commission, provider_payout, "
                        "payment_status, discount_amount, tip_amount) "
                        "VALUES (:id, :consumer, :provider, :address, :status, 100, 20, 80, "
                        "'PENDING', 0, 0)"
                    ),
                    {
                        "id": ids[key],
                        "consumer": ids["consumer"],
                        "provider": provider,
                        "address": ids["address"],
                        "status": status,
                    },
                )
    finally:
        engine.dispose()
    return ids


def _query(url: URL, sql: str):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql)).mappings().all()
    finally:
        engine.dispose()


def _professionals(url: URL) -> dict:
    """``{service_provider_id: row}`` for every Pro."""
    rows = _query(url, "SELECT * FROM professionals")
    return {row["service_provider_id"]: dict(row) for row in rows}


def _booking_professionals(url: URL) -> dict:
    """``{booking_id: professional_id}``."""
    rows = _query(url, "SELECT id, professional_id FROM bookings")
    return {row["id"]: row["professional_id"] for row in rows}


def test_svc_339_schema_matches_the_lld_and_downgrade_restores_the_tables(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_339)
    before = {table: _shape(scratch_db, table) for table in ("bookings", "reviews")}

    _alembic(scratch_db, "upgrade", SVC_339)
    pros = _shape(scratch_db, "professionals")

    foreign_keys = {fk[1]: fk[2:] for fk in pros["foreign_keys"]}
    assert foreign_keys[("service_provider_id",)] == ("provider_profiles", ("user_id",), "CASCADE")
    assert foreign_keys[("user_id",)] == ("users", ("id",), None)
    assert ("uq_professionals_user_sp", ("user_id", "service_provider_id")) in pros[
        "unique_constraints"
    ]
    nullable = {name: is_nullable for name, _, is_nullable in pros["columns"]}
    assert nullable["service_provider_id"] is False
    assert nullable["display_name"] is False

    indexes = {i[0]: i[1:] for i in pros["indexes"]}
    assert indexes["ix_professionals_sp_id"][0] == ("service_provider_id",)
    assert "is_online = true" in indexes["ix_professionals_online"][2].lower(), "must be partial"
    assert "ix_professionals_location" in indexes

    for table in ("bookings", "reviews"):
        fks = {fk[0]: fk[1:4] for fk in _shape(scratch_db, table)["foreign_keys"]}
        assert fks[f"fk_{table}_professional_id"] == (
            ("professional_id",),
            "professionals",
            ("id",),
        )
    booking_indexes = {i[0]: i[1:] for i in _shape(scratch_db, "bookings")["indexes"]}
    assert "professional_id IS NOT NULL" in booking_indexes["ix_bookings_professional_id"][2]

    _alembic(scratch_db, "downgrade", BEFORE_SVC_339)
    assert {table: _shape(scratch_db, table) for table in ("bookings", "reviews")} == before
    engine = create_engine(scratch_db)
    try:
        assert "professionals" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_svc_339_gives_every_provider_a_pro_for_self_and_backfills_bookings(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_339)
    ids = _seed_before_svc_339(scratch_db)

    _alembic(scratch_db, "upgrade", SVC_339)

    # Every SP has exactly one Pro — the same comparison as the post-deploy check.
    pros = _professionals(scratch_db)
    providers = _query(scratch_db, "SELECT user_id FROM provider_profiles")
    assert set(pros) == {row["user_id"] for row in providers}
    assert len(pros) == len(providers) == 3
    for sp_id, pro in pros.items():
        assert pro["user_id"] == sp_id, "solo case: the Pro-for-self shares the SP's user_id"

    sunita = pros[ids["sunita"]]
    assert sunita["display_name"] == "Sunita"
    assert sunita["kyc_status"] == "VERIFIED"
    assert sunita["kyc_docs"] == {"aadhaar_front": "kyc/sunita/front.jpg"}
    assert sunita["kyc_reviewed_by"] == ids["admin"]
    assert sunita["is_online"] is True
    assert sunita["profile_photo_url"] == "https://example.test/sunita.jpg"
    # users.name is nullable and display_name is not, so the backfill falls back.
    assert pros[ids["unnamed"]]["display_name"] == "Sparkle Cleaners"
    assert pros[ids["blank"]]["display_name"] == "Professional"

    bookings = _booking_professionals(scratch_db)
    assert bookings[ids["booked"]] == sunita["id"]
    assert bookings[ids["unassigned"]] is None
    assert bookings[ids["orphaned"]] is None, "no provider profile, so no Pro to point at"


def test_svc_339_backfill_is_idempotent(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_339)
    ids = _seed_before_svc_339(scratch_db)
    _alembic(scratch_db, "upgrade", SVC_339)
    pros, bookings = _professionals(scratch_db), _booking_professionals(scratch_db)

    # The migration's own statements, run a second time.
    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            conn.execute(text(_svc_339_sql("BACKFILL_PROFESSIONALS")))
            conn.execute(text(_svc_339_sql("BACKFILL_BOOKINGS")))
    finally:
        engine.dispose()
    assert _professionals(scratch_db) == pros
    assert _booking_professionals(scratch_db) == bookings

    # Down and back up lands on the same state. Pro ids are regenerated, so
    # compare by what each row means rather than by its id.
    def meaning(pros: dict, bookings: dict) -> tuple:
        sp_of = {pro["id"]: sp_id for sp_id, pro in pros.items()}
        return (
            {sp: (p["user_id"], p["display_name"], p["kyc_status"]) for sp, p in pros.items()},
            {booking: sp_of.get(pro_id) for booking, pro_id in bookings.items()},
        )

    _alembic(scratch_db, "downgrade", BEFORE_SVC_339)
    _alembic(scratch_db, "upgrade", SVC_339)
    again = meaning(_professionals(scratch_db), _booking_professionals(scratch_db))
    assert again == meaning(pros, bookings)
    assert again[1][ids["booked"]] == ids["sunita"]
