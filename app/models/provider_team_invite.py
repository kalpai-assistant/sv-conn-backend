import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# The link texted to the invitee is INVITE_URL_BASE + token: the one line to
# change when the shape firms up. STPL rejected the path-segment form
# (/invite/<token>) for DLT; the query-param form is what SVC-328 resubmitted.
# The domain is the web app (not the marketing site at eazyserv.com, which has no
# /invite route) and must match the URL pattern registered with the DLT
# template, or telcos drop the SMS.
INVITE_URL_BASE = "https://app.eazyserv.com/invite?t="

# ``secrets.token_urlsafe(n)`` yields ~1.33n characters: 48 → 64 chars, per the
# ticket. NB: DLT caps each template variable at 30 characters, so a token
# texted on its own (template holds the URL prefix) needs n ≤ 22, and a full URL
# in one variable can't fit at all. Pending SVC-328's final template shape.
INVITE_TOKEN_BYTES = 48


class InviteStatus(str, enum.Enum):
    """Lifecycle of a team invite (SVC-320).

    Stored as a plain VARCHAR rather than a Postgres enum, so adding a state
    later is a code change, not a migration.

    Expiry is lazy: a row stays ``PENDING`` past ``expires_at`` until something
    touches it (an accept attempt, or the SP re-inviting the same phone), which
    flips it to ``EXPIRED``. Read "is it live?" from ``is_live``, never from
    ``status`` alone.
    """

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class ProviderTeamInvite(Base):
    """An SP's invitation for a phone number to join its team as a Pro (SVC-320).

    The token travels by SMS and is single-use: accepting moves the row out of
    ``PENDING`` for good.
    """

    __tablename__ = "provider_team_invites"

    __table_args__ = (
        # At most one live invite per (SP, phone). Partial, so accepted, revoked
        # and expired rows stay as history and don't block a fresh invite.
        Index(
            "ux_provider_team_invites_pending",
            "invited_by",
            "invitee_phone",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # The SP, keyed by ``provider_profiles.user_id`` like ``professionals``.
    # CASCADE: an SP's invites go with it.
    invited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider_profiles.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Canonical ``+91XXXXXXXXXX``, the same form ``users.phone`` holds, so
    # accept-invite can compare the two directly.
    invitee_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    # ``secrets.token_urlsafe(48)`` is 64 characters; 96 leaves headroom.
    token: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=InviteStatus.PENDING.value,
        server_default=InviteStatus.PENDING.value,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @property
    def invite_url(self) -> str:
        return INVITE_URL_BASE + self.token

    def is_live(self, now: datetime) -> bool:
        return self.status == InviteStatus.PENDING.value and self.expires_at > now
