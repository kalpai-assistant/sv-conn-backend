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

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.orm import aliased

from app.models.booking import ACTIVE_JOB_STATUSES, Booking
from app.models.booking_offer import BookingOffer
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.user import User
from app.utils.geo import st_dwithin_column_radius


def assignable_team_pro_exists():
    """EXISTS a team Pro of the outer query's SP who could take a job right now
    (SVC-326): not the Pro-for-self, still on the team, KYC-verified, online,
    their user account active (SVC-329), and not holding a booking in
    ``ACTIVE_JOB_STATUSES``.

    The same tests ``/assign`` applies to a team Pro, and the busy test reads
    the same status tuple, so dispatch can't offer an SP a job that no one on
    its team could then be given. Correlated on ``ProviderProfile.user_id``.
    """
    pro = aliased(Professional)
    pro_user = aliased(User)
    job = aliased(Booking)
    return exists().where(
        pro.service_provider_id == ProviderProfile.user_id,
        pro.user_id != ProviderProfile.user_id,
        pro.is_active.is_(True),
        pro.kyc_status == KYCStatus.VERIFIED,
        pro.is_online.is_(True),
        # A deactivated person can't log in to do the job (SVC-329).
        exists().where(pro_user.id == pro.user_id, pro_user.is_active.is_(True)),
        ~exists().where(
            job.professional_id == pro.id,
            job.status.in_(ACTIVE_JOB_STATUSES),
        ),
    )


def has_someone_to_do_it():
    """The SP takes jobs itself, or has a team Pro who can (SVC-326).

    Phrased as "no switched-off Pro-for-self" rather than "a Pro-for-self that
    is on", so an SP whose Pro-for-self row is missing (accept repairs that)
    keeps the behaviour it had before the switch existed. That branch is a
    single-row index probe and comes first; the team branch only runs for an
    SP that has switched off.
    """
    self_pro = aliased(Professional)
    return or_(
        ~exists().where(
            self_pro.service_provider_id == ProviderProfile.user_id,
            self_pro.user_id == ProviderProfile.user_id,
            self_pro.accepts_direct_jobs.is_(False),
        ),
        assignable_team_pro_exists(),
    )


def _eligibility_stages(address_location) -> list[tuple[str, object]]:
    """Every eligibility condition except which services the SP covers, named
    and in the order ``broadcast_funnel`` reports them (SVC-349).

    The one list behind the single-service and cart statements and the funnel
    alike, so none of them can drift; before SVC-326 it was written out three
    times. The order is only for the funnel's report: SQL doesn't care.
    """
    return [
        ("kyc_verified", ProviderProfile.kyc_status == KYCStatus.VERIFIED),
        # A deactivated owner can't log in to accept; their token may still be
        # set, so is_online and fcm_token alone don't exclude them (SVC-329).
        ("account_active", User.is_active.is_(True)),
        ("has_push_token", User.fcm_token.is_not(None)),
        ("online", ProviderProfile.is_online.is_(True)),
        (
            "in_service_area",
            st_dwithin_column_radius(
                ProviderProfile.service_area_center,
                address_location,
                ProviderProfile.service_area_radius_km,
            ),
        ),
        ("has_someone_to_do_it", has_someone_to_do_it()),
    ]


def _base_conditions(address_location) -> list:
    """``_eligibility_stages`` without the names: the WHERE list."""
    return [condition for _, condition in _eligibility_stages(address_location)]


def _not_already_answered(booking_id):
    """The SP hasn't answered this booking's offer yet (SVC-320). Correlated
    on ``User.id``."""
    return ~exists().where(
        BookingOffer.booking_id == booking_id,
        BookingOffer.provider_id == User.id,
        BookingOffer.response.is_not(None),
    )


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

    SVC-326 adds the last condition (``has_someone_to_do_it``): an SP that has
    switched off taking jobs itself is eligible only while a team Pro could be
    assigned the job.

    ``address_location`` is the address's geography value, passed in rather than
    re-queried so the caller controls the fetch.
    """
    return [
        ProviderServiceModel.service_id == service_id,
        *_base_conditions(address_location),
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
    base_conditions = _base_conditions(address_location)
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


def broadcast_recipients_stmt(service_ids: Sequence, address_location, booking_id) -> Select:
    """Who the ``new_request`` push for this booking goes to: everyone eligible,
    minus SPs who already answered this booking's offer (SVC-320).

    The exclusion is for a reopened booking, broadcast a second time. It leaves
    out the SP that gave it back (its row is ACCEPTED or DECLINED) and any that
    declined it; the offer poll already skips answered rows the same way. A
    first broadcast has no rows yet, and a Celery retry's rows are still
    unanswered, so for those this is exactly ``eligible_providers_multi_stmt``.

    Eligibility itself, and so who may *accept*, is unchanged: this narrows who
    is pushed, not who is allowed.
    """
    return eligible_providers_multi_stmt(service_ids, address_location).where(
        _not_already_answered(booking_id)
    )


# The funnel's stages in order, each paired with the reason logged when it is
# the first to leave nobody.
FUNNEL_REASONS = {
    "covers_services": "no_provider_covers_services",
    "kyc_verified": "none_kyc_verified",
    "account_active": "none_account_active",
    "has_push_token": "none_has_push_token",
    "online": "none_online",
    "in_service_area": "none_in_service_area",
    "has_someone_to_do_it": "none_has_someone_to_do_it",
    "not_already_answered": "all_already_answered",
}


def broadcast_funnel(db, service_ids: Sequence, address_location, booking_id) -> dict[str, int]:
    """How many SPs are still in the running after each eligibility stage, for a
    broadcast that found nobody (SVC-349). Only that path runs it.

    Cumulative and in ``FUNNEL_REASONS`` order: ``counts["online"]`` is the SPs
    covering the whole cart that are also KYC-verified, active, reachable and
    online. The last stage is ``broadcast_recipients_stmt``'s own set, because
    every stage is the same expression that statement uses.

    One query. The cart test (IN + HAVING, as in
    ``eligible_providers_multi_stmt``) is a subquery of provider ids, and
    ``ProviderProfile`` stays un-aliased in the outer FROM: the
    ``has_someone_to_do_it`` EXISTS clauses correlate on it by name, so an alias
    there would silently bind them to the wrong table.

    Takes the sync session: its only caller is the Celery broadcast.
    """
    service_ids = list(service_ids)
    covers = (
        select(ProviderServiceModel.provider_id)
        .where(ProviderServiceModel.service_id.in_(service_ids))
        .group_by(ProviderServiceModel.provider_id)
        .having(func.count(func.distinct(ProviderServiceModel.service_id)) == len(service_ids))
        .subquery()
    )
    stages = [
        *_eligibility_stages(address_location),
        ("not_already_answered", _not_already_answered(booking_id)),
    ]
    # One provider_profiles row per SP, so a plain COUNT is per SP.
    columns = [func.count().label("covers_services")]
    so_far: list = []
    for name, condition in stages:
        so_far.append(condition)
        columns.append(func.count().filter(and_(*so_far)).label(name))
    row = db.execute(
        select(*columns)
        .select_from(ProviderProfile)
        .join(User, User.id == ProviderProfile.user_id)
        .join(covers, covers.c.provider_id == ProviderProfile.user_id)
    ).one()
    return dict(row._mapping)


def funnel_reason(counts: dict[str, int]) -> str:
    """The first stage that left nobody, or ``unknown`` when none did (someone
    became eligible between the broadcast query and the funnel)."""
    for stage, reason in FUNNEL_REASONS.items():
        if counts.get(stage) == 0:
            return reason
    return "unknown"


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

    base_conditions = _base_conditions(address_location)
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
