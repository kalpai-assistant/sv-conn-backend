import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# The owner history on the Teams screen reads these three by name, and
# GET /admin/actions returns nothing else. Don't rename them.
TEAM_ACTIONS = ("user_deactivated", "pro_force_removed", "pro_reassigned")

# Every action and target type in use. Add new ones here; no migration needed,
# since SVC-423 dropped the DB CHECKs so a new admin story doesn't need one just
# to log itself. New actions use the ``area.verb`` form.
ADMIN_ACTIONS = (
    *TEAM_ACTIONS,
    "kyc.approve",
    "kyc.reject",
    "booking.force_cancel",
    "payout.enqueue",
    "payout.requeue_failed",
    "payout.requeue_reversed",
    "app_version.release",
    "service.create",
    "service.update",
    "service.delete",
)
ADMIN_ACTION_TARGET_TYPES = (
    "user",
    "professional",
    "provider",
    "booking",
    "app_version",
    "service",
)

USER_AGENT_MAX_LENGTH = 512


class AdminAction(Base):
    """One admin mutation, who made it, why, and to what (SVC-329, SVC-423).

    ``target_type`` + ``target_id`` name what was acted on: the user for a
    deactivation, the Pro row for a force-remove, the *old* Pro row for a
    reassign. No FK on ``target_id``, since it points at many tables. It is text
    because not every target has a uuid: an app version is keyed by platform,
    and ``pricing_config`` (SVC-428) by a text key.

    ``sp_user_id`` / ``to_sp_user_id`` are the owners the action touched, so an
    owner's history is one indexed read: the SP a Pro was removed from (or the
    owner who was deactivated), and for a reassign the owner the person moved
    to. Filtering by an owner matches either column, so a move shows in both
    owners' histories.

    ``before_json`` / ``after_json`` hold allow-listed fields only (see
    ``app/services/admin_audit.py``): NULL before on a create, NULL after on a
    delete. ``ip_address`` and ``user_agent`` are NULL when they can't be saved.

    **Append-only.** Rows are written only by ``record_admin_action``, and
    nothing updates or deletes them. Each is written in the same transaction as
    the change it describes. ``POST /admin/providers/{id}/payout`` has no change
    of its own: it commits its row, then enqueues the task that moves the money.
    """

    __tablename__ = "admin_actions"

    __table_args__ = (
        Index("ix_admin_actions_target", "target_type", "target_id"),
        Index("ix_admin_actions_admin_created", "admin_user_id", text("created_at DESC")),
        Index("ix_admin_actions_created_at", "created_at"),
        Index("ix_admin_actions_sp_user_id", "sp_user_id"),
        Index("ix_admin_actions_to_sp_user_id", "to_sp_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sp_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    to_sp_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # none_as_null: otherwise a Python None is saved as the JSON value null, and
    # "NULL on a create" stops being true.
    before_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    after_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(USER_AGENT_MAX_LENGTH))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
