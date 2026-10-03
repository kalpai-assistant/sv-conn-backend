import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# The link in the invite SMS. Fully static: STPL rejected both templates that
# carried the token in the URL (path segment, then query param) with "Please
# mention fully static link" (SVC-328). The invitee opens this page and types the
# code from the same SMS. It opens the app rather than the browser only once
# Android App Links are set up for app.eazyserv.com (SVC-343). The domain is the
# web app, not the marketing site eazyserv.com, which has no /invite route.
INVITE_URL = "https://app.eazyserv.com/invite"

# The invite code: zero-padded digits (SVC-320, Vipul 2026-10-02). Numeric for a
# single mobile keyboard and Android's "copy code" chip. 10^6 combinations is
# safe because accept-invite is limited to 10 attempts/hour per user and a code
# only works from the invited phone. Going to 8 digits is this one line; the
# VARCHAR(96) column needs no migration.
INVITE_CODE_DIGITS = 6


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

    The code travels by SMS and is single-use: accepting moves the row out of
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
        # A code identifies a live invite only together with the phone it was
        # sent to. Codes repeat over time (10^6 of them, and accepted / revoked /
        # expired rows keep theirs), so a global UNIQUE(token) would turn into
        # random insert failures. Live codes are unique per phone instead, which
        # is exactly what accept-invite looks up by.
        Index(
            "ux_provider_team_invites_pending_code",
            "invitee_phone",
            "token",
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
    # The invite code (INVITE_CODE_DIGITS digits). Named ``token`` for the API's
    # ``{"token": ...}`` body. Not unique on its own; see the index above.
    token: Mapped[str] = mapped_column(String(96), nullable=False)
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
        return INVITE_URL

    def is_live(self, now: datetime) -> bool:
        return self.status == InviteStatus.PENDING.value and self.expires_at > now
