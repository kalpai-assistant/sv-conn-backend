"""
Migration round trips on a scratch database (SVC-319, SVC-339, SVC-340, SVC-320,
SVC-344, SVC-333, SVC-329, SVC-366).

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

from app.seed_data.service_details import (
    SERVICE_DETAILS,
    UPDATE_SERVICE_DETAILS,
    service_details_params,
)
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
# SVC-340 (service steps + exclusions) and the revision it builds on. Its text
# and UPDATE live in app.seed_data.service_details, which the migration imports.
SVC_340 = "4d1cca7bc41d"
BEFORE_SVC_340 = SVC_339
# SVC-320 PR 1 (team invites + professionals.is_active + catch-up backfill).
SVC_320 = "17702cbd14a4"
BEFORE_SVC_320 = SVC_340
SVC_320_FILE = (
    REPO_ROOT / "alembic" / "versions" / "17702cbd14a4_svc_320_team_invites_professionals_is_.py"
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


def _migration_sql(path: Path, name: str) -> str:
    """A SQL constant from a migration file, so a re-run test executes the
    statement the migration did rather than a copy that could drift from it."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


def _svc_339_sql(name: str) -> str:
    return _migration_sql(SVC_339_FILE, name)


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


# --- SVC-340: services.steps + services.exclusions -----------------------------


def _seed_before_svc_340(url: URL) -> dict:
    """Services in the schema as it stood before SVC-340.

    Two match seed keys, one of them with the apostrophe in "Men's haircut". Two
    don't: a different name in a seeded category, and a seeded name in a
    category the seed doesn't name — the key is (category slug, name), not name.
    """
    cats = {slug: uuid.uuid4() for slug in ("plumber", "salon-men", "svc-340-other")}
    ids = {key: uuid.uuid4() for key in ("drain", "haircut", "beard", "decoy")}
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for slug, cat_id in cats.items():
                conn.execute(
                    text(
                        "INSERT INTO categories (id, name, slug, is_active, sort_order) "
                        "VALUES (:id, :name, :slug, true, 1)"
                    ),
                    {"id": cat_id, "name": f"SVC-340 {slug}", "slug": slug},
                )
            for key, slug, name in (
                ("drain", "plumber", "Drain blockage removal"),
                ("haircut", "salon-men", "Men's haircut"),
                ("beard", "salon-men", "Beard styling"),
                ("decoy", "svc-340-other", "Drain blockage removal"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO services (id, category_id, name, base_price, "
                        "duration_minutes, is_active) "
                        "VALUES (:id, :category_id, :name, 99, 60, true)"
                    ),
                    {"id": ids[key], "category_id": cats[slug], "name": name},
                )
    finally:
        engine.dispose()
    return ids


def _service_details(url: URL) -> dict:
    """``{service_id: (steps, exclusions)}``."""
    rows = _query(url, "SELECT id, steps, exclusions FROM services")
    return {row["id"]: (row["steps"], row["exclusions"]) for row in rows}


def test_svc_340_adds_nullable_jsonb_columns_and_downgrade_restores_services(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_340)
    before = _shape(scratch_db, "services")

    _alembic(scratch_db, "upgrade", SVC_340)
    upgraded = _shape(scratch_db, "services")
    columns = {name: (type_, nullable) for name, type_, nullable in upgraded["columns"]}
    assert columns["steps"] == ("JSONB", True)
    assert columns["exclusions"] == ("JSONB", True)
    # Only the two columns changed: no index, constraint or key.
    assert {k: v for k, v in upgraded.items() if k != "columns"} == {
        k: v for k, v in before.items() if k != "columns"
    }
    assert set(upgraded["columns"]) - set(before["columns"]) == {
        ("steps", "JSONB", True),
        ("exclusions", "JSONB", True),
    }

    _alembic(scratch_db, "downgrade", BEFORE_SVC_340)
    assert _shape(scratch_db, "services") == before


def test_svc_340_seeds_matching_services_only_and_is_idempotent(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_340)
    ids = _seed_before_svc_340(scratch_db)

    _alembic(scratch_db, "upgrade", SVC_340)

    details = _service_details(scratch_db)
    assert details[ids["drain"]] == SERVICE_DETAILS[("plumber", "Drain blockage removal")]
    assert details[ids["haircut"]] == SERVICE_DETAILS[("salon-men", "Men's haircut")]
    assert details[ids["beard"]] == (None, None), "name not in the seed"
    assert details[ids["decoy"]] == (None, None), "seeded name, but in another category"

    # The migration's own statement, run a second time: same rows, same values.
    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            matched = sum(
                conn.execute(text(UPDATE_SERVICE_DETAILS), params).rowcount
                for params in service_details_params()
            )
    finally:
        engine.dispose()
    assert matched == 2
    assert _service_details(scratch_db) == details


# --- SVC-320: team invites, professionals.is_active, catch-up backfill ---------


def _seed_after_svc_339(url: URL) -> dict:
    """What beta looks like between the SVC-339 and SVC-320 deploys.

    The SVC-339-era rows from ``_seed_before_svc_339`` (Sunita has a Pro-for-self
    and a linked booking) plus ``late``: a provider who signed up afterwards,
    when nothing created a Pro, with an accepted booking whose
    ``professional_id`` is NULL. ``open`` is an unassigned booking that must
    stay NULL.
    """
    ids = _seed_before_svc_339(url)
    _alembic(url, "upgrade", BEFORE_SVC_320)
    for key in ("late", "late_booking", "open"):
        ids[key] = uuid.uuid4()
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, phone, name, role, is_active) "
                    "VALUES (:id, '+919000000001', 'Late Signup', 'PROVIDER', true)"
                ),
                {"id": ids["late"]},
            )
            conn.execute(
                text(
                    "INSERT INTO provider_profiles (id, user_id, service_area_radius_km, "
                    "avg_rating, total_completed_jobs, kyc_status, is_online) "
                    "VALUES (:id, :user_id, 10, 0, 0, 'PENDING', false)"
                ),
                {"id": uuid.uuid4(), "user_id": ids["late"]},
            )
            for key, provider, status in (
                ("late_booking", ids["late"], "ACCEPTED"),
                ("open", None, "REQUESTED"),
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


def test_svc_320_schema_and_downgrade_restores_professionals(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_320)
    before = _shape(scratch_db, "professionals")

    _alembic(scratch_db, "upgrade", SVC_320)
    pro_columns = {
        name: nullable for name, _, nullable in _shape(scratch_db, "professionals")["columns"]
    }
    assert pro_columns["is_active"] is False, "is_active must be NOT NULL"

    invites = _shape(scratch_db, "provider_team_invites")
    foreign_keys = {fk[1]: fk[2:] for fk in invites["foreign_keys"]}
    assert foreign_keys[("invited_by",)] == ("provider_profiles", ("user_id",), "CASCADE")
    assert foreign_keys[("accepted_by_user_id",)] == ("users", ("id",), None)
    # A 6-digit code repeats over time, so it must not be globally unique.
    assert invites["unique_constraints"] == []
    nullable = {name: is_nullable for name, _, is_nullable in invites["columns"]}
    for column in ("invited_by", "invitee_phone", "token", "status", "expires_at", "created_at"):
        assert nullable[column] is False, column
    indexes = {i[0]: i[1:] for i in invites["indexes"]}
    assert {name: (cols, unique) for name, (cols, unique, _) in indexes.items()} == {
        "ux_provider_team_invites_pending": (("invited_by", "invitee_phone"), True),
        "ux_provider_team_invites_pending_code": (("invitee_phone", "token"), True),
    }
    for name, (_, _, where) in indexes.items():
        assert "PENDING" in where, f"{name} must be partial on status = 'PENDING'"

    _alembic(scratch_db, "downgrade", BEFORE_SVC_320)
    assert _shape(scratch_db, "professionals") == before
    engine = create_engine(scratch_db)
    try:
        assert "provider_team_invites" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_svc_320_backfills_providers_and_bookings_since_svc_339(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_339)
    ids = _seed_after_svc_339(scratch_db)
    before_pros = _professionals(scratch_db)
    assert ids["late"] not in before_pros, "the gap this migration closes"

    _alembic(scratch_db, "upgrade", SVC_320)

    pros = _professionals(scratch_db)
    providers = _query(scratch_db, "SELECT user_id FROM provider_profiles")
    assert set(pros) == {row["user_id"] for row in providers}
    late = pros[ids["late"]]
    assert late["user_id"] == ids["late"]
    assert late["display_name"] == "Late Signup"
    assert all(p["is_active"] is True for p in pros.values())
    # Pros SVC-339 made are untouched.
    for sp_id, pro in before_pros.items():
        assert pros[sp_id]["id"] == pro["id"]

    bookings = _booking_professionals(scratch_db)
    assert bookings[ids["late_booking"]] == late["id"]
    assert bookings[ids["booked"]] == pros[ids["sunita"]]["id"]
    assert bookings[ids["open"]] is None
    assert bookings[ids["orphaned"]] is None


def test_svc_320_backfill_is_idempotent(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_339)
    _seed_after_svc_339(scratch_db)
    _alembic(scratch_db, "upgrade", SVC_320)
    pros, bookings = _professionals(scratch_db), _booking_professionals(scratch_db)

    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            conn.execute(text(_migration_sql(SVC_320_FILE, "BACKFILL_PROFESSIONALS")))
            conn.execute(text(_migration_sql(SVC_320_FILE, "BACKFILL_BOOKINGS")))
    finally:
        engine.dispose()
    assert _professionals(scratch_db) == pros
    assert _booking_professionals(scratch_db) == bookings

    # Down and up again. The backfilled rows belong to SVC-339's schema and
    # survive the downgrade, so the re-upgrade leaves them exactly as they were.
    _alembic(scratch_db, "downgrade", BEFORE_SVC_320)
    _alembic(scratch_db, "upgrade", SVC_320)
    assert _professionals(scratch_db) == pros
    assert _booking_professionals(scratch_db) == bookings


# --- SVC-320 PR 2: bookings.reopened_at ------------------------------------------


SVC_320_PR2 = "e937ca1c5a84"
BEFORE_SVC_320_PR2 = SVC_320


def test_svc_320_pr2_adds_a_nullable_reopened_at_and_downgrade_restores_bookings(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_320_PR2)
    before = _shape(scratch_db, "bookings")

    _alembic(scratch_db, "upgrade", SVC_320_PR2)
    columns = {
        name: (kind, nullable) for name, kind, nullable in _shape(scratch_db, "bookings")["columns"]
    }
    assert columns["reopened_at"] == ("TIMESTAMP", True)

    _alembic(scratch_db, "downgrade", BEFORE_SVC_320_PR2)
    assert _shape(scratch_db, "bookings") == before


# --- SVC-344: bookings.last_chance_at + nullable status-log actor ------------------


SVC_344 = "dc120cbb3548"
BEFORE_SVC_344 = SVC_320_PR2


def test_svc_344_schema_and_downgrade(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_344)
    before = {t: _shape(scratch_db, t) for t in ("bookings", "booking_status_logs")}

    _alembic(scratch_db, "upgrade", SVC_344)
    bookings = {n: (k, nl) for n, k, nl in _shape(scratch_db, "bookings")["columns"]}
    assert bookings["last_chance_at"] == ("TIMESTAMP", True)
    logs = {n: nl for n, _, nl in _shape(scratch_db, "booking_status_logs")["columns"]}
    assert logs["changed_by"] is True, "NULL = the system"

    # A system row (changed_by NULL) must not block the downgrade: it is
    # attributed to the booking's consumer, then NOT NULL comes back.
    ids = _seed_before_svc_339(scratch_db)
    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO booking_status_logs (id, booking_id, from_status, to_status, "
                    "changed_by) VALUES (:id, :b, 'requested', 'cancelled', NULL)"
                ),
                {"id": uuid.uuid4(), "b": ids["unassigned"]},
            )
    finally:
        engine.dispose()

    _alembic(scratch_db, "downgrade", BEFORE_SVC_344)
    assert {t: _shape(scratch_db, t) for t in ("bookings", "booking_status_logs")} == before
    [row] = _query(
        scratch_db,
        f"SELECT changed_by FROM booking_status_logs WHERE booking_id = '{ids['unassigned']}'",
    )
    assert row["changed_by"] == ids["consumer"]


# --- SVC-333: booking_assignment_events ---------------------------------------------


SVC_333 = "e409e33c21bb"
BEFORE_SVC_333 = SVC_344


def test_svc_333_creates_the_event_table_and_downgrade_drops_it(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_333)
    before = {t: _shape(scratch_db, t) for t in ("bookings", "booking_status_logs")}

    _alembic(scratch_db, "upgrade", SVC_333)
    shape = _shape(scratch_db, "booking_assignment_events")
    assert {n: nl for n, _, nl in shape["columns"]} == {
        "id": False,
        "booking_id": False,
        "kind": False,
        "from_professional_id": True,
        "to_professional_id": True,
        "actor_user_id": True,
        "note": True,
        "created_at": False,
    }
    assert {(cols, table) for _, cols, table, _, _ in shape["foreign_keys"]} == {
        (("booking_id",), "bookings"),
        (("from_professional_id",), "professionals"),
        (("to_professional_id",), "professionals"),
        (("actor_user_id",), "users"),
    }
    assert (
        "ix_booking_assignment_events_booking_id_created_at",
        ("booking_id", "created_at"),
        False,
        "None",
    ) in shape["indexes"]
    [check] = _query(
        scratch_db,
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conname = 'ck_booking_assignment_events_kind'",
    )
    for kind in ("claimed", "repicked", "declined", "released", "provider_cancelled"):
        assert f"'{kind}'" in check["def"]

    _alembic(scratch_db, "downgrade", BEFORE_SVC_333)
    assert {t: _shape(scratch_db, t) for t in ("bookings", "booking_status_logs")} == before
    assert (
        _query(
            scratch_db,
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'booking_assignment_events'",
        )
        == []
    )


# SVC-329 (admin_actions) and the revision it builds on (SVC-326).
SVC_329 = "1866e2059674"
BEFORE_SVC_329 = "b62ccfb038a4"


def test_svc_329_creates_the_admin_actions_table_and_downgrade_drops_it(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_329)

    _alembic(scratch_db, "upgrade", SVC_329)
    shape = _shape(scratch_db, "admin_actions")
    assert {n: nl for n, _, nl in shape["columns"]} == {
        "id": False,
        "admin_user_id": False,
        "action": False,
        "target_type": False,
        "target_id": False,
        "sp_user_id": True,
        "to_sp_user_id": True,
        "reason": True,
        "details": False,
        "created_at": False,
    }
    assert {(cols, table) for _, cols, table, _, _ in shape["foreign_keys"]} == {
        (("admin_user_id",), "users"),
        (("sp_user_id",), "users"),
        (("to_sp_user_id",), "users"),
    }
    assert {(name, cols) for name, cols, *_ in shape["indexes"]} == {
        ("ix_admin_actions_target", ("target_type", "target_id")),
        ("ix_admin_actions_created_at", ("created_at",)),
        ("ix_admin_actions_sp_user_id", ("sp_user_id",)),
        ("ix_admin_actions_to_sp_user_id", ("to_sp_user_id",)),
    }
    checks = {
        row["conname"]: row["def"]
        for row in _query(
            scratch_db,
            "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
            "WHERE conname LIKE 'ck_admin_actions_%'",
        )
    }
    for action in ("user_deactivated", "pro_force_removed", "pro_reassigned"):
        assert f"'{action}'" in checks["ck_admin_actions_action"]
    for target_type in ("user", "professional"):
        assert f"'{target_type}'" in checks["ck_admin_actions_target_type"]

    _alembic(scratch_db, "downgrade", BEFORE_SVC_329)
    assert (
        _query(
            scratch_db,
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'admin_actions'",
        )
        == []
    )


# SVC-366 (ios app_versions row) and the revision it builds on (SVC-329).
SVC_366 = "e721a87d86e1"
BEFORE_SVC_366 = SVC_329


def _app_versions(url: URL) -> dict:
    """``{platform: row}`` for every app_versions row."""
    rows = _query(url, "SELECT * FROM app_versions")
    return {row["platform"]: dict(row) for row in rows}


def test_svc_366_seeds_ios_from_pwa_and_downgrade_removes_it(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_366)
    # A PWA bump before the deploy: ios must copy it, not the original seed.
    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE app_versions SET version = '1.0.4', min_supported_version = '1.0.2', "
                    "url = 'https://example.invalid/pwa', release_notes = 'pwa notes' "
                    "WHERE platform = 'pwa'"
                )
            )
    finally:
        engine.dispose()
    before = _app_versions(scratch_db)

    # Runs the real env.py, so the ADD VALUE inside autocommit_block is exercised
    # under the same outer transaction a deploy uses.
    _alembic(scratch_db, "upgrade", SVC_366)
    after = _app_versions(scratch_db)
    assert set(after) == {"android", "pwa", "ios"}
    ios = after["ios"]
    assert (ios["version"], ios["min_supported_version"]) == ("1.0.4", "1.0.2")
    assert ios["url"] is None
    assert ios["release_notes"] is None
    assert ios["released_at"] is not None
    # android + pwa untouched.
    assert {p: after[p] for p in ("android", "pwa")} == before

    _alembic(scratch_db, "downgrade", BEFORE_SVC_366)
    assert _app_versions(scratch_db) == before
    # Postgres can't drop an enum value, so 'ios' stays on the type.
    labels = {
        row["enumlabel"]
        for row in _query(
            scratch_db,
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'appplatform'",
        )
    }
    assert labels == {"android", "pwa", "ios"}

    # Re-upgrade with the enum value already present: IF NOT EXISTS + the data step.
    _alembic(scratch_db, "upgrade", SVC_366)
    again = _app_versions(scratch_db)
    assert set(again) == {"android", "pwa", "ios"}
    assert (again["ios"]["version"], again["ios"]["min_supported_version"]) == ("1.0.4", "1.0.2")


# --- SVC-400: services catalogue fields ------------------------------------------


SVC_400 = "314fe1318dfa"
BEFORE_SVC_400 = "1c0b3408bdb6"


def test_svc_400_adds_four_nullable_columns_and_downgrade_restores_services(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_400)
    before = _shape(scratch_db, "services")

    _alembic(scratch_db, "upgrade", SVC_400)
    upgraded = _shape(scratch_db, "services")
    # Only the four columns changed: no index, constraint or key.
    assert {k: v for k, v in upgraded.items() if k != "columns"} == {
        k: v for k, v in before.items() if k != "columns"
    }
    assert set(upgraded["columns"]) - set(before["columns"]) == {
        ("hero_image_url", "VARCHAR(512)", True),
        ("included_items", "JSONB", True),
        ("faqs", "JSONB", True),
        ("before_after_photos", "JSONB", True),
    }

    _alembic(scratch_db, "downgrade", BEFORE_SVC_400)
    assert _shape(scratch_db, "services") == before
