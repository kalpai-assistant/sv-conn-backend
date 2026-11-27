"""A ban on an SP submitting service requests, after too many rejections (SVC-436).

One row per ban, so the history ("banned, then an admin cleared it") is kept:
there is deliberately no unique on ``sp_id``. A ban is **active** while
``banned_until > now()`` and ``cleared_at IS NULL``. It **ended** at
``cleared_at`` if an admin cleared it early, else at ``banned_until``. A second
trigger while a ban is active extends that row instead of adding one.

Written only by ``ServiceRequestService.trigger_spam_check_on_rejection``; the
admin clear endpoint is a later story.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SpRequestBan(Base):
    __tablename__ = "sp_request_bans"
    __table_args__ = (Index("ix_sp_request_bans_sp_id", "sp_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # RESTRICT (no ondelete): accounts are tombstoned, never deleted (SVC-367).
    sp_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_profiles.user_id"), nullable=False
    )
    banned_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # SET NULL (Vipul, SVC-436 comment 14840): keep the record, drop attribution.
    cleared_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
