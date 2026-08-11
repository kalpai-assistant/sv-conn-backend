"""
Import all ORM models here so Alembic can discover them via Base.metadata.
The order of imports matters for circular-reference safety; models with no
FK dependencies come first.
"""

from app.models.address import Address  # noqa: F401
from app.models.app_version import AppPlatform, AppVersion  # noqa: F401
from app.models.booking import (  # noqa: F401
    VALID_TRANSITIONS,
    Booking,
    BookingStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.models.booking_item import BookingItem  # noqa: F401
from app.models.booking_status_log import BookingStatusLog  # noqa: F401
from app.models.category import Category  # noqa: F401
from app.models.coupon import Coupon, CouponRedemption, DiscountType  # noqa: F401
from app.models.payment import Payment  # noqa: F401
from app.models.payout_audit import PayoutAudit  # noqa: F401
from app.models.provider_profile import KYCStatus, ProviderProfile  # noqa: F401
from app.models.provider_service import ProviderService  # noqa: F401
from app.models.review import Review  # noqa: F401
from app.models.service import Service  # noqa: F401
from app.models.user import User, UserRole  # noqa: F401
