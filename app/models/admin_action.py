import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# The only actions and target types there are (SVC-329). The DB CHECK
# constraints are built from these tuples, so adding one means a migration too.
ADMIN_ACTIONS = ("user_deactivated", "pro_force_removed", "pro_reassigned")
ADMIN_ACTION_TARGET_TYPES = ("user", "professional")


class AdminAction(Base):
    """One admin mutation, who made it, why, and to what (SVC-329).

    ``target_type`` + ``target_id`` name what was acted on: the user for a
    deactivation, the Pro row for a force-remove, the *old* Pro row for a
    reassign. No FK on ``target_id``, since it points at either table.

    ``sp_user_id`` / ``to_sp_user_id`` are the owners the action touched, so an
    owner's history is one indexed read: the SP a Pro was removed from (or the
    owner who was deactivated), and for a reassign the owner the person moved
    to. Filtering by an owner matches either column, so a move shows in both
    owners' histories.

    **Append-only.** Rows are written only by ``record_admin_action``, in the
    same transaction as the change they describe, and nothing updates or
    deletes them.
    """

    __tablename__ = "admin_actions"

    __table_args__ = (
        CheckConstraint(
            "action IN (" + ", ".join(f"'{a}'" for a in ADMIN_ACTIONS) + ")",
            name="ck_admin_actions_action",
        ),
        CheckConstraint(
            "target_type IN (" + ", ".join(f"'{t}'" for t in ADMIN_ACTION_TARGET_TYPES) + ")",
            name="ck_admin_actions_target_type",
        ),
        Index("ix_admin_actions_target", "target_type", "target_id"),
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
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
