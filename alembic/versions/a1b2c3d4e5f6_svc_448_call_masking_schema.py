"""SVC-448: call-masking schema (virtual_number_pool, call_mappings, call_records)

Revision ID: a1b2c3d4e5f6
Revises: d7e3b5a9c1f2
Create Date: 2026-11-09

Foundation for SVC-446 (Exotel call masking). Three tables + three Postgres
enum types. Wave 1 (SVC-449/450/451) imports the models these create; this
revision ships alone.

Baked defaults (locked in SVC-448):

* UUID PKs, matching the rest of the SVC schema.
* Phones are E.164 varchar(16) everywhere; no (country, number) split.
* ``call_mappings.virtual_number_e164`` is denormalized off
  ``virtual_number_pool.e164``: the Exotel webhook does a single-row lookup
  by virtual number without a join, and the partial unique index below
  lives on the same column.
* Partial unique ``(virtual_number_e164) WHERE released_at IS NULL`` — one
  active mapping per number at DB level.
* ``call_records.call_mapping_id`` is nullable: a call to a virtual number
  with no live mapping still logs a row (``direction='unmatched'``) for audit.

The enum values are spelled out as a list here, not imported from the
model, so this revision keeps creating the same enums if the model's enum
later changes. Downgrade drops enums after the tables so the types are
unreferenced at DROP TYPE time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "d7e3b5a9c1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Enum type descriptors — single source of truth for upgrade + downgrade so
# the two can't disagree about the value list.
_PROVIDER_ENUM = sa.Enum("exotel", name="virtualnumberprovider")
_NUMBER_STATUS_ENUM = sa.Enum(
    "active", "disabled", "retired", name="virtualnumberstatus"
)
_DIRECTION_ENUM = sa.Enum(
    "customer_to_sp", "sp_to_customer", "unmatched", name="calldirection"
)
_CALL_STATUS_ENUM = sa.Enum(
    "ringing",
    "in_progress",
    "completed",
    "failed",
    "no_answer",
    "busy",
    name="callstatus",
)


def upgrade() -> None:
    bind = op.get_bind()
    _PROVIDER_ENUM.create(bind, checkfirst=True)
    _NUMBER_STATUS_ENUM.create(bind, checkfirst=True)
    _DIRECTION_ENUM.create(bind, checkfirst=True)
    _CALL_STATUS_ENUM.create(bind, checkfirst=True)

    # --- virtual_number_pool ------------------------------------------------
    op.create_table(
        "virtual_number_pool",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("e164", sa.String(length=16), nullable=False),
        sa.Column(
            "provider",
            sa.Enum(
                "exotel", name="virtualnumberprovider", create_type=False
            ),
            nullable=False,
            server_default="exotel",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "disabled",
                "retired",
                name="virtualnumberstatus",
                create_type=False,
            ),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("e164", name="uq_virtual_number_pool_e164"),
    )
    op.create_index(
        "ix_virtual_number_pool_status",
        "virtual_number_pool",
        ["status"],
    )

    # --- call_mappings ------------------------------------------------------
    op.create_table(
        "call_mappings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("virtual_number_id", sa.UUID(), nullable=False),
        sa.Column("virtual_number_e164", sa.String(length=16), nullable=False),
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("customer_phone_e164", sa.String(length=16), nullable=False),
        sa.Column("sp_phone_e164", sa.String(length=16), nullable=False),
        sa.Column(
            "allocated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["virtual_number_id"], ["virtual_number_pool.id"]
        ),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # Partial unique — one active mapping per number at DB level, so a
    # concurrent allocator can't double-book.
    op.create_index(
        "uq_call_mappings_virtual_number_active",
        "call_mappings",
        ["virtual_number_e164"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        "ix_call_mappings_booking_id",
        "call_mappings",
        ["booking_id"],
    )
    # Partial: the release-expired cron only cares about active rows.
    op.create_index(
        "ix_call_mappings_expires_at_active",
        "call_mappings",
        ["expires_at"],
        postgresql_where=sa.text("released_at IS NULL"),
    )

    # --- call_records -------------------------------------------------------
    op.create_table(
        "call_records",
        sa.Column("id", sa.UUID(), nullable=False),
        # Nullable on purpose: unmatched calls still log a row.
        sa.Column("call_mapping_id", sa.UUID(), nullable=True),
        sa.Column("provider_call_sid", sa.String(length=64), nullable=False),
        sa.Column("from_e164", sa.String(length=16), nullable=False),
        sa.Column("to_e164", sa.String(length=16), nullable=False),
        sa.Column("bridged_to_e164", sa.String(length=16), nullable=True),
        sa.Column(
            "direction",
            sa.Enum(
                "customer_to_sp",
                "sp_to_customer",
                "unmatched",
                name="calldirection",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "ringing",
                "in_progress",
                "completed",
                "failed",
                "no_answer",
                "busy",
                name="callstatus",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column("recording_url", sa.Text(), nullable=True),
        sa.Column(
            "recording_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["call_mapping_id"], ["call_mappings.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_call_sid", name="uq_call_records_provider_call_sid"
        ),
    )
    op.create_index(
        "ix_call_records_call_mapping_id",
        "call_records",
        ["call_mapping_id"],
    )
    op.create_index(
        "ix_call_records_from_e164",
        "call_records",
        ["from_e164"],
    )
    op.create_index(
        "ix_call_records_started_at_desc",
        "call_records",
        ["started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_call_records_started_at_desc", table_name="call_records")
    op.drop_index("ix_call_records_from_e164", table_name="call_records")
    op.drop_index("ix_call_records_call_mapping_id", table_name="call_records")
    op.drop_table("call_records")

    op.drop_index(
        "ix_call_mappings_expires_at_active", table_name="call_mappings"
    )
    op.drop_index("ix_call_mappings_booking_id", table_name="call_mappings")
    op.drop_index(
        "uq_call_mappings_virtual_number_active", table_name="call_mappings"
    )
    op.drop_table("call_mappings")

    op.drop_index(
        "ix_virtual_number_pool_status", table_name="virtual_number_pool"
    )
    op.drop_table("virtual_number_pool")

    bind = op.get_bind()
    _CALL_STATUS_ENUM.drop(bind, checkfirst=True)
    _DIRECTION_ENUM.drop(bind, checkfirst=True)
    _NUMBER_STATUS_ENUM.drop(bind, checkfirst=True)
    _PROVIDER_ENUM.drop(bind, checkfirst=True)
