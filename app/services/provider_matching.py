"""
Provider eligibility for a booking broadcast — the single definition of
"may this provider see and take this job".

Session-less on purpose: this is consumed from two places that cannot share a
session type — the sync Celery worker that fans the ``new_request`` push out, and
the async request path that serves a provider the offer behind that push. Both
call the same expressions, so a provider can never be pushed a job they are then
refused, or shown one they were never sent (SVC-153).
"""

from sqlalchemy import Select, select

from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.user import User
from app.utils.geo import st_dwithin_column_radius


def eligible_provider_conditions(service_id, address_location) -> list:
    """WHERE conditions selecting the providers eligible for one booking.

    A provider qualifies when they are subscribed to the booked service, online,
    KYC-verified, and the consumer's address falls inside *their own* service
    radius — the radius is per-provider (``service_area_radius_km``), which is why
    this uses ``st_dwithin_column_radius`` and not ``st_dwithin_filter``, whose
    radius is one constant for the whole query.

    ``address_location`` is the address's geography value, passed in rather than
    re-queried so the caller controls the fetch.
    """
    return [
        ProviderServiceModel.service_id == service_id,
        ProviderProfile.is_online.is_(True),
        ProviderProfile.kyc_status == KYCStatus.VERIFIED,
        st_dwithin_column_radius(
            ProviderProfile.service_area_center,
            address_location,
            ProviderProfile.service_area_radius_km,
        ),
    ]


def eligible_providers_stmt(service_id, address_location) -> Select:
    """Select the ``User`` rows that should receive a booking's broadcast."""
    return (
        select(User)
        .join(ProviderProfile, User.id == ProviderProfile.user_id)
        .join(ProviderServiceModel, ProviderServiceModel.provider_id == ProviderProfile.user_id)
        .where(*eligible_provider_conditions(service_id, address_location))
    )


def is_eligible_provider_stmt(service_id, address_location, provider_id) -> Select:
    """Select ``1`` when this specific provider is eligible for this booking.

    The membership test behind the broadcast: narrowing the same query rather than
    restating the rule, so the two can't drift.
    """
    return (
        select(ProviderProfile.user_id)
        .join(ProviderServiceModel, ProviderServiceModel.provider_id == ProviderProfile.user_id)
        .where(
            ProviderProfile.user_id == provider_id,
            *eligible_provider_conditions(service_id, address_location),
        )
        .limit(1)
    )
