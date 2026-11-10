"""DNC (Do-Not-Call) list — phone-level opt-out for call masking (SVC-451).

One row per phone that must never be bridged by the call-masking layer.
Written by admin / support (no self-serve in v1); read by
:meth:`CallMaskingService.allocate_mapping` as a pre-allocation block —
if either customer or SP phone is on DNC, allocation is refused with a
user-friendly error. See SVC-451 for the policy decisions.

Design decisions:

* ``phone_e164`` is UNIQUE — one DNC entry per number. A re-add with a
  new ``source``/``reason`` is explicit (delete-and-insert via admin CRUD)
  so the audit trail reads as events, not silent upserts.
* ``source`` is an enum so the audit shows *why* a number is on DNC:
  ``user_request`` (came through support), ``trai_notice`` (TRAI /
  regulator sent us a notice), or ``admin`` (platform-initiated, e.g.
  known spam / abuse number).
* ``reason`` is free-form — the SR number, notice reference, or the
  admin's note. Nullable because the ``source`` alone is sometimes all
  we have.
* No ``updated_at`` / soft-delete: entries are immutable — remove =
  hard-delete. SVC-454's admin CRUD treats add/remove as separate audit
  events.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DncSource(str, enum.Enum):
    """Why a phone is on the DNC list. Audit-trail only — all three block equally."""

    USER_REQUEST = "user_request"
    TRAI_NOTICE = "trai_notice"
    ADMIN = "admin"


class DncListEntry(Base):
    """One row per phone on the Do-Not-Call list.

    A hit on this table in :meth:`CallMaskingService.allocate_mapping`
    blocks the mapping with :class:`PhoneOnDoNotCallList` (422). No
    self-serve opt-out path in v1 — admin writes only.
    """

    __tablename__ = "dnc_list"

    __table_args__ = (
        # Recent-first scan for the admin console (SVC-454).
        Index("ix_dnc_list_added_at", "added_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # E.164, same shape as everywhere else in the call-masking schema
    # (``+91...``, varchar(16)). UNIQUE at DB level — one entry per phone.
    phone_e164: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    source: Mapped[DncSource] = mapped_column(
        Enum(
            DncSource,
            name="dnc_source_enum",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    # Free-form audit note. SR number, TRAI notice reference, admin reason.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
