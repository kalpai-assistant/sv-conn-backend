"""
Migration round trips on a scratch database (SVC-319, SVC-339, SVC-340, SVC-320,
SVC-344, SVC-333, SVC-329, SVC-366, SVC-408, SVC-480, SVC-418, SVC-414).

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

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

from app.seed_data.service_details import (
    COMMON_FAQS,
    SERVICE_DETAILS,
    UPDATE_SERVICE_DETAILS_SVC_340,
    svc_340_details_params,
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
# and frozen UPDATE live in app.seed_data.service_details, which the migration
# imports.
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


def _alembic_result(url: URL, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": url.render_as_string(hide_password=False)}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _alembic(url: URL, *args: str) -> None:
    result = _alembic_result(url, *args)
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


def _svc_340_text(key: tuple[str, str]) -> tuple[list[str], list[str]]:
    """What SVC-340 wrote for a service: string steps (the titles) and exclusions."""
    details = SERVICE_DETAILS[key]
    return [step["title"] for step in details["steps"]], details["exclusions"]


def test_svc_340_seeds_matching_services_only_and_is_idempotent(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_340)
    ids = _seed_before_svc_340(scratch_db)

    _alembic(scratch_db, "upgrade", SVC_340)

    details = _service_details(scratch_db)
    assert details[ids["drain"]] == _svc_340_text(("plumber", "Drain blockage removal"))
    assert details[ids["haircut"]] == _svc_340_text(("salon-men", "Men's haircut"))
    assert details[ids["beard"]] == (None, None), "name not in the seed"
    assert details[ids["decoy"]] == (None, None), "seeded name, but in another category"

    # The migration's own statement, run a second time: same rows, same values.
    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            matched = sum(
                conn.execute(text(UPDATE_SERVICE_DETAILS_SVC_340), params).rowcount
                for params in svc_340_details_params()
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


# SVC-423 (audit log on admin_actions) and the revision it builds on (SVC-422).
SVC_423 = "c4e81d07a3f6"
BEFORE_SVC_423 = "a7c31e90d2b4"
TEAM_ACTION_NAMES = ("user_deactivated", "pro_force_removed", "pro_reassigned")


def _admin_action_checks(url: URL) -> dict:
    return {
        row["conname"]: row["def"]
        for row in _query(
            url,
            "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
            "WHERE conname LIKE 'ck_admin_actions_%'",
        )
    }


def test_svc_423_extends_admin_actions_and_downgrade_survives_the_new_rows(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_423)
    before_shape = _shape(scratch_db, "admin_actions")
    assert set(_admin_action_checks(scratch_db)) == {
        "ck_admin_actions_action",
        "ck_admin_actions_target_type",
    }
    admin_id, team_row, team_target = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, phone, name, role, is_active) "
                    "VALUES (:id, '+919000000002', 'An Admin', 'CONSUMER', true)"
                ),
                {"id": admin_id},
            )
            conn.execute(
                text(
                    "INSERT INTO admin_actions (id, admin_user_id, action, target_type, target_id) "
                    "VALUES (:id, :admin, 'pro_force_removed', 'professional', :target)"
                ),
                {"id": team_row, "admin": admin_id, "target": team_target},
            )
    finally:
        engine.dispose()

    _alembic(scratch_db, "upgrade", SVC_423)
    shape = _shape(scratch_db, "admin_actions")
    columns = {name: (type_, nullable) for name, type_, nullable in shape["columns"]}
    assert columns["target_id"] == ("VARCHAR(64)", False)
    assert columns["admin_user_id"][1] is False
    assert columns["before_json"][1] is True
    assert columns["after_json"][1] is True
    assert columns["ip_address"] == ("INET", True)
    assert columns["user_agent"] == ("VARCHAR(512)", True)
    assert _admin_action_checks(scratch_db) == {}
    assert ("ix_admin_actions_target", ("target_type", "target_id")) in {
        (name, cols) for name, cols, *_ in shape["indexes"]
    }
    index_def = _query(
        scratch_db,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_admin_actions_admin_created'",
    )[0]["indexdef"]
    assert "(admin_user_id, created_at DESC)" in index_def
    assert _query(scratch_db, "SELECT id, target_id FROM admin_actions") == [
        {"id": team_row, "target_id": str(team_target)}
    ]

    # What the old schema can't hold: a new action, a new target type, a text id.
    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO admin_actions "
                    "(id, admin_user_id, action, target_type, target_id, before_json, ip_address) "
                    "VALUES (:id, :admin, 'app_version.release', 'app_version', 'android', "
                    "'{}'::jsonb, '203.0.113.9')"
                ),
                {"id": uuid.uuid4(), "admin": admin_id},
            )
    finally:
        engine.dispose()

    _alembic(scratch_db, "downgrade", BEFORE_SVC_423)
    assert _shape(scratch_db, "admin_actions") == before_shape
    # The team row survives with its uuid; the row the old schema can't hold is gone.
    assert _query(scratch_db, "SELECT id, target_id FROM admin_actions") == [
        {"id": team_row, "target_id": team_target}
    ]
    checks = _admin_action_checks(scratch_db)
    for action in TEAM_ACTION_NAMES:
        assert f"'{action}'" in checks["ck_admin_actions_action"]
    for target_type in ("user", "professional"):
        assert f"'{target_type}'" in checks["ck_admin_actions_target_type"]

    # And it can come back up.
    _alembic(scratch_db, "upgrade", SVC_423)
    assert _admin_action_checks(scratch_db) == {}


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


# --- SVC-400: service content seed (steps, included items, FAQs) -----------------


SVC_400_CONTENT = "9486c2fc98b3"
BEFORE_SVC_400_CONTENT = SVC_400


def _service_content(url: URL) -> dict:
    """``{service_id: (steps, exclusions, included_items, faqs)}``."""
    rows = _query(url, "SELECT id, steps, exclusions, included_items, faqs FROM services")
    return {
        row["id"]: (row["steps"], row["exclusions"], row["included_items"], row["faqs"])
        for row in rows
    }


def test_svc_400_content_seeds_matching_services_and_downgrade_restores_svc_340(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_340)
    ids = _seed_before_svc_340(scratch_db)
    _alembic(scratch_db, "upgrade", BEFORE_SVC_400_CONTENT)
    before = _service_content(scratch_db)
    before_shape = _shape(scratch_db, "services")

    _alembic(scratch_db, "upgrade", SVC_400_CONTENT)

    content = _service_content(scratch_db)
    drain = SERVICE_DETAILS[("plumber", "Drain blockage removal")]
    assert content[ids["drain"]] == (
        drain["steps"],
        drain["exclusions"],
        drain["included_items"],
        drain["faqs"] + COMMON_FAQS,
    )
    assert content[ids["haircut"]][0][0] == {
        "title": "Professional arrives with sanitised tools and a fresh cape",
        "duration_minutes": 5,
        "icon": "sparkles",
    }
    assert content[ids["beard"]] == (None, None, None, None), "name not in the seed"
    assert content[ids["decoy"]] == (None, None, None, None), "another category"
    hero = _query(scratch_db, "SELECT hero_image_url, before_after_photos FROM services")
    assert all(r["hero_image_url"] is None and r["before_after_photos"] is None for r in hero)
    assert _shape(scratch_db, "services") == before_shape, "data only, no DDL"

    # Downgrade puts the SVC-340 text back exactly; upgrading again lands on
    # the same content.
    _alembic(scratch_db, "downgrade", BEFORE_SVC_400_CONTENT)
    assert _service_content(scratch_db) == before
    _alembic(scratch_db, "upgrade", SVC_400_CONTENT)
    assert _service_content(scratch_db) == content
    assert before[ids["drain"]] == (
        *_svc_340_text(("plumber", "Drain blockage removal")),
        None,
        None,
    )


# --- SVC-400 follow-up: step times for 3 services, painting's one-day total ------


SVC_400_STEP_TIMES = "efe67aee5402"
BEFORE_SVC_400_STEP_TIMES = "b3f1c7a9d388"
_NEWLY_TIMED = [
    ("ac", "Split AC foam-jet service"),
    ("pest-control", "Cockroach control"),
    ("appliances", "Washing machine repair"),
]
_PAINTING = ("painting", "Full room painting")
_DRAIN = ("plumber", "Drain blockage removal")


def _seed_before_svc_400_step_times(url: URL) -> dict:
    """The 5 services as beta had them after 9486c2fc98b3: the 3 newly timed
    ones and painting with untimed steps, everyone at the pricing seed's 60.
    Drain stands in for the services this revision must not change."""
    ids = {}
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for slug, name in [*_NEWLY_TIMED, _PAINTING, _DRAIN]:
                details = SERVICE_DETAILS[(slug, name)]
                steps = details["steps"]
                if (slug, name) in _NEWLY_TIMED:
                    steps = [
                        {k: v for k, v in step.items() if k != "duration_minutes"} for step in steps
                    ]
                cat_id, ids[(slug, name)] = uuid.uuid4(), uuid.uuid4()
                conn.execute(
                    text(
                        "INSERT INTO categories (id, name, slug, is_active, sort_order) "
                        "VALUES (:id, :name, :slug, true, 1)"
                    ),
                    {"id": cat_id, "name": f"Step times {slug}", "slug": slug},
                )
                conn.execute(
                    text(
                        "INSERT INTO services (id, category_id, name, base_price, "
                        "duration_minutes, is_active, steps, exclusions, included_items, faqs) "
                        "VALUES (:id, :category_id, :name, 99, 60, true, CAST(:steps AS jsonb), "
                        "CAST(:exclusions AS jsonb), CAST(:included_items AS jsonb), "
                        "CAST(:faqs AS jsonb))"
                    ),
                    {
                        "id": ids[(slug, name)],
                        "category_id": cat_id,
                        "name": name,
                        "steps": json.dumps(steps),
                        "exclusions": json.dumps(details["exclusions"]),
                        "included_items": json.dumps(details["included_items"]),
                        "faqs": json.dumps(details["faqs"] + COMMON_FAQS),
                    },
                )
    finally:
        engine.dispose()
    return ids


def _durations_and_content(url: URL) -> dict:
    rows = _query(
        url,
        "SELECT id, duration_minutes, steps, exclusions, included_items, faqs FROM services",
    )
    return {
        row["id"]: (
            row["duration_minutes"],
            row["steps"],
            row["exclusions"],
            row["included_items"],
            row["faqs"],
        )
        for row in rows
    }


def test_svc_400_step_times_and_painting_total_round_trip(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_400_STEP_TIMES)
    ids = _seed_before_svc_400_step_times(scratch_db)
    before = _durations_and_content(scratch_db)
    before_shape = _shape(scratch_db, "services")

    _alembic(scratch_db, "upgrade", SVC_400_STEP_TIMES)

    after = _durations_and_content(scratch_db)
    for key in _NEWLY_TIMED:
        duration, steps, *rest = after[ids[key]]
        assert duration == 60, key
        assert steps == SERVICE_DETAILS[key]["steps"], key
        assert sum(step["duration_minutes"] for step in steps) == 60, key
        assert [s["title"] for s in steps] == [s["title"] for s in before[ids[key]][1]], key
        assert rest == list(before[ids[key]][2:]), key
    duration, steps, *_ = after[ids[_PAINTING]]
    assert duration == 480
    assert all("duration_minutes" not in step for step in steps)
    assert after[ids[_PAINTING]][1:] == before[ids[_PAINTING]][1:], "only the total changed"
    assert after[ids[_DRAIN]] == before[ids[_DRAIN]], "other services unchanged"
    assert _shape(scratch_db, "services") == before_shape, "data only, no DDL"

    _alembic(scratch_db, "downgrade", BEFORE_SVC_400_STEP_TIMES)
    assert _durations_and_content(scratch_db) == before
    _alembic(scratch_db, "upgrade", SVC_400_STEP_TIMES)
    assert _durations_and_content(scratch_db) == after


# --- FIRST20 retired (soft delete) --------------------------------------------


RETIRE_FIRST20 = "ba56e901f1c4"
BEFORE_RETIRE_FIRST20 = "efe67aee5402"


def _seed_coupons(url: URL) -> None:
    """FIRST20 stored in mixed case (codes are matched on lower()), plus the
    two coupons that replace it, which this revision must not touch."""
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for code in ("First20", "EXPO25", "FIRST100"):
                conn.execute(
                    text(
                        "INSERT INTO coupons (id, code, discount_type, discount_value, "
                        "valid_until, usage_limit_per_user, is_active) VALUES (:id, :code, "
                        "'PERCENT', 20, now() + interval '30 days', 1, true)"
                    ),
                    {"id": uuid.uuid4(), "code": code},
                )
    finally:
        engine.dispose()


def _coupon_states(url: URL) -> dict:
    return {
        row["code"]: row["is_active"] for row in _query(url, "SELECT code, is_active FROM coupons")
    }


def test_retire_first20_coupon_round_trip(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_RETIRE_FIRST20)
    _seed_coupons(scratch_db)
    before_shape = _shape(scratch_db, "coupons")

    _alembic(scratch_db, "upgrade", RETIRE_FIRST20)

    # Soft delete: the row stays, only the kill switch flips.
    assert _coupon_states(scratch_db) == {"First20": False, "EXPO25": True, "FIRST100": True}
    assert _shape(scratch_db, "coupons") == before_shape, "data only, no DDL"

    _alembic(scratch_db, "downgrade", BEFORE_RETIRE_FIRST20)
    assert _coupon_states(scratch_db) == {"First20": True, "EXPO25": True, "FIRST100": True}


# --- SVC-428: category pricing columns + pricing_config ------------------------

SVC_428 = "d762e3df6aa5"
BEFORE_SVC_428 = "9d31d9715e0e"
SVC_428_COLUMNS = {
    ("is_sensitive", "BOOLEAN", False),
    ("reference_price", "NUMERIC(10, 2)", True),
    ("bounds_pct", "NUMERIC(5, 2)", True),
    ("platform_commission_pct", "NUMERIC(5, 2)", False),
    ("platform_commission_min", "NUMERIC(10, 2)", False),
    ("commission_gst_pct", "NUMERIC(5, 2)", False),
}


def _seed_before_svc_428(url: URL) -> None:
    """Three of the four sensitive slugs (no pest-control), plus one that isn't."""
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for slug in ("ac", "plumber", "electrician", "cleaning"):
                conn.execute(
                    text(
                        "INSERT INTO categories (id, name, slug, is_active, sort_order) "
                        "VALUES (:id, :name, :slug, true, 1)"
                    ),
                    {"id": uuid.uuid4(), "name": f"SVC-428 {slug}", "slug": slug},
                )
    finally:
        engine.dispose()


def test_svc_428_adds_pricing_columns_seeds_config_and_round_trips(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_428)
    _seed_before_svc_428(scratch_db)
    before = _shape(scratch_db, "categories")

    _alembic(scratch_db, "upgrade", SVC_428)
    upgraded = _shape(scratch_db, "categories")
    assert set(upgraded["columns"]) - set(before["columns"]) == SVC_428_COLUMNS

    pricing = {
        row["slug"]: dict(row)
        for row in _query(
            scratch_db,
            "SELECT slug, is_sensitive, bounds_pct, reference_price, platform_commission_pct, "
            "platform_commission_min, commission_gst_pct FROM categories",
        )
    }
    # pest-control isn't in this DB: skipped, not an error. The L1 tiles come from
    # an earlier migration and stay at the defaults.
    seeded = {"ac", "plumber", "electrician", "cleaning"}
    assert {
        slug: (r["is_sensitive"], r["bounds_pct"]) for slug, r in pricing.items() if slug in seeded
    } == {
        "ac": (True, 25),
        "plumber": (True, 20),
        "electrician": (True, 20),
        "cleaning": (False, None),
    }
    assert not [slug for slug, r in pricing.items() if slug not in seeded and r["is_sensitive"]]
    for row in pricing.values():
        assert row["reference_price"] is None
        assert (
            row["platform_commission_pct"],
            row["platform_commission_min"],
            row["commission_gst_pct"],
        ) == (20, 30, 18)

    config = {
        row["key"]: dict(row)
        for row in _query(scratch_db, "SELECT key, value_json, updated_by FROM pricing_config")
    }
    assert {key: row["value_json"] for key, row in config.items()} == {
        "ranking_weights": {
            "rating": 0.35,
            "price_vs_ref": 0.25,
            "distance": 0.20,
            "reliability": 0.15,
            "activity": 0.05,
        },
        "boost_window_days": 14,
        "boost_window_jobs": 10,
        "sheet_ttl_seconds": 600,
    }
    assert all(row["updated_by"] is None for row in config.values())

    # A sensitive row without bounds can't be saved.
    engine = create_engine(scratch_db)
    try:
        with pytest.raises(Exception, match="ck_categories_sensitive_needs_bounds"):
            with engine.begin() as conn:
                conn.execute(text("UPDATE categories SET bounds_pct = NULL WHERE slug = 'ac'"))
    finally:
        engine.dispose()

    _alembic(scratch_db, "downgrade", BEFORE_SVC_428)
    assert _shape(scratch_db, "categories") == before
    [table] = _query(scratch_db, "SELECT to_regclass('pricing_config') AS t")
    assert table["t"] is None
    assert not _query(
        scratch_db,
        "SELECT 1 FROM pg_constraint WHERE conname = 'ck_categories_sensitive_needs_bounds'",
    )


SVC_430 = "33122ac2373c"
BEFORE_SVC_430 = SVC_428
SVC_430_FILE = (
    REPO_ROOT
    / "alembic"
    / "versions"
    / "33122ac2373c_svc_430_catalog_icon_key_price_unit_min_qty.py"
)


def _svc_430_module():
    spec = importlib.util.spec_from_file_location("svc_430_migration", SVC_430_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_svc_430_frozen_checks_match_the_model():
    from app.models.price_unit import MIN_QTY_CHECK, PRICE_UNIT_CHECK

    module = _svc_430_module()
    assert (module.PRICE_UNIT_CHECK, module.MIN_QTY_CHECK) == (PRICE_UNIT_CHECK, MIN_QTY_CHECK)


def _seed_before_svc_430(url: URL) -> uuid.UUID:
    """One category and one service, written before the columns exist."""
    category_id, service_id = uuid.uuid4(), uuid.uuid4()
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO categories (id, name, slug, is_active, sort_order) "
                    "VALUES (:id, 'SVC-430 cat', 'svc-430-cat', true, 1)"
                ),
                {"id": category_id},
            )
            conn.execute(
                text(
                    "INSERT INTO services (id, category_id, name, base_price, duration_minutes, "
                    "is_active) VALUES (:id, :cat, 'SVC-430 svc', 499, 60, true)"
                ),
                {"id": service_id, "cat": category_id},
            )
    finally:
        engine.dispose()
    return service_id


def _refused_by(url: URL, sql: str, constraint: str) -> None:
    engine = create_engine(url)
    try:
        with pytest.raises(Exception, match=constraint):
            with engine.begin() as conn:
                conn.execute(text(sql))
    finally:
        engine.dispose()


def test_svc_430_adds_catalog_columns_backfills_and_round_trips(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_430)
    service_id = _seed_before_svc_430(scratch_db)
    before = {t: _shape(scratch_db, t) for t in ("categories", "services")}

    _alembic(scratch_db, "upgrade", SVC_430)
    after = {t: _shape(scratch_db, t) for t in ("categories", "services")}
    assert set(after["categories"]["columns"]) - set(before["categories"]["columns"]) == {
        ("icon_key", "VARCHAR(40)", True)
    }
    assert set(after["services"]["columns"]) - set(before["services"]["columns"]) == {
        ("icon_key", "VARCHAR(40)", True),
        ("price_unit", "VARCHAR(16)", False),
        ("min_qty", "INTEGER", True),
    }
    # The existing row is backfilled.
    [row] = _query(
        scratch_db,
        f"SELECT price_unit, min_qty, icon_key FROM services WHERE id = '{service_id}'",
    )
    assert dict(row) == {"price_unit": "PER_VISIT", "min_qty": None, "icon_key": None}

    where = f"WHERE id = '{service_id}'"
    _refused_by(
        scratch_db, f"UPDATE services SET price_unit = 'PER_DAY' {where}", "ck_services_price_unit"
    )
    _refused_by(
        scratch_db,
        f"UPDATE services SET price_unit = 'PER_SQFT' {where}",
        "ck_services_min_qty_unit",
    )
    _refused_by(
        scratch_db,
        f"UPDATE services SET price_unit = 'PER_HOUR', min_qty = 0 {where}",
        "ck_services_min_qty_unit",
    )
    _refused_by(scratch_db, f"UPDATE services SET min_qty = 3 {where}", "ck_services_min_qty_unit")
    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE services SET price_unit = 'PER_SQFT', min_qty = 5 {where}"))
    finally:
        engine.dispose()

    _alembic(scratch_db, "downgrade", BEFORE_SVC_430)
    assert {t: _shape(scratch_db, t) for t in ("categories", "services")} == before
    assert not _query(
        scratch_db,
        "SELECT 1 FROM pg_constraint WHERE conname IN "
        "('ck_services_price_unit', 'ck_services_min_qty_unit')",
    )


# --- SVC-432: analytics materialized views -------------------------------------

SVC_432 = "0319a6a0b99b"
BEFORE_SVC_432 = "84057c00390d"


def _matviews(url: URL) -> dict:
    """``{name: (definition, populated, [unique index defs])}`` for the analytics views."""
    from app.analytics.views import CURRENT_VIEWS

    names = [v.name for v in CURRENT_VIEWS]
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            views = conn.execute(
                text(
                    "SELECT matviewname, definition, ispopulated FROM pg_matviews "
                    "WHERE matviewname = ANY(:n)"
                ),
                {"n": names},
            ).all()
            indexes = conn.execute(
                text("SELECT tablename, indexdef FROM pg_indexes WHERE tablename = ANY(:n)"),
                {"n": names},
            ).all()
    finally:
        engine.dispose()
    return {
        name: (definition, populated, sorted(i.indexdef for i in indexes if i.tablename == name))
        for name, definition, populated in views
    }


def test_svc_432_views_round_trip_and_refresh_on_a_fresh_db(scratch_db):
    from unittest.mock import patch

    from sqlalchemy.orm import sessionmaker

    from app.analytics.views import CURRENT_VIEWS
    from app.tasks.analytics_tasks import refresh_analytics_views

    _alembic(scratch_db, "upgrade", BEFORE_SVC_432)
    assert _matviews(scratch_db) == {}

    _alembic(scratch_db, "upgrade", SVC_432)
    views = _matviews(scratch_db)
    assert set(views) == {v.name for v in CURRENT_VIEWS}
    for name, (_, populated, indexes) in views.items():
        # WITH DATA, and a unique index, or REFRESH ... CONCURRENTLY refuses.
        assert populated, name
        assert any(i.startswith("CREATE UNIQUE INDEX") for i in indexes), name

    # The real beat task on a DB that has only ever been migrated.
    engine = create_engine(scratch_db)
    try:
        with patch("app.database.SyncSessionLocal", sessionmaker(bind=engine)):
            refresh_analytics_views()
    finally:
        engine.dispose()
    [meta] = _query(scratch_db, "SELECT id, first_booking_at FROM mv_analytics_meta")
    assert dict(meta) == {"id": 1, "first_booking_at": None}

    _alembic(scratch_db, "downgrade", BEFORE_SVC_432)
    assert _matviews(scratch_db) == {}
    _alembic(scratch_db, "upgrade", SVC_432)
    assert set(_matviews(scratch_db)) == {v.name for v in CURRENT_VIEWS}


def test_svc_432_migrated_views_match_create_all(scratch_db):
    """The migration and the suite's create_all must build the same views."""
    import app.models  # noqa: F401  (registers the create_all hook)
    from app.database import Base

    _alembic(scratch_db, "upgrade", "head")
    test_db = make_url(TEST_DATABASE_URL).set(drivername="postgresql+psycopg2")
    engine = create_engine(test_db)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    migrated, created = _matviews(scratch_db), _matviews(test_db)
    assert migrated == created, (
        "The analytics views built by create_all differ from the migrated ones. "
        "If CURRENT_VIEWS changed, drop the analytics matviews in sc_test (the "
        "create_all hook uses IF NOT EXISTS and keeps an old definition), or use "
        "a fresh test DB."
    )


# --- SVC-433: support notes + bookings.dispatch_retried_at ---------------------------


SVC_433 = "66ca0b0a9c76"
BEFORE_SVC_433 = SVC_432


def _check_constraints(url: URL, table: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_check_constraints(table)}
    finally:
        engine.dispose()


def test_svc_433_adds_notes_and_the_retry_column_and_round_trips(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_433)
    bookings_before = _shape(scratch_db, "bookings")
    views_before = _matviews(scratch_db)

    _alembic(scratch_db, "upgrade", SVC_433)
    bookings_after = _shape(scratch_db, "bookings")
    assert set(bookings_after["columns"]) - set(bookings_before["columns"]) == {
        ("dispatch_retried_at", "TIMESTAMP", True),
    }
    assert {k: v for k, v in bookings_after.items() if k != "columns"} == {
        k: v for k, v in bookings_before.items() if k != "columns"
    }
    notes = _shape(scratch_db, "booking_support_notes")
    assert notes["primary_key"] == ["id"]
    assert sorted(fk[1:4] for fk in notes["foreign_keys"]) == [
        (("author_user_id",), "users", ("id",)),
        (("booking_id",), "bookings", ("id",)),
    ]
    assert (
        "ix_booking_support_notes_booking_id_created_at",
        ("booking_id", "created_at"),
        False,
        "None",
    ) in notes["indexes"]
    assert _check_constraints(scratch_db, "booking_support_notes") == {
        "ck_booking_support_notes_kind",
        "ck_booking_support_notes_note_has_body",
        "ck_booking_support_notes_body_length",
    }
    # A new bookings column leaves the SVC-432 analytics views as they were.
    assert _matviews(scratch_db) == views_before

    _alembic(scratch_db, "downgrade", BEFORE_SVC_433)
    assert _shape(scratch_db, "bookings") == bookings_before
    engine = create_engine(scratch_db)
    try:
        assert not inspect(engine).has_table("booking_support_notes")
    finally:
        engine.dispose()
    assert _matviews(scratch_db) == views_before


# --- SVC-408: sp_service_pricing, SP boost columns, bookings.locked_price ---------


SVC_408 = "ed0ba663c93c"
BEFORE_SVC_408 = SVC_433
SVC_408_FILE = (
    REPO_ROOT / "alembic" / "versions" / "ed0ba663c93c_svc_408_marketplace_pricing_data_model.py"
)


def test_svc_408_frozen_check_matches_the_model():
    from app.models.price_unit import PRICE_UNIT_CHECK

    spec = importlib.util.spec_from_file_location("svc_408_migration", SVC_408_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PRICE_UNIT_CHECK == PRICE_UNIT_CHECK


def _seed_before_svc_408(url: URL) -> dict:
    """A provider and two bookings, one with a coupon discount, before SVC-408."""
    ids = {key: uuid.uuid4() for key in ("consumer", "sp", "address", "plain", "discounted")}
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for key, role in (("consumer", "CONSUMER"), ("sp", "PROVIDER")):
                conn.execute(
                    text(
                        "INSERT INTO users (id, phone, role, is_active) "
                        "VALUES (:id, :phone, :role, true)"
                    ),
                    {"id": ids[key], "phone": f"+9{ids[key].int % 10**11:011d}", "role": role},
                )
            conn.execute(
                text(
                    "INSERT INTO provider_profiles (id, user_id, service_area_radius_km, "
                    "avg_rating, total_completed_jobs, kyc_status, is_online) "
                    "VALUES (:id, :user_id, 10, 4.5, 7, 'VERIFIED', true)"
                ),
                {"id": uuid.uuid4(), "user_id": ids["sp"]},
            )
            conn.execute(
                text(
                    "INSERT INTO addresses (id, user_id, label, full_address, is_default) "
                    "VALUES (:id, :user_id, 'Home', '1 Test Road', true)"
                ),
                {"id": ids["address"], "user_id": ids["consumer"]},
            )
            for key, amount, discount in (
                ("plain", "499.00", "0"),
                ("discounted", "1000.00", "200.00"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO bookings (id, consumer_id, provider_id, consumer_address_id, "
                        "status, service_amount, platform_commission, provider_payout, "
                        "payment_status, discount_amount, tip_amount) "
                        "VALUES (:id, :consumer, :sp, :address, 'COMPLETED', :amount, "
                        ":amount * 0.2, :amount * 0.8, 'PAID', :discount, 0)"
                    ),
                    {
                        "id": ids[key],
                        "consumer": ids["consumer"],
                        "sp": ids["sp"],
                        "address": ids["address"],
                        "amount": Decimal(amount),
                        "discount": Decimal(discount),
                    },
                )
    finally:
        engine.dispose()
    return ids


def test_svc_408_adds_pricing_schema_backfills_and_round_trips(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_408)
    ids = _seed_before_svc_408(scratch_db)
    tables = ("bookings", "provider_profiles")
    before = {t: _shape(scratch_db, t) for t in tables}
    checks_before = {t: _check_constraints(scratch_db, t) for t in tables}
    views_before = _matviews(scratch_db)

    _alembic(scratch_db, "upgrade", SVC_408)
    after = {t: _shape(scratch_db, t) for t in tables}
    assert set(after["bookings"]["columns"]) - set(before["bookings"]["columns"]) == {
        ("locked_price", "NUMERIC(12, 2)", False),
    }
    assert set(after["provider_profiles"]["columns"]) - set(
        before["provider_profiles"]["columns"]
    ) == {
        ("boost_eligible_at", "TIMESTAMP", True),
        ("boost_completed_jobs_count", "INTEGER", False),
    }
    for t in tables:
        assert {k: v for k, v in after[t].items() if k != "columns"} == {
            k: v for k, v in before[t].items() if k != "columns"
        }

    # Existing rows: locked_price is the pre-discount service_amount, not the net.
    locked = {
        row["id"]: (row["locked_price"], row["service_amount"])
        for row in _query(scratch_db, "SELECT id, locked_price, service_amount FROM bookings")
    }
    assert locked == {
        ids["plain"]: (Decimal("499.00"), Decimal("499.00")),
        ids["discounted"]: (Decimal("1000.00"), Decimal("1000.00")),
    }
    [sp] = _query(
        scratch_db, "SELECT boost_eligible_at, boost_completed_jobs_count FROM provider_profiles"
    )
    assert dict(sp) == {"boost_eligible_at": None, "boost_completed_jobs_count": 0}

    pricing = _shape(scratch_db, "sp_service_pricing")
    assert pricing["columns"] == sorted(
        [
            ("id", "UUID", False),
            ("sp_id", "UUID", False),
            ("service_id", "UUID", False),
            ("price_amount", "NUMERIC(12, 2)", False),
            ("price_unit", "VARCHAR(16)", False),
            ("is_active", "BOOLEAN", False),
            ("created_at", "TIMESTAMP", False),
            ("updated_at", "TIMESTAMP", False),
        ]
    )
    assert pricing["primary_key"] == ["id"]
    assert pricing["foreign_keys"] == sorted(
        [
            (
                "fk_sp_service_pricing_service_id",
                ("service_id",),
                "services",
                ("id",),
                "RESTRICT",
            ),
            (
                "fk_sp_service_pricing_sp_id",
                ("sp_id",),
                "provider_profiles",
                ("user_id",),
                "RESTRICT",
            ),
        ]
    )
    assert pricing["unique_constraints"] == [
        ("uq_sp_service_pricing_sp_service", ("sp_id", "service_id"))
    ]
    assert (
        "ix_sp_service_pricing_service_active",
        ("service_id", "is_active"),
        False,
        "None",
    ) in pricing["indexes"]
    assert _check_constraints(scratch_db, "sp_service_pricing") == {
        "ck_sp_service_pricing_price_unit"
    }
    # A booking written without locked_price is refused, not defaulted.
    _refused_by(
        scratch_db,
        f"INSERT INTO bookings (id, consumer_id, consumer_address_id, status, service_amount, "
        f"platform_commission, provider_payout, payment_status, discount_amount, tip_amount) "
        f"VALUES ('{uuid.uuid4()}', '{ids['consumer']}', '{ids['address']}', 'REQUESTED', "
        f"100, 20, 80, 'PENDING', 0, 0)",
        "locked_price",
    )
    # New columns leave the SVC-432 analytics views as they were.
    assert _matviews(scratch_db) == views_before

    _alembic(scratch_db, "downgrade", BEFORE_SVC_408)
    assert {t: _shape(scratch_db, t) for t in tables} == before
    assert {t: _check_constraints(scratch_db, t) for t in tables} == checks_before
    engine = create_engine(scratch_db)
    try:
        assert not inspect(engine).has_table("sp_service_pricing")
    finally:
        engine.dispose()
    assert _matviews(scratch_db) == views_before


# --- SVC-479: bookings.chosen_sp_id ------------------------------------------------


SVC_479 = "d9bbf36bb62a"
BEFORE_SVC_479 = SVC_408


def test_svc_479_adds_chosen_sp_id_and_round_trips(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_408)
    ids = _seed_before_svc_408(scratch_db)
    _alembic(scratch_db, "upgrade", BEFORE_SVC_479)
    before = _shape(scratch_db, "bookings")
    views_before = _matviews(scratch_db)

    _alembic(scratch_db, "upgrade", SVC_479)
    after = _shape(scratch_db, "bookings")
    assert ("chosen_sp_id", "UUID", True) in after["columns"]
    assert (
        "fk_bookings_chosen_sp_id",
        ("chosen_sp_id",),
        "provider_profiles",
        ("user_id",),
        "RESTRICT",
    ) in after["foreign_keys"]
    assert (
        "ix_bookings_chosen_sp_id",
        ("chosen_sp_id",),
        False,
        "(chosen_sp_id IS NOT NULL)",
    ) in after["indexes"]
    # Existing bookings stay the open broadcast: no backfill.
    rows = _query(scratch_db, "SELECT chosen_sp_id FROM bookings")
    assert rows and all(row["chosen_sp_id"] is None for row in rows)

    # Only an SP can be chosen, and a chosen SP can't be hard-deleted.
    _refused_by(
        scratch_db,
        f"UPDATE bookings SET chosen_sp_id = '{ids['consumer']}' WHERE id = '{ids['plain']}'",
        "fk_bookings_chosen_sp_id",
    )
    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE bookings SET chosen_sp_id = :sp WHERE id = :b"),
                {"sp": ids["sp"], "b": ids["plain"]},
            )
    finally:
        engine.dispose()
    _refused_by(
        scratch_db,
        f"DELETE FROM provider_profiles WHERE user_id = '{ids['sp']}'",
        "fk_bookings_chosen_sp_id",
    )
    assert _matviews(scratch_db) == views_before

    # Down with a chosen SP still set: the column just goes.
    _alembic(scratch_db, "downgrade", BEFORE_SVC_479)
    assert _shape(scratch_db, "bookings") == before
    assert _matviews(scratch_db) == views_before


# --- SVC-480: sp_service_pricing per service variant ------------------------------


SVC_480 = "77ae2d00d567"
BEFORE_SVC_480 = SVC_479

_SVC_480_SERVICES = {
    # key: (base_price, duration, [(variant key, sort_order, is_active)])
    "svc": ("2499.00", 60, [("v1", 0, True), ("v2", 1, True)]),
    "other_svc": ("499.00", 60, [("other_v", 0, True)]),
    "inactive_only": ("799.00", 60, [("off_v", 0, False)]),
    "bare_whole": ("499.00", 45, []),
    "bare_odd": ("499.99", 30, []),
}


def _seed_svc_480_catalog(url: URL) -> dict:
    """An SP and five services: two with active variants, one whose only variant
    is off, and two with no variant row at all (admin-created ones)."""
    ids = {"sp": uuid.uuid4(), "cat": uuid.uuid4()}
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, phone, role, is_active) "
                    "VALUES (:id, :phone, 'PROVIDER', true)"
                ),
                {"id": ids["sp"], "phone": f"+9{ids['sp'].int % 10**11:011d}"},
            )
            conn.execute(
                text(
                    "INSERT INTO provider_profiles (id, user_id, service_area_radius_km, "
                    "avg_rating, total_completed_jobs, kyc_status, is_online) "
                    "VALUES (:id, :user_id, 10, 4.5, 7, 'VERIFIED', true)"
                ),
                {"id": uuid.uuid4(), "user_id": ids["sp"]},
            )
            conn.execute(
                text(
                    "INSERT INTO categories (id, name, slug, is_active, sort_order) "
                    "VALUES (:id, 'SVC-480 cat', 'svc-480-cat', true, 1)"
                ),
                {"id": ids["cat"]},
            )
            for key, (price, duration, variants) in _SVC_480_SERVICES.items():
                ids[key] = uuid.uuid4()
                conn.execute(
                    text(
                        "INSERT INTO services (id, category_id, name, base_price, "
                        "duration_minutes, is_active) "
                        "VALUES (:id, :cat, :name, :price, :duration, true)"
                    ),
                    {
                        "id": ids[key],
                        "cat": ids["cat"],
                        "name": key,
                        "price": Decimal(price),
                        "duration": duration,
                    },
                )
                for v_key, order, active in variants:
                    ids[v_key] = uuid.uuid4()
                    conn.execute(
                        text(
                            "INSERT INTO service_variants (id, service_id, label, base_price, "
                            "provider_payout, platform_commission, sort_order, is_active) "
                            "VALUES (:id, :svc, :label, :price, :price * 0.7, :price * 0.3, "
                            ":order, :active)"
                        ),
                        {
                            "id": ids[v_key],
                            "svc": ids[key],
                            "label": v_key,
                            "price": Decimal(price),
                            "order": order,
                            "active": active,
                        },
                    )
    finally:
        engine.dispose()
    return ids


def _price_sql(sp, service, variant=None) -> str:
    """A price row. Without a variant it names no variant column, so it also
    works before SVC-480."""
    if variant is None:
        return (
            "INSERT INTO sp_service_pricing (id, sp_id, service_id, price_amount, price_unit) "
            f"VALUES ('{uuid.uuid4()}', '{sp}', '{service}', 2299, 'PER_VISIT')"
        )
    return (
        "INSERT INTO sp_service_pricing (id, sp_id, service_id, service_variant_id, "
        f"price_amount, price_unit) VALUES ('{uuid.uuid4()}', '{sp}', '{service}', "
        f"'{variant}', 2299, 'PER_VISIT')"
    )


def _insert_price(url: URL, sp, service, variant=None) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text(_price_sql(sp, service, variant)))
    finally:
        engine.dispose()


def _variants_of(url: URL, service_id) -> list[dict]:
    return [
        dict(row)
        for row in _query(
            url,
            "SELECT id, label, subcategory, base_price, platform_commission, provider_payout, "
            "gst_percent, duration_minutes, sort_order, is_active FROM service_variants "
            f"WHERE service_id = '{service_id}' ORDER BY sort_order, id",
        )
    ]


def test_svc_480_prices_per_variant_and_round_trips(scratch_db):
    from app.config import get_settings
    from app.utils.money import to_decimal

    _alembic(scratch_db, "upgrade", BEFORE_SVC_480)
    ids = _seed_svc_480_catalog(scratch_db)
    tables = ("sp_service_pricing", "service_variants")
    before = {t: _shape(scratch_db, t) for t in tables}
    variants_before = {
        key: _variants_of(scratch_db, ids[key]) for key in ("svc", "other_svc", "inactive_only")
    }
    # SVC-408 rows, one per (SP, service): each gets its service's first variant.
    for key in ("svc", "other_svc", "inactive_only"):
        _insert_price(scratch_db, ids["sp"], ids[key])
    views_before = _matviews(scratch_db)

    _alembic(scratch_db, "upgrade", SVC_480)

    # A Standard variant for the two services with none, split like
    # create_booking's legacy branch at this deployment's percent.
    rate = to_decimal(get_settings().PLATFORM_COMMISSION_PERCENT) / 100
    for key in ("bare_whole", "bare_odd"):
        price, duration, _ = _SVC_480_SERVICES[key]
        price = Decimal(price)
        commission = to_decimal(price * rate)
        [standard] = _variants_of(scratch_db, ids[key])
        assert {k: v for k, v in standard.items() if k != "id"} == {
            "label": "Standard",
            "subcategory": None,
            "base_price": price,
            "platform_commission": commission,
            "provider_payout": price - commission,
            "gst_percent": Decimal("0.00"),
            "duration_minutes": duration,
            "sort_order": 0,
            "is_active": True,
        }, key
    # 499.99 × 20% = 99.998: rounded half up to 100.00, as to_decimal does.
    if rate == Decimal("0.2"):
        assert _variants_of(scratch_db, ids["bare_odd"])[0]["platform_commission"] == Decimal(
            "100.00"
        )
    # Services with a variant row, even an inactive one, are untouched.
    for key, rows in variants_before.items():
        assert _variants_of(scratch_db, ids[key]) == rows, key

    pricing = _shape(scratch_db, "sp_service_pricing")
    assert set(pricing["columns"]) - set(before["sp_service_pricing"]["columns"]) == {
        ("service_variant_id", "UUID", False)
    }
    assert (
        "fk_sp_service_pricing_variant",
        ("service_variant_id", "service_id"),
        "service_variants",
        ("id", "service_id"),
        "RESTRICT",
    ) in pricing["foreign_keys"]
    assert pricing["unique_constraints"] == [
        ("uq_sp_service_pricing_sp_variant", ("sp_id", "service_variant_id"))
    ]
    assert set(pricing["indexes"]) - set(before["sp_service_pricing"]["indexes"]) == {
        (
            "ix_sp_service_pricing_variant_active",
            ("service_variant_id", "is_active"),
            False,
            "None",
        ),
        # Postgres backs the new unique with an index of its own name.
        (
            "uq_sp_service_pricing_sp_variant",
            ("sp_id", "service_variant_id"),
            True,
            "None",
        ),
    }
    assert ("uq_service_variants_id_service_id", ("id", "service_id")) in _shape(
        scratch_db, "service_variants"
    )["unique_constraints"]
    mapped = {
        row["service_id"]: row["service_variant_id"]
        for row in _query(
            scratch_db, "SELECT service_id, service_variant_id FROM sp_service_pricing"
        )
    }
    # First variant by sort_order; the only (inactive) one when nothing is on.
    assert mapped == {
        ids["svc"]: ids["v1"],
        ids["other_svc"]: ids["other_v"],
        ids["inactive_only"]: ids["off_v"],
    }

    # Every price names a variant, one per (SP, variant).
    _refused_by(scratch_db, _price_sql(ids["sp"], ids["bare_whole"]), "service_variant_id")
    _refused_by(
        scratch_db,
        _price_sql(ids["sp"], ids["svc"], ids["v1"]),
        "uq_sp_service_pricing_sp_variant",
    )
    _insert_price(scratch_db, ids["sp"], ids["svc"], ids["v2"])  # another variant: fine
    # Not another service's variant (one this SP hasn't priced), nor an unknown one.
    foreign = _variants_of(scratch_db, ids["bare_whole"])[0]["id"]
    _refused_by(
        scratch_db,
        _price_sql(ids["sp"], ids["svc"], foreign),
        "fk_sp_service_pricing_variant",
    )
    _refused_by(
        scratch_db,
        _price_sql(ids["sp"], ids["svc"], uuid.uuid4()),
        "fk_sp_service_pricing_variant",
    )
    # A priced variant can't be hard-deleted.
    _refused_by(
        scratch_db,
        f"DELETE FROM service_variants WHERE id = '{ids['v1']}'",
        "fk_sp_service_pricing_variant",
    )
    assert _matviews(scratch_db) == views_before

    # Down: refused while an SP has more than one row for a service, and the
    # schema is left at SVC-480.
    result = _alembic_result(scratch_db, "downgrade", BEFORE_SVC_480)
    assert result.returncode != 0
    assert "SVC-480 downgrade refused: 1 (sp_id, service_id) pair(s)" in result.stderr
    assert f"service_id={ids['svc']}" in result.stderr
    [version] = _query(scratch_db, "SELECT version_num FROM alembic_version")
    assert version["version_num"] == SVC_480
    assert _shape(scratch_db, "sp_service_pricing") == pricing

    # With one row per (SP, service) left, down restores the SVC-408 shape and
    # keeps the Standard variants.
    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM sp_service_pricing WHERE service_variant_id = :v"),
                {"v": ids["v2"]},
            )
    finally:
        engine.dispose()
    standards = {key: _variants_of(scratch_db, ids[key]) for key in ("bare_whole", "bare_odd")}
    _alembic(scratch_db, "downgrade", BEFORE_SVC_480)
    assert {t: _shape(scratch_db, t) for t in tables} == before
    assert {key: _variants_of(scratch_db, ids[key]) for key in standards} == standards
    [count] = _query(scratch_db, "SELECT count(*) AS n FROM sp_service_pricing")
    assert count["n"] == 3
    _refused_by(
        scratch_db,
        _price_sql(ids["sp"], ids["svc"]),
        "uq_sp_service_pricing_sp_service",
    )
    assert _matviews(scratch_db) == views_before

    # Up again: the same shape, and no second Standard variant.
    _alembic(scratch_db, "upgrade", SVC_480)
    assert _shape(scratch_db, "sp_service_pricing") == pricing
    assert {key: _variants_of(scratch_db, ids[key]) for key in standards} == standards


def test_seed_wipe_clears_sp_prices_first(scratch_db):
    """``seed_pricing_from_xlsx.py`` wipes the catalog; a price row (RESTRICT on
    both its service and its variant) must not make that fail."""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from scripts.seed_pricing_from_xlsx import wipe_services_and_variants

    _alembic(scratch_db, "upgrade", SVC_480)
    ids = _seed_svc_480_catalog(scratch_db)
    _insert_price(scratch_db, ids["sp"], ids["svc"], ids["v1"])
    _insert_price(scratch_db, ids["sp"], ids["other_svc"], ids["other_v"])
    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO provider_services (id, provider_id, service_id) "
                    "VALUES (:id, :sp, :svc)"
                ),
                {"id": uuid.uuid4(), "sp": ids["sp"], "svc": ids["svc"]},
            )
    finally:
        engine.dispose()

    async def wipe() -> None:
        async_engine = create_async_engine(
            scratch_db.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
        )
        try:
            async with AsyncSession(async_engine) as db:
                await wipe_services_and_variants(db)
                await db.commit()
        finally:
            await async_engine.dispose()

    asyncio.run(wipe())

    for table in ("sp_service_pricing", "provider_services", "service_variants", "services"):
        [count] = _query(scratch_db, f"SELECT count(*) AS n FROM {table}")
        assert count["n"] == 0, table
    # Categories and the SP stay.
    [cat] = _query(scratch_db, f"SELECT count(*) AS n FROM categories WHERE id = '{ids['cat']}'")
    [sps] = _query(scratch_db, "SELECT count(*) AS n FROM provider_profiles")
    assert (cat["n"], sps["n"]) == (1, 1)


# --- SVC-418: default SP price per variant (data migration) ------------------------


SVC_418 = "471bb682ac1b"
BEFORE_SVC_418 = SVC_480

# key: (is_active, price_unit, [(variant key, base_price, is_active)])
_SVC_418_SERVICES = {
    "multi": (True, "PER_VISIT", [("m1", "2499.00", True), ("m2", "3999.00", True)]),
    "multi_off_variant": (True, "PER_VISIT", [("m_off", "1999.00", False)]),
    "per_item": (True, "PER_ITEM", [("u1", "149.50", True)]),
    "off_service": (False, "PER_VISIT", [("o1", "999.00", True)]),
    "unsubscribed": (True, "PER_VISIT", [("n1", "599.00", True)]),
}
# Who subscribes to what. sp_c subscribes to nothing.
_SVC_418_SUBSCRIPTIONS = {
    "sp_a": ("multi", "multi_off_variant", "per_item", "off_service"),
    "sp_b": ("multi",),
}


def _engine_run(url: URL, sql: str, params: dict | None = None) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text(sql), params or {})
    finally:
        engine.dispose()


def _seed_svc_418(url: URL) -> dict:
    """Three SPs, a category and five services, at SVC-480. Of the subscriptions,
    a default price is due for sp_a × (m1, m2, u1) and sp_b × (m1, m2): not for an
    inactive variant, an inactive service, or a service nobody subscribes to."""
    ids = {"cat": uuid.uuid4()}
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for sp in ("sp_a", "sp_b", "sp_c"):
                ids[sp] = uuid.uuid4()
                conn.execute(
                    text(
                        "INSERT INTO users (id, phone, role, is_active) "
                        "VALUES (:id, :phone, 'PROVIDER', true)"
                    ),
                    {"id": ids[sp], "phone": f"+9{ids[sp].int % 10**11:011d}"},
                )
                conn.execute(
                    text(
                        "INSERT INTO provider_profiles (id, user_id, service_area_radius_km, "
                        "avg_rating, total_completed_jobs, kyc_status, is_online) "
                        "VALUES (:id, :user_id, 10, 4.5, 7, 'VERIFIED', true)"
                    ),
                    {"id": uuid.uuid4(), "user_id": ids[sp]},
                )
            conn.execute(
                text(
                    "INSERT INTO categories (id, name, slug, is_active, sort_order) "
                    "VALUES (:id, 'SVC-418 cat', 'svc-418-cat', true, 1)"
                ),
                {"id": ids["cat"]},
            )
            for key, (active, unit, variants) in _SVC_418_SERVICES.items():
                ids[key] = uuid.uuid4()
                conn.execute(
                    text(
                        "INSERT INTO services (id, category_id, name, base_price, "
                        "duration_minutes, is_active, price_unit) "
                        "VALUES (:id, :cat, :name, 100, 60, :active, :unit)"
                    ),
                    {
                        "id": ids[key],
                        "cat": ids["cat"],
                        "name": key,
                        "active": active,
                        "unit": unit,
                    },
                )
                for order, (v_key, price, v_active) in enumerate(variants):
                    ids[v_key] = uuid.uuid4()
                    conn.execute(
                        text(
                            "INSERT INTO service_variants (id, service_id, label, base_price, "
                            "provider_payout, platform_commission, sort_order, is_active) "
                            "VALUES (:id, :svc, :label, :price, :price * 0.8, :price * 0.2, "
                            ":order, :active)"
                        ),
                        {
                            "id": ids[v_key],
                            "svc": ids[key],
                            "label": v_key,
                            "price": Decimal(price),
                            "order": order,
                            "active": v_active,
                        },
                    )
            for sp, services in _SVC_418_SUBSCRIPTIONS.items():
                for key in services:
                    conn.execute(
                        text(
                            "INSERT INTO provider_services (id, provider_id, service_id) "
                            "VALUES (:id, :sp, :svc)"
                        ),
                        {"id": uuid.uuid4(), "sp": ids[sp], "svc": ids[key]},
                    )
    finally:
        engine.dispose()
    return ids


def _svc_418_id(sp, variant) -> uuid.UUID:
    """The id the backfill derives for (sp, variant), computed independently."""
    return uuid.UUID(hashlib.md5(f"svc-418:{sp}:{variant}".encode()).hexdigest())


def _prices(url: URL) -> dict:
    """``{(sp_id, variant_id): row}`` for every price row."""
    return {
        (row["sp_id"], row["service_variant_id"]): dict(row)
        for row in _query(
            url,
            "SELECT id, sp_id, service_id, service_variant_id, price_amount, price_unit, "
            "is_active, created_at, updated_at FROM sp_service_pricing",
        )
    }


def _dry_run(url: URL) -> dict:
    from scripts.dry_run_default_sp_prices import collect

    return collect(url.render_as_string(hide_password=False))


def _sp_price_row(url: URL, sp, service, variant, price: str) -> uuid.UUID:
    """A price the SP set itself: a random id, like the app's."""
    row_id = uuid.uuid4()
    _engine_run(
        url,
        "INSERT INTO sp_service_pricing (id, sp_id, service_id, service_variant_id, "
        "price_amount, price_unit) VALUES (:id, :sp, :svc, :v, :price, 'PER_VISIT')",
        {"id": row_id, "sp": sp, "svc": service, "v": variant, "price": Decimal(price)},
    )
    return row_id


def test_svc_418_backfills_one_price_per_active_variant_and_is_idempotent(scratch_db):
    from app.seed_data.svc_418_default_prices import backfill_default_prices

    _alembic(scratch_db, "upgrade", BEFORE_SVC_418)
    ids = _seed_svc_418(scratch_db)
    # sp_b already priced m1 itself, below the platform price.
    own_id = _sp_price_row(scratch_db, ids["sp_b"], ids["multi"], ids["m1"], "2299.00")
    own_before = _prices(scratch_db)
    shape_before = _shape(scratch_db, "sp_service_pricing")
    views_before = _matviews(scratch_db)

    counts = _dry_run(scratch_db)
    assert counts == {
        "subscriptions": 5,
        "active_subscriptions": 4,  # not sp_a's off_service
        "sps": 2,
        "candidates": 5,  # sp_a × m1, m2, u1; sp_b × m1, m2
        "already_present": 1,
        "to_insert": 4,
    }
    # The dry run wrote nothing.
    assert _prices(scratch_db) == own_before

    _alembic(scratch_db, "upgrade", SVC_418)

    prices = _prices(scratch_db)
    expected = {
        ("sp_a", "multi", "m1", "2499.00", "PER_VISIT"),
        ("sp_a", "multi", "m2", "3999.00", "PER_VISIT"),
        ("sp_a", "per_item", "u1", "149.50", "PER_ITEM"),
        ("sp_b", "multi", "m2", "3999.00", "PER_VISIT"),
    }
    new = {key: row for key, row in prices.items() if key not in own_before}
    assert len(new) == counts["to_insert"]
    assert set(new) == {(ids[sp], ids[v]) for sp, _, v, _, _ in expected}
    for sp, service, variant, price, unit in expected:
        row = new[(ids[sp], ids[variant])]
        assert row["id"] == _svc_418_id(ids[sp], ids[variant])
        assert row["service_id"] == ids[service]
        assert (row["price_amount"], row["price_unit"], row["is_active"]) == (
            Decimal(price),
            unit,
            True,
        )
        assert row["created_at"] == row["updated_at"]
    # sp_b's own price is untouched, at its own id and amount.
    assert prices[(ids["sp_b"], ids["m1"])] == own_before[(ids["sp_b"], ids["m1"])]
    assert prices[(ids["sp_b"], ids["m1"])]["id"] == own_id
    # No row for the inactive variant, the inactive service, nor the unsubscribed one.
    priced_variants = {variant for _, variant in prices}
    assert not priced_variants & {ids["m_off"], ids["o1"], ids["n1"]}
    assert not any(sp == ids["sp_c"] for sp, _ in prices)
    # A data migration: the table and the analytics views are unchanged.
    assert _shape(scratch_db, "sp_service_pricing") == shape_before
    assert _matviews(scratch_db) == views_before

    # Nothing left to do, and running the insert again adds nothing.
    assert _dry_run(scratch_db)["to_insert"] == 0
    engine = create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            assert backfill_default_prices(conn) == 0
    finally:
        engine.dispose()
    assert _prices(scratch_db) == prices


def test_svc_418_downgrade_removes_only_its_unedited_rows(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_418)
    ids = _seed_svc_418(scratch_db)
    own_id = _sp_price_row(scratch_db, ids["sp_b"], ids["multi"], ids["m1"], "2299.00")
    _alembic(scratch_db, "upgrade", SVC_418)
    backfilled = _prices(scratch_db)

    # sp_a edits its m1 default the way SVC-412's PATCH will: a later updated_at.
    _engine_run(
        scratch_db,
        "UPDATE sp_service_pricing SET price_amount = 2799, updated_at = now() "
        "WHERE sp_id = :sp AND service_variant_id = :v",
        {"sp": ids["sp_a"], "v": ids["m1"]},
    )
    # After the upgrade, a new variant appears and sp_a prices it itself.
    ids["m3"] = uuid.uuid4()
    _engine_run(
        scratch_db,
        "INSERT INTO service_variants (id, service_id, label, base_price, provider_payout, "
        "platform_commission, sort_order, is_active) "
        "VALUES (:id, :svc, 'm3', 4999, 3999.20, 999.80, 2, true)",
        {"id": ids["m3"], "svc": ids["multi"]},
    )
    later_id = _sp_price_row(scratch_db, ids["sp_a"], ids["multi"], ids["m3"], "4499.00")
    keep = {
        (ids["sp_a"], ids["m1"]),  # backfilled, then edited
        (ids["sp_a"], ids["m3"]),  # created after the upgrade
        (ids["sp_b"], ids["m1"]),  # there before the upgrade
    }
    kept_before = {key: row for key, row in _prices(scratch_db).items() if key in keep}
    assert len(kept_before) == 3

    _alembic(scratch_db, "downgrade", BEFORE_SVC_418)

    assert _prices(scratch_db) == kept_before
    edited = kept_before[(ids["sp_a"], ids["m1"])]
    assert edited["id"] == _svc_418_id(ids["sp_a"], ids["m1"])  # still the derived id
    assert edited["price_amount"] == Decimal("2799.00")
    assert edited["updated_at"] > edited["created_at"]
    assert kept_before[(ids["sp_a"], ids["m3"])]["id"] == later_id
    assert kept_before[(ids["sp_b"], ids["m1"])]["id"] == own_id

    # Up again: the unedited defaults come back. The edited row, which conflicts
    # on both its id and (sp_id, service_variant_id), is skipped without error.
    _alembic(scratch_db, "upgrade", SVC_418)
    again = _prices(scratch_db)
    assert again[(ids["sp_a"], ids["m1"])] == edited
    # sp_b subscribes to "multi" too, so a re-run also defaults the m3 that was
    # added in between; sp_a's own m3 price is kept.
    assert set(again) == set(backfilled) | {(ids["sp_a"], ids["m3"]), (ids["sp_b"], ids["m3"])}
    assert again[(ids["sp_a"], ids["m3"])] == kept_before[(ids["sp_a"], ids["m3"])]
    sp_b_m3 = again[(ids["sp_b"], ids["m3"])]
    assert (sp_b_m3["id"], sp_b_m3["price_amount"]) == (
        _svc_418_id(ids["sp_b"], ids["m3"]),
        Decimal("4999.00"),
    )
    stamps = ("created_at", "updated_at")
    for key in set(backfilled) - keep:
        restored = {k: v for k, v in again[key].items() if k not in stamps}
        original = {k: v for k, v in backfilled[key].items() if k not in stamps}
        assert restored == original


# --- SVC-414: start the new-SP boost for already-approved SPs (data migration) ------


SVC_414 = "ac2f13dad2a1"
BEFORE_SVC_414 = SVC_418

_T_REVIEWED = "2026-11-10 09:30:00+00"
_T_BOOSTED = "2026-10-01 08:00:00+00"

# key: (kyc_status, kyc_reviewed_at, total_completed_jobs, boost_eligible_at, boost count)
_SVC_414_SPS = {
    "approved": ("VERIFIED", _T_REVIEWED, 4, None, 0),
    "no_review_time": ("VERIFIED", None, 2, None, 0),
    "pending": ("PENDING", None, 0, None, 0),
    "submitted": ("SUBMITTED", None, 0, None, 0),
    "rejected": ("REJECTED", _T_REVIEWED, 0, None, 0),
    "already_boosted": ("VERIFIED", _T_REVIEWED, 9, _T_BOOSTED, 2),
}


def _seed_svc_414(url: URL) -> dict:
    ids = {}
    for key, (kyc, reviewed, total, boosted, count) in _SVC_414_SPS.items():
        ids[key] = uuid.uuid4()
        _engine_run(
            url,
            "INSERT INTO users (id, phone, role, is_active) VALUES (:id, :phone, 'PROVIDER', true)",
            {"id": ids[key], "phone": f"+9{ids[key].int % 10**11:011d}"},
        )
        _engine_run(
            url,
            "INSERT INTO provider_profiles (id, user_id, service_area_radius_km, avg_rating, "
            "total_completed_jobs, kyc_status, kyc_reviewed_at, is_online, boost_eligible_at, "
            "boost_completed_jobs_count) "
            "VALUES (:id, :user_id, 10, 4.5, :total, :kyc, :reviewed, true, :boosted, :count)",
            {
                "id": uuid.uuid4(),
                "user_id": ids[key],
                "total": total,
                "kyc": kyc,
                "reviewed": reviewed,
                "boosted": boosted,
                "count": count,
            },
        )
    return ids


def _boost_state(url: URL, ids: dict) -> dict:
    by_user = {
        row["user_id"]: dict(row)
        for row in _query(
            url,
            "SELECT user_id, kyc_status, kyc_reviewed_at, total_completed_jobs, "
            "boost_eligible_at, boost_completed_jobs_count FROM provider_profiles",
        )
    }
    return {key: by_user[user_id] for key, user_id in ids.items()}


def test_svc_414_backfills_verified_sps_only_and_is_idempotent(scratch_db):
    _alembic(scratch_db, "upgrade", BEFORE_SVC_414)
    ids = _seed_svc_414(scratch_db)
    before = _boost_state(scratch_db, ids)
    shape_before = _shape(scratch_db, "provider_profiles")

    result = _alembic_result(scratch_db, "upgrade", SVC_414)

    assert result.returncode == 0, result.stderr
    assert "1 VERIFIED SPs started at kyc_reviewed_at, 1 skipped" in result.stderr
    after = _boost_state(scratch_db, ids)
    approved = after["approved"]
    # Started at its approval time, counting the jobs it has already done.
    assert approved["boost_eligible_at"] == approved["kyc_reviewed_at"]
    assert approved["boost_completed_jobs_count"] == 4
    assert {k: v for k, v in approved.items() if not k.startswith("boost_")} == {
        k: v for k, v in before["approved"].items() if not k.startswith("boost_")
    }
    # No approval time to start from, not VERIFIED, or already started: untouched.
    for key in ("no_review_time", "pending", "submitted", "rejected", "already_boosted"):
        assert after[key] == before[key], key
    assert after["already_boosted"]["boost_completed_jobs_count"] == 2
    # A data migration: the table is unchanged.
    assert _shape(scratch_db, "provider_profiles") == shape_before

    # The downgrade leaves the rows as they are, and a second upgrade starts nothing.
    _alembic(scratch_db, "downgrade", BEFORE_SVC_414)
    assert _boost_state(scratch_db, ids) == after
    again = _alembic_result(scratch_db, "upgrade", SVC_414)
    assert again.returncode == 0, again.stderr
    assert "0 VERIFIED SPs started at kyc_reviewed_at, 1 skipped" in again.stderr
    assert _boost_state(scratch_db, ids) == after
