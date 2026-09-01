"""add booking_offers table

Revision ID: c3d9e1f70a2b
Revises: 813f1842efef
Create Date: 2026-08-31 18:40:00.000000

SVC-254: persists which providers were offered which booking, so an offer
survives a failed FCM push. Backs `GET /providers/me/offers` (the polling
fallback) and `PATCH /bookings/{id}/decline`.

No `expires_at` column on purpose — expiry is `booking.created_at +
BOOKING_REQUEST_TIMEOUT_SECONDS`, read live by both `_offer_expiry` and
`expire_stale_bookings`. A stored copy would be a second deadline that
disagrees the moment the setting is retuned.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3d9e1f70a2b"
down_revision: Union[str, None] = "813f1842efef"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Labels are the enum MEMBER NAMES, not their values: the model declares
    # `Enum(OfferResponse)` with no `values_callable`, and SQLAlchemy persists
    # names in that form. Same convention as `settlementstatus`.
    offer_response = postgresql.ENUM(
        "ACCEPTED", "DECLINED", name="offerresponse", create_type=False
    )
    offer_response.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "booking_offers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "booking_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bookings.id"),
            nullable=False,
        ),
        sa.Column(
            "provider_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "offered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Null while the offer is open.
        sa.Column("response", offer_response, nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        # The dispatch task is a Celery task and Celery retries. Without this a
        # retry offers the same provider the same booking twice.
        sa.UniqueConstraint("booking_id", "provider_id", name="uix_booking_offer"),
    )

    op.create_index("ix_booking_offers_booking_id", "booking_offers", ["booking_id"])
    op.create_index("ix_booking_offers_provider_id", "booking_offers", ["provider_id"])
    # The poll's hot path: "my rows that are still open".
    op.create_index(
        "ix_booking_offers_provider_response", "booking_offers", ["provider_id", "response"]
    )


def downgrade() -> None:
    op.drop_table("booking_offers")
    # Drop the enum type too — leaving orphans behind pollutes psql \dT.
    postgresql.ENUM(name="offerresponse").drop(op.get_bind(), checkfirst=True)
