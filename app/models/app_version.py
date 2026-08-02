"""App version metadata (SVC-162).

Powers the in-app version check → the mechanism that lets us force an
upgrade after direct-APK / PWA distribution (no Play Store, no
auto-update). One row per platform (currently ``android`` + ``pwa``).

Deliberately a table rather than env vars: `min_supported_version` is the
emergency lever we flip during an incident. Env vars require a Fargate
task revision + rollout (~5 min); a table row can be flipped by an
admin API call OR raw ``UPDATE`` in ~1 second.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AppPlatform(str, enum.Enum):
    ANDROID = "android"
    PWA = "pwa"


class AppVersion(Base):
    """One row per platform, holds the current + minimum-supported version.

    Enforced-unique on ``platform`` so a lookup by platform always returns
    a single row (or nothing — first-boot state before seed).
    """

    __tablename__ = "app_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # unique=True enforces one row per platform. Simpler than a composite key
    # and matches the query pattern (always "give me the row for X platform").
    platform: Mapped[AppPlatform] = mapped_column(
        Enum(AppPlatform), unique=True, nullable=False, index=True
    )

    # SemVer strings — comparison is done in-app via the `semver` npm package
    # / equivalent. Stored as string because Postgres has no semver type and
    # sortable-string wouldn't order 1.10.0 > 1.2.0 correctly anyway.
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    min_supported_version: Mapped[str] = mapped_column(String(32), nullable=False)

    # Android APK URL. NULL for PWA (updates come via service worker).
    url: Mapped[str | None] = mapped_column(String(500))

    # Short plain-text release notes. Rendered in the upgrade nudge toast.
    # Kept plain — no markdown, no HTML — because the toast has no room for
    # anything else and any user-supplied markup risks injection into a
    # notification body.
    release_notes: Mapped[str | None] = mapped_column(String(500))

    released_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
