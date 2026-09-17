"""
Provider eligibility for a booking broadcast — the single definition of
"may this provider see and take this job".

Session-less on purpose: this is consumed from two places that cannot share a
session type — the sync Celery worker that fans the ``new_request`` push out, and
the async request path that serves a provider the offer behind that push. Both
call the same expressions, so a provider can never be pushed a job they are then
refused, or shown one they were never sent (SVC-153).

Multi-service carts use matching-policy (i): a provider is eligible for a
cart only if they are subscribed to *every* service in it. See
``eligible_providers_multi_stmt`` for how that intersection is expressed in
one query (rather than the intuitive-but-slower "eligible per service, then
intersect" that would run N queries and merge in Python).
"""

from collections.abc import Sequence

from sqlalchemy import Select, func, select

from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.user import User
from app.utils.geo import st_dwithin_column_radius


def eligible_provider_conditions(service_id, address_location) -> list:
    """WHERE conditions selecting the providers eligible for one booking.

    A provider qualifies when they are subscribed to the booked service, online,
    KYC-verified, reachable by push (SVC-276 — ``fcm_token IS NOT NULL``), and the
    consumer's address falls inside *their own* service radius — the radius is
    per-provider (``service_area_radius_km``), which is why this uses
    ``st_dwithin_column_radius`` and not ``st_dwithin_filter``, whose radius is
    one constant for the whole query.

    The token predicate is what SVC-276 added. Before it, ``is_online`` was the
    only reachability signal and the client is the only thing that ever cleared
    it — so a provider whose session expired, or who uninstalled, or whose token
    rotated and re-registration failed still passed this filter, got a
    ``booking_offers`` row written for them, and heard nothing. Now the invariant
    is by construction: broadcast only picks providers we can actually reach.
    Defence-in-depth in ``notification_tasks.send_booking_notification`` still
    skips a NULL token per-recipient, so the two remain redundant on purpose.

    ``address_location`` is the address's geography value, passed in rather than
    re-queried so the caller controls the fetch.
    """
    return [
        ProviderServiceModel.service_id == service_id,
        ProviderProfile.is_online.is_(True),
        ProviderProfile.kyc_status == KYCStatus.VERIFIED,
        User.fcm_token.is_not(None),
        st_dwithin_column_radius(
            ProviderProfile.service_area_center,
            address_location,
            ProviderProfile.service_area_radius_km,
        ),
    ]


def eligible_providers_stmt(service_id, address_location) -> Select:
    """Select the ``User`` rows that should receive a broadcast for one service.

    Preserved verbatim from the pre-cart era for callers that still need the
    single-service form (none in current code, but the shape is clean and
    used by tests that pin the invariant).
    """
    return (
        select(User)
        .join(ProviderProfile, User.id == ProviderProfile.user_id)
        .join(ProviderServiceModel, ProviderServiceModel.provider_id == ProviderProfile.user_id)
        .where(*eligible_provider_conditions(service_id, address_location))
    )


def is_eligible_provider_stmt(service_id, address_location, provider_id) -> Select:
    """Select ``1`` when this specific provider is eligible for one service.

    The membership test behind the broadcast: narrowing the same query rather than
    restating the rule, so the two can't drift.
    """
    return (
        select(ProviderProfile.user_id)
        .join(User, User.id == ProviderProfile.user_id)
        .join(ProviderServiceModel, ProviderServiceModel.provider_id == ProviderProfile.user_id)
        .where(
            ProviderProfile.user_id == provider_id,
            *eligible_provider_conditions(service_id, address_location),
        )
        .limit(1)
    )


# ---------------------------------------------------------------------------
# Multi-service (cart) variants — matching-policy (i)
# ---------------------------------------------------------------------------


def eligible_providers_multi_stmt(service_ids: Sequence, address_location) -> Select:
    """``User`` rows that should receive a broadcast for a whole cart.

    A provider qualifies only if they are subscribed to **every** service in
    the cart — the intersection, not the union. Expressed as one query:
    join provider_services filtered to the cart's service ids, keep providers
    whose count of matched rows equals the cart size.

    Written this way instead of the intuitive "one query per service, then
    intersect in Python" for two reasons:

    1. That form is N round-trips per broadcast; the new_request Celery task
       already runs for every booking created, and multiplying its DB work
       by cart size would be silently expensive at scale.
    2. Intersecting in Python moves a correctness-critical set operation
       away from the DB and out of the invariant SVC-153 established
       (broadcast set == accept set). Anywhere the two sides drift, a
       provider gets a push they can't act on or vice versa.

    Falls back to ``eligible_providers_stmt`` for the degenerate one-item
    case rather than paying the GROUP BY overhead when it can't help.
    """
    service_ids = list(service_ids)
    if len(service_ids) == 1:
        return eligible_providers_stmt(service_ids[0], address_location)

    # Same conditions as the single-service form, minus the single-service
    # equality — that's replaced by the IN + HAVING pair below.
    base_conditions = [
        ProviderProfile.is_online.is_(True),
        ProviderProfile.kyc_status == KYCStatus.VERIFIED,
        User.fcm_token.is_not(None),
        st_dwithin_column_radius(
            ProviderProfile.service_area_center,
            address_location,
            ProviderProfile.service_area_radius_km,
        ),
    ]
    return (
        select(User)
        .join(ProviderProfile, User.id == ProviderProfile.user_id)
        .join(ProviderServiceModel, ProviderServiceModel.provider_id == ProviderProfile.user_id)
        .where(
            ProviderServiceModel.service_id.in_(service_ids),
            *base_conditions,
        )
        .group_by(User.id, ProviderProfile.user_id)
        # COUNT DISTINCT rather than plain COUNT — a duplicated
        # provider_services row (shouldn't happen; uix_provider_service
        # enforces uniqueness) would otherwise inflate the count and
        # falsely qualify a provider.
        .having(func.count(func.distinct(ProviderServiceModel.service_id)) == len(service_ids))
    )


def is_eligible_provider_multi_stmt(service_ids: Sequence, address_location, provider_id) -> Select:
    """Select ``1`` when this specific provider is eligible for every service
    in the cart.

    Membership test behind the cart broadcast; same shape as
    ``is_eligible_provider_stmt`` for single service. Kept as a distinct
    function (rather than "call the multi variant with a list") so the
    single-service call site's error messages and OpenAPI descriptions
    remain unambiguous.
    """
    service_ids = list(service_ids)
    if len(service_ids) == 1:
        return is_eligible_provider_stmt(service_ids[0], address_location, provider_id)

    base_conditions = [
        ProviderProfile.is_online.is_(True),
        ProviderProfile.kyc_status == KYCStatus.VERIFIED,
        User.fcm_token.is_not(None),
        st_dwithin_column_radius(
            ProviderProfile.service_area_center,
            address_location,
            ProviderProfile.service_area_radius_km,
        ),
    ]
    return (
        select(ProviderProfile.user_id)
        .join(User, User.id == ProviderProfile.user_id)
        .join(ProviderServiceModel, ProviderServiceModel.provider_id == ProviderProfile.user_id)
        .where(
            ProviderProfile.user_id == provider_id,
            ProviderServiceModel.service_id.in_(service_ids),
            *base_conditions,
        )
        .group_by(ProviderProfile.user_id)
        .having(func.count(func.distinct(ProviderServiceModel.service_id)) == len(service_ids))
        .limit(1)
    )
