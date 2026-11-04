import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AdminRole(str, enum.Enum):
    ADMIN = "admin"
    OPS = "ops"
    SUPPORT = "support"


def admin_placeholder_phone(admin_id: uuid.UUID) -> str:
    """``users.phone`` for an admin's linked row: ``A`` + 14 hex of the admin id.

    Same idea as a deleted account's ``D`` + 14 hex (SVC-367). ``users.phone`` is
    NOT NULL and unique, and this value can never pass phone validation, so
    nobody can ask for an OTP against it.
    """
    return "A" + admin_id.hex[:14]


class AdminUser(Base):
    """An admin console login (SVC-422). Provisioned only, no self-signup.

    ``user_id`` links to a ``users`` row that exists to be the actor id:
    ``admin_actions`` and ``payout_audit`` have foreign keys to ``users.id``, and
    existing admin routes read ``admin.id``. That row has ``is_active=false`` and
    a placeholder phone, so it can never log in by phone.

    To unblock a locked account:
    ``UPDATE admin_users SET failed_login_count = 0, locked_until = NULL WHERE email = '...'``.
    """

    __tablename__ = "admin_users"
    __table_args__ = (
        # Login lowercases the email, so a mixed-case row inserted by hand could
        # never log in. Provisioning is by SQL, so refuse it at the database.
        CheckConstraint("email = lower(email)", name="ck_admin_users_email_lowercase"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Encrypted with app/utils/encryption.py. Set at setup, before it is verified,
    # so a non-NULL secret does not mean TOTP is on: ``totp_enabled_at`` does.
    totp_secret: Mapped[str | None] = mapped_column(String(255))
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    role: Mapped[AdminRole] = mapped_column(
        Enum(AdminRole, name="adminrole", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
