"""SVC-451: dnc_list table + `declined_consent` extension of callstatus enum.

Revision ID: 8e0f4119da49
Revises: 943ec493119e
Create Date: 2026-11-09

Builds the DNC infra for call-masking (SVC-446 epic, Wave 1):

* ``dnc_list`` — phone-level opt-out used as a pre-allocation block in
  :meth:`CallMaskingService.allocate_mapping` (SVC-449). Both customer and
  SP phone are checked; a hit refuses allocation with a user-friendly
  error. No self-serve opt-out in v1 — admin-only writes (SVC-454 adds
  the CRUD surface).
* ``dnc_source_enum`` — ``user_request`` / ``trai_notice`` / ``admin``,
  so the audit trail knows *why* a number is on DNC.
* ``callstatus`` enum gains a new value ``declined_consent``: the press-2
  path on the consent IVR (SVC-450 webhook) writes that onto
  ``call_records.status`` after hanging up.

``ALTER TYPE ... ADD VALUE`` runs inside ``op.get_context().autocommit_block()``
because Postgres only lets a newly-added enum value be used in queries
*after* the enclosing transaction commits — matching the project
convention set by ``d7e3b5a9c1f2`` (PAYOUT_RETURNED) and
``e721a87d86e1`` (ios platform).

Downgrade drops ``dnc_list`` + ``dnc_source_enum``. The ``callstatus``
enum value cannot be removed on Postgres (``ALTER TYPE ... DROP VALUE``
does not exist); the downgrade therefore leaves the extra value in place.
Documented here so a future rollback doesn't surprise the operator.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8e0f4119da49"
down_revision: str | None = "943ec493119e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Shared enum instance — one ``postgresql.ENUM`` per Postgres type, used by
# both the explicit ``.create()`` at the top of upgrade and the column that
# references it. ``create_type=False`` keeps ``op.create_table`` from
# re-emitting a redundant ``CREATE TYPE`` on the duplicate.
_DNC_SOURCE_ENUM = postgresql.ENUM(
    "user_request",
    "trai_notice",
    "admin",
    name="dnc_source_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    _DNC_SOURCE_ENUM.create(bind, checkfirst=True)

    op.create_table(
        "dnc_list",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("phone_e164", sa.String(length=16), nullable=False),
        sa.Column("source", _DNC_SOURCE_ENUM, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("phone_e164", name="uq_dnc_list_phone_e164"),
    )
    op.create_index(
        "ix_dnc_list_added_at",
        "dnc_list",
        ["added_at"],
    )

    # Extend ``callstatus`` with ``declined_consent``. The ``autocommit_block``
    # ensures the new value is committed before any subsequent SQL (within
    # this or a later revision) tries to use it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE callstatus ADD VALUE IF NOT EXISTS 'declined_consent'")


def downgrade() -> None:
    op.drop_index("ix_dnc_list_added_at", table_name="dnc_list")
    op.drop_table("dnc_list")

    bind = op.get_bind()
    _DNC_SOURCE_ENUM.drop(bind, checkfirst=True)

    # Postgres has no ``ALTER TYPE ... DROP VALUE``. The ``declined_consent``
    # entry stays in the ``callstatus`` enum after downgrade — documented
    # in the module docstring so a future rollback doesn't surprise anyone.
