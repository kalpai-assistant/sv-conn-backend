"""One row per Exotel call the platform saw (SVC-448).

The audit log for every bridged (and every *unbridged*) call that hit the
pool. Written on the ``incoming`` webhook (ringing state, provider CallSid
assigned), updated on the ``call-ended`` webhook (duration, status,
recording URL). Admins read this at the call-records viewer (SVC-454).

Design decisions (see ticket SVC-448):

* ``call_mapping_id`` is **nullable** on purpose — a call to a virtual number
  that has no active mapping (wrong-number, pool recycled, mapping already
  released) still logs a row with ``direction = 'unmatched'``. We keep the
  evidence for disputes and for admin "did this call hit us?" lookups.
* ``provider_call_sid`` is Exotel's CallSid, unique — the webhook is
  deliberately idempotent via ``INSERT ... ON CONFLICT`` on this column.
* Enums are string-valued (not Python-name-valued) so the DB enum text
  matches the API text; see the ``AppVersion.platform`` comment for why.
* ``recording_expires_at`` is set to ``started_at + 90d`` by the call-ended
  webhook (SVC-450). The nightly purge job (SVC-451) deletes recordings
  past their expiry via the Exotel delete-recording API and clears the URL.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CallDirection(str, enum.Enum):
    """Who dialed whom, resolved against the active mapping.

    ``unmatched`` is a call to a virtual number with no live mapping (wrong
    number, pool recycled, mapping already released) — still logged for audit.
    """

    CUSTOMER_TO_SP = "customer_to_sp"
    SP_TO_CUSTOMER = "sp_to_customer"
    UNMATCHED = "unmatched"


class CallStatus(str, enum.Enum):
    """Exotel call lifecycle. Mirrors Exotel's own vocabulary 1:1 so the
    webhook writes the status it was given, without a translation layer.

    ``declined_consent`` is the one addition on top of Exotel's native set
    (SVC-451): the press-2 branch on the consent IVR writes this status
    after hanging up. The DB enum value is added by the SVC-451 migration
    (``ALTER TYPE callstatus ADD VALUE 'declined_consent'``).
    """

    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    DECLINED_CONSENT = "declined_consent"


class CallRecord(Base):
    """Audit log of one call seen on a virtual number — bridged or not.

    Created on the Exotel ``incoming`` webhook in ``ringing`` state; updated to
    terminal status on ``call-ended``. Admins query by call_mapping_id (one
    mapping, many retries), by from_e164 (who called this number), and by
    started_at DESC (feed).
    """

    __tablename__ = "call_records"

    __table_args__ = (
        Index("ix_call_records_call_mapping_id", "call_mapping_id"),
        Index("ix_call_records_from_e164", "from_e164"),
        # Admin feed ordering. DESC so the viewer's "newest first" default
        # is an index scan, not a sort.
        Index("ix_call_records_started_at_desc", "started_at", postgresql_using="btree"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable: a call to a virtual number with no active mapping still logs
    # a row (direction='unmatched') for audit. The ticket deliberately asks
    # for this — unmatched calls are evidence in disputes.
    call_mapping_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("call_mappings.id"), nullable=True
    )
    # Exotel's CallSid, unique. The webhook upserts on this column so a
    # delivery retry does not create a second row.
    provider_call_sid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    from_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    # The virtual number that was dialed.
    to_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    # Whichever real phone we bridged the call to (SP's for customer→SP,
    # customer's for SP→customer). NULL on an unmatched / failed-before-bridge
    # call.
    bridged_to_e164: Mapped[str | None] = mapped_column(String(16), nullable=True)
    direction: Mapped[CallDirection] = mapped_column(
        Enum(
            CallDirection,
            name="calldirection",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    status: Mapped[CallStatus] = mapped_column(
        Enum(
            CallStatus,
            name="callstatus",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Exotel's recording URL. Text because Exotel URLs can carry long signed
    # query strings. NULL until call-ended, and set back to NULL by the
    # nightly purge once past retention.
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # started_at + 90d, set by the call-ended webhook. The nightly purge
    # (SVC-451) deletes recordings past this and clears recording_url.
    recording_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
