"""Scheduled commission changes and the notices sent about them (SVC-486).

A ``commission_changes`` row is one change of a variant's commission percent.
An increase is scheduled at least 14 days ahead. A decrease can take effect
the same day. A PATCH decrease is recorded here too, already applied, so every
% change has one paper trail. Rows are never deleted.

* ``scheduled``: waiting for ``effective_from`` (00:00 IST that day). At most
  one per variant, held by a partial unique index.
* ``applied``: ``commission_changes.apply_due_commission_changes`` set the
  variant's amounts at ``new_pct`` on its price at that moment. The amounts
  before and after are stored here, with ``applied_at``. The apply writes no
  admin audit row, because no admin acted. The admin's decision is audited when
  they schedule or cancel the change.
* ``cancelled``: an admin cancelled it before it applied.

``commission_change_notices`` is the pros' paper trail (Vipul, SVC-486
comment 14963): one row per SP per channel per kind, with its outcome. It holds
no PII. The SP, the change and its old %, new % and date are all reached
through the two ids.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

STATUS_SCHEDULED = "scheduled"
STATUS_APPLIED = "applied"
STATUS_CANCELLED = "cancelled"

KIND_SCHEDULED = "scheduled"
KIND_CANCELLED = "cancelled"

CHANNEL_PUSH = "push"
CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"

NOTICE_SENT = "sent"
NOTICE_FAILED = "failed"
NOTICE_SKIPPED_NO_EMAIL = "skipped_no_email"
NOTICE_SKIPPED_NO_TOKEN = "skipped_no_token"
NOTICE_MOCKED = "mocked"

SCHEDULED_PREDICATE = "status = 'scheduled'"


class CommissionChange(Base):
    __tablename__ = "commission_changes"

    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled', 'applied', 'cancelled')",
            name="ck_commission_changes_status",
        ),
        CheckConstraint(
            "status <> 'applied' OR (applied_at IS NOT NULL"
            " AND new_platform_commission IS NOT NULL AND new_provider_payout IS NOT NULL"
            " AND old_platform_commission IS NOT NULL AND old_provider_payout IS NOT NULL)",
            name="ck_commission_changes_applied",
        ),
        CheckConstraint(
            "status <> 'cancelled' OR (cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL)",
            name="ck_commission_changes_cancelled",
        ),
        Index(
            "uq_commission_changes_one_scheduled",
            "service_variant_id",
            unique=True,
            postgresql_where=text(SCHEDULED_PREDICATE),
        ),
        Index(
            "ix_commission_changes_scheduled_effective",
            "effective_from",
            postgresql_where=text(SCHEDULED_PREDICATE),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_variants.id"), nullable=False, index=True
    )
    old_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    new_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The variant's amounts just before and just after the apply; NULL until then.
    old_platform_commission: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    old_provider_payout: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    new_platform_commission: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    new_provider_payout: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CommissionChangeNotice(Base):
    __tablename__ = "commission_change_notices"

    __table_args__ = (
        CheckConstraint(
            "kind IN ('scheduled', 'cancelled')", name="ck_commission_change_notices_kind"
        ),
        CheckConstraint(
            "channel IN ('push', 'email', 'sms')", name="ck_commission_change_notices_channel"
        ),
        CheckConstraint(
            "status IN ('sent', 'failed', 'skipped_no_email', 'skipped_no_token', 'mocked')",
            name="ck_commission_change_notices_status",
        ),
        # One row per SP per channel per kind: a re-run task can't send twice.
        UniqueConstraint(
            "change_id", "sp_id", "kind", "channel", name="uq_commission_change_notices_once"
        ),
        Index("ix_commission_change_notices_sp_id", "sp_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    change_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commission_changes.id"), nullable=False
    )
    sp_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text)
