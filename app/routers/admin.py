"""
Admin router: user management, booking oversight, and KYC approval.
Each route names its roles with ``require_roles`` (SVC-422; table in ADMIN.md).
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.dependencies import authorize_admin_token, get_redis, require_roles
from app.exceptions import BookingNotFound
from app.models.app_version import AppPlatform, AppVersion
from app.models.booking import Booking, BookingStatus, SettlementStatus
from app.models.payment import Payment
from app.models.provider_profile import KYCStatus
from app.models.user import User
from app.schemas.admin_team import DeactivateUserRequest
from app.schemas.booking import (
    BookingListResponse,
    BookingResponse,
    BookingTimelineItem,
    ForceCancelBookingRequest,
)
from app.schemas.payout import (
    PayoutBookingsResponse,
    PayoutDashboardResponse,
    PendingSettlementsResponse,
    ProviderPayoutHistoryResponse,
)
from app.schemas.provider import (
    KYCDocsResponse,
    KYCQueueCountResponse,
    KYCQueueRow,
    KYCRejectionRequest,
    KYCReviewResponse,
    ProKYCDocsResponse,
)
from app.schemas.user import UserResponse
from app.services.admin_audit import audit_actor, audit_admin_action, snapshot
from app.services.admin_team_service import AdminTeamService
from app.services.booking_service import BookingService
from app.services.kyc_queue_service import (
    KYCQueueFilter,
    KYCQueueKind,
    KYCQueueService,
    kyc_queue_filter,
)
from app.services.payout_dashboard_service import PayoutDashboardService
from app.services.payout_service import PayoutService
from app.services.provider_service import ProviderService

router = APIRouter(prefix="/admin", tags=["Admin"])

_admin_support_dep = Depends(require_roles("admin", "support"))
_any_admin_role_dep = Depends(require_roles("admin", "ops", "support"))

# India has no DST, so a fixed offset is exact (same as provider_service._IST).
_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=_IST)


def _disputed_exists(booking_id):
    """EXISTS a charged-back payment for this booking (SVC-134). One rule for the
    list, which correlates on ``Booking.id``, and the detail, which passes an id."""
    return (
        select(Payment.id)
        .where(Payment.booking_id == booking_id, Payment.disputed_at.is_not(None))
        .exists()
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[UserResponse], dependencies=[_admin_support_dep])
async def list_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User).offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.patch("/users/{user_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_user(
    user_id: UUID,
    request: Request,
    body: DeactivateUserRequest | None = None,
    admin: User = Depends(require_roles("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a user. An unknown id is a silent 204. The body (an optional
    ``reason`` for the audit log) may be left out entirely.

    SVC-329: 409 ``USER_HAS_ACTIVE_JOBS`` (ids in ``details.booking_ids``)
    while they hold a job, with nothing changed. Otherwise they also go offline
    everywhere and are removed from any team they're a Pro on, and an
    ``admin_actions`` row is written in the same transaction.
    """
    reason = body.reason if body is not None else None
    if await AdminTeamService(db).deactivate_user(audit_actor(request, admin), user_id, reason):
        await db.commit()


# ---------------------------------------------------------------------------
# Provider KYC (SC-31)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _KYCQueueParams:
    queue_filter: KYCQueueFilter
    kind: KYCQueueKind


def _kyc_queue_params(
    status_filter: str = Query(
        KYCStatus.SUBMITTED.value,
        alias="status",
        description=(
            "Filter: one of the KYCStatus values (submitted/verified/rejected/pending); "
            "'auto_approved', profiles verified by the AI verifier bot in the last 7 days "
            "(SVC-307), for the spot-check tab; or 'all' (SVC-424): submitted + verified + "
            "rejected, never a row with no documents uploaded."
        ),
    ),
    kind: KYCQueueKind = Query(
        "sp",
        description=(
            "SVC-327: which KYC records. 'sp' (default, the queue as it has always "
            "been): service providers. 'pro': team professionals' Aadhaar KYC, "
            "reviewed via /admin/pros/{professional_id}/kyc. 'all': both, merged."
        ),
    ),
) -> _KYCQueueParams:
    """The queue and its count take the same params, so they can't disagree."""
    try:
        return _KYCQueueParams(kyc_queue_filter(status_filter), kind)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unknown status filter {status_filter!r}. Expected one of "
                f"{[s.value for s in KYCStatus]}, 'auto_approved' or 'all'."
            ),
        ) from exc


@router.get(
    "/providers/kyc/pending",
    response_model=list[KYCQueueRow],
    dependencies=[_any_admin_role_dep],
)
async def list_kyc_queue(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    params: _KYCQueueParams = Depends(_kyc_queue_params),
    db: AsyncSession = Depends(get_db),
):
    """List KYC records by status, leaving out deleted accounts and removed Pros.

    One route for every tab keeps the pagination and response shape in one place.
    Submitted (and pending) sort oldest submitted first, so the admin drains the
    queue FIFO. Rejected, verified and auto_approved sort newest review first,
    which is the useful order when hunting a past decision. ``all`` sorts newest
    submitted first.
    """
    return await KYCQueueService(db).list_rows(params.queue_filter, params.kind, page, per_page)


@router.get(
    "/providers/kyc/pending/count",
    response_model=KYCQueueCountResponse,
    dependencies=[_any_admin_role_dep],
)
async def count_kyc_queue(
    params: _KYCQueueParams = Depends(_kyc_queue_params),
    db: AsyncSession = Depends(get_db),
):
    """How many rows ``GET /providers/kyc/pending`` has across all pages, for the
    console's sidebar badge (SVC-424). A badge that includes team Pros must ask
    for ``kind=all``; the default is ``sp``, the same as the queue."""
    count = await KYCQueueService(db).count(params.queue_filter, params.kind)
    return KYCQueueCountResponse(count=count)


@router.get(
    "/providers/{user_id}/kyc",
    response_model=KYCDocsResponse,
    dependencies=[_any_admin_role_dep],
)
async def get_provider_kyc_review(user_id: UUID, db: AsyncSession = Depends(get_db)):
    """
    Full KYC review payload for an admin: profile metadata + presigned URLs for
    the uploaded docs (1h TTL). Powers the admin KYC review page (SVC-90).
    """
    return await ProviderService(db).get_kyc_review_payload(user_id)


def _push_kyc_rejected(user_id: UUID) -> None:
    """Enqueue the rejection push (SVC-444). Call only after the commit, so a worker
    can't run before the rejection is saved. Best effort: the rejection and its audit
    row are already saved, and a broker problem shouldn't report them as failed.
    """
    # Imported here to keep FastAPI cold start off the Celery broker path.
    import structlog

    from app.tasks.notification_tasks import send_kyc_rejected_notification

    try:
        send_kyc_rejected_notification.delay(user_id=str(user_id))
    except Exception as exc:  # noqa: BLE001
        structlog.get_logger(__name__).error(
            "kyc.rejected_push_enqueue_failed", user_id=str(user_id), error=str(exc)
        )


@router.post("/providers/{user_id}/kyc/verify", response_model=KYCReviewResponse)
async def verify_provider_kyc(
    user_id: UUID,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    service = ProviderService(db)
    before = snapshot("provider_profile", await service.get_profile(user_id))
    profile = await service.verify_kyc(user_id)
    await audit_admin_action(
        db,
        audit_actor(request, admin),
        action="kyc.approve",
        target_type="provider",
        target_id=user_id,
        sp_user_id=user_id,
        before=before,
        after=snapshot("provider_profile", profile),
    )
    await db.commit()
    return profile


@router.post("/providers/{user_id}/kyc/reject", response_model=KYCReviewResponse)
async def reject_provider_kyc(
    user_id: UUID,
    body: KYCRejectionRequest,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    service = ProviderService(db)
    before = snapshot("provider_profile", await service.get_profile(user_id))
    profile = await service.reject_kyc(user_id, reason=body.reason)
    await audit_admin_action(
        db,
        audit_actor(request, admin),
        action="kyc.reject",
        target_type="provider",
        target_id=user_id,
        reason=body.reason,
        sp_user_id=user_id,
        before=before,
        after=snapshot("provider_profile", profile),
    )
    await db.commit()
    _push_kyc_rejected(user_id)
    return profile


# ---------------------------------------------------------------------------
# Team Pro KYC (SVC-327): the same review, on a professionals row
# ---------------------------------------------------------------------------
#
# Addressed by professional id, not user id: the SP routes above stay exactly as
# the admin frontend uses them, and a Pro's KYC can never land on an SP row.
# Pros-for-self 404 here; their KYC is their SP's.


@router.get(
    "/pros/{professional_id}/kyc",
    response_model=ProKYCDocsResponse,
    dependencies=[_any_admin_role_dep],
)
async def get_pro_kyc_review(professional_id: UUID, db: AsyncSession = Depends(get_db)):
    """A team Pro's KYC review payload, with which SP they work for."""
    return await ProviderService(db).get_pro_kyc_review_payload(professional_id)


@router.post("/pros/{professional_id}/kyc/verify", response_model=KYCReviewResponse)
async def verify_pro_kyc(
    professional_id: UUID,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    service = ProviderService(db)
    before = snapshot("professional", await service.get_team_pro(professional_id))
    pro = await service.verify_pro_kyc(professional_id)
    await audit_admin_action(
        db,
        audit_actor(request, admin),
        action="kyc.approve",
        target_type="professional",
        target_id=professional_id,
        sp_user_id=pro.service_provider_id,
        before=before,
        after=snapshot("professional", pro),
    )
    await db.commit()
    return pro


@router.post("/pros/{professional_id}/kyc/reject", response_model=KYCReviewResponse)
async def reject_pro_kyc(
    professional_id: UUID,
    body: KYCRejectionRequest,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    service = ProviderService(db)
    before = snapshot("professional", await service.get_team_pro(professional_id))
    pro = await service.reject_pro_kyc(professional_id, reason=body.reason)
    await audit_admin_action(
        db,
        audit_actor(request, admin),
        action="kyc.reject",
        target_type="professional",
        target_id=professional_id,
        reason=body.reason,
        sp_user_id=pro.service_provider_id,
        before=before,
        after=snapshot("professional", pro),
    )
    await db.commit()
    # A removed Pro has left the team, though this route can still act on them.
    if pro.is_active:
        _push_kyc_rejected(pro.user_id)
    return pro


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------


@router.get("/bookings", response_model=BookingListResponse, dependencies=[_any_admin_role_dep])
async def list_all_bookings(
    booking_status: str | None = Query(None, alias="status"),
    disputed: bool | None = Query(
        None, description="Filter to bookings with (or without) a charged-back payment"
    ),
    created_from: date | None = Query(
        None, description="Created on or after this day, in IST (YYYY-MM-DD)"
    ),
    created_to: date | None = Query(
        None, description="Created on or before this day, in IST (YYYY-MM-DD)"
    ),
    consumer_phone: str | None = Query(
        None, description="Customer phone; any format, the last 10 digits must match exactly"
    ),
    provider_name: str | None = Query(
        None, description="Case-insensitive substring of the SP's name"
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    # A booking can have several payment rows (failed attempts + the captured one), so
    # "is this booking disputed" is an EXISTS over its payments rather than a join —
    # a join would duplicate booking rows once per payment (SVC-134). The phone and
    # name filters below are EXISTS for the same reason (SVC-425).
    disputed_exists = _disputed_exists(Booking.id)

    # Every filter goes into both the page query and the count query, so `total`
    # always describes the rows the pages walk through.
    filters = []

    if booking_status:
        try:
            filters.append(Booking.status == BookingStatus(booking_status))
        except ValueError:
            # Unknown status is ignored, not a 422: the legacy screen relies on it.
            pass

    if disputed is not None:
        filters.append(disputed_exists if disputed else ~disputed_exists)

    # SVC-425: dates are IST calendar days, both ends inclusive, so `created_to`
    # is "before the next IST midnight".
    if created_from and created_to and created_from > created_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="created_from must be on or before created_to",
        )
    if created_from:
        filters.append(Booking.created_at >= _ist_midnight(created_from))
    if created_to:
        filters.append(Booking.created_at < _ist_midnight(created_to + timedelta(days=1)))

    if consumer_phone is not None:
        digits = re.sub(r"\D", "", consumer_phone)
        if len(digits) < 10:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="consumer_phone needs at least 10 digits",
            )
        # users.phone is stored as +91XXXXXXXXXX, so an exact match keeps the index.
        filters.append(
            select(User.id)
            .where(User.id == Booking.consumer_id, User.phone == f"+91{digits[-10:]}")
            .exists()
        )

    name = (provider_name or "").strip()
    if name:
        # The admin types a name, not a pattern: % and _ match themselves.
        pattern = "%" + re.sub(r"([\\%_])", r"\\\1", name) + "%"
        filters.append(
            select(User.id)
            .where(User.id == Booking.provider_id, User.name.ilike(pattern, escape="\\"))
            .exists()
        )

    stmt = select(Booking, disputed_exists.label("is_disputed")).where(*filters)
    count_stmt = select(func.count()).select_from(Booking).where(*filters)

    total = (await db.execute(count_stmt)).scalar_one()
    # Newest first; `id` breaks created_at ties so a row never shows on two pages
    # or on none (SVC-425).
    stmt = (
        stmt.order_by(Booking.created_at.desc(), Booking.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    result = await db.execute(stmt)
    rows = list(result.all())
    bookings = [booking for booking, _ in rows]

    # Hydrated even though `for_viewer(viewer_id=None)` then drops the address: the
    # schema reads `Booking.consumer_address` while validating, and that relationship
    # is `lazy="raise_on_sql"` (SVC-170). Withholding by simply not loading would
    # work today and break the moment the redaction rule changes — who may see an
    # address is one decision, and it lives next to the doorstep-PIN rule it sits
    # beside, not in the shape of this query. The single-booking route below is
    # the one that serves it. Same principle for items: attached because the
    # response schema is going to look for them, not because an admin needs the
    # per-item breakdown right now.
    svc = BookingService(db)
    await svc.attach_addresses(bookings)
    await svc.attach_items(bookings)
    # Same reasoning as `attach_items` above: hydrated because the shared
    # response schema is going to read it, not because this listing needs a
    # per-booking transition log (SVC-248).
    await svc.attach_status_logs(bookings)
    await svc.attach_professionals(bookings)

    # SVC-299: the admin bookings page renders "consumer phone" and "provider
    # name" columns, both of which live on `users` and neither of which is on
    # `BookingResponse` by default. Fetching them here in one keyed SELECT
    # rather than lazy-loading via the ORM relationships — the list is up to
    # 200 rows and each row has two party pointers, which would otherwise
    # fire 400 round-trips.
    party_ids = {b.consumer_id for b in bookings}
    party_ids.update(b.provider_id for b in bookings if b.provider_id is not None)
    users_by_id: dict[UUID, User] = {}
    if party_ids:
        user_rows = await db.execute(select(User).where(User.id.in_(party_ids)))
        users_by_id = {u.id: u for u in user_rows.scalars()}

    return BookingListResponse(
        # No viewer identity to compare against — an admin is never the booking's
        # consumer, so no row here carries its doorstep PIN. This listing spans
        # every booking in the system, live ones included (SVC-169). For the same
        # reason no row carries a consumer address either, though the reasoning
        # differs — bulk PII minimisation rather than secrecy. Both rules live in
        # `for_viewer`; see its docstring (SVC-170). `is_disputed` is stitched in
        # from the EXISTS in the query (SVC-134). `consumer_phone` /
        # `provider_name` are stitched from the batched user lookup above (SVC-299).
        bookings=[
            BookingResponse.for_viewer(booking, viewer_id=None).model_copy(
                update={
                    "is_disputed": is_disputed,
                    "consumer_phone": (
                        users_by_id[booking.consumer_id].phone
                        if booking.consumer_id in users_by_id
                        else None
                    ),
                    "provider_name": (
                        users_by_id[booking.provider_id].name
                        if booking.provider_id and booking.provider_id in users_by_id
                        else None
                    ),
                }
            )
            for booking, is_disputed in rows
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.post(
    "/bookings/{booking_id}/cancel",
    response_model=BookingResponse,
    responses={
        404: {"description": "No such booking"},
        409: {"description": "Booking is already in a terminal state"},
    },
)
async def force_cancel_booking(
    booking_id: UUID,
    body: ForceCancelBookingRequest,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a booking on a customer's behalf (SVC-299).

    Support-only escape hatch — bypasses the customer/provider valid-transition
    rules so a stuck booking (partner ghosted en_route, wrong service dispatched,
    obvious fraud) can be closed without opening a psql shell.

    Refund inherits the consumer cancel policy in
    ``BookingService._apply_cancellation_policy`` (see there): a captured
    online payment triggers a Razorpay refund minus the ``ACCEPTED`` / ``EN_ROUTE``
    cancellation fee; PENDING and cash bookings are pure state changes with no
    money movement. IN_PROGRESS is intentionally *not* auto-refunded — the
    job has started, so support is expected to refund manually via the
    payments surface after diagnosing.

    ``booking_status_logs`` gets a row with ``changed_by=admin.id`` and the
    reason in the ``note`` column; ``booking.cancellation_reason`` is stamped
    with the same string. Both parties get the standard cancelled push
    (SVC-278, SVC-348) with the admin as actor so the notification tone matches
    consumer-initiated cancels — the customer/provider hears "your booking
    was cancelled", not the internal detail that support did it.
    """
    booking = await BookingService(db).force_cancel(
        booking_id=booking_id, actor=audit_actor(request, admin), reason=body.reason
    )
    # Mirror the consumer-cancel push. Both consumer and provider need to know:
    # the admin's id is neither party's, so the worker pushes both (SVC-348),
    # plus a team Pro on the job. Import here
    # rather than at module top for the same reason ``pay_out_provider`` does —
    # keeps FastAPI cold start off the Celery broker path.
    from app.tasks.notification_tasks import send_booking_notification

    send_booking_notification.delay(
        booking_id=str(booking.id), event="cancelled", actor_id=str(admin.id)
    )
    return await _for_admin_with_parties(db, booking)


@router.get(
    "/bookings/{booking_id}",
    response_model=BookingResponse,
    dependencies=[_any_admin_role_dep],
    responses={404: {"description": "No such booking"}},
)
async def get_any_booking(booking_id: UUID, db: AsyncSession = Depends(get_db)):
    """Fetch any one booking by id, whoever it belongs to.

    Distinct from ``GET /bookings/{id}``, which admits only that booking's own
    consumer or assigned provider — an admin is neither, so the user-facing route
    refuses them and there was previously no way for support to look at a single
    booking at all.

    This one carries the consumer's address, which the bulk listing above
    deliberately does not (SVC-170). That is the whole distinction: one booking
    asked for by id is a support question about a specific job, where a page of
    every booking on the platform is a standing export of home addresses. It
    still withholds the doorstep PIN — see ``BookingResponse.for_admin``.
    """
    booking = await db.get(Booking, booking_id)
    if booking is None:
        raise BookingNotFound(booking_id)

    svc = BookingService(db)
    await svc.attach_addresses([booking])
    await svc.attach_items([booking])
    await svc.attach_status_logs([booking])
    await svc.attach_professionals([booking])
    return await _for_admin_with_parties(db, booking)


@router.get(
    "/bookings/{booking_id}/timeline",
    response_model=list[BookingTimelineItem],
    dependencies=[_any_admin_role_dep],
    responses={404: {"description": "No such booking"}},
)
async def get_booking_timeline(booking_id: UUID, db: AsyncSession = Depends(get_db)):
    """Everything that happened to one booking, oldest first (SVC-333): status
    changes (``type: "status"``) merged with changes of its Pro (``type:
    "assignment"``: claimed, repicked, declined, released, provider_cancelled).

    Answers "who was actually sent, by whom, and when" in a dispute. Its own
    route rather than more fields on ``GET /admin/bookings/{id}``, which reuses
    the consumer's ``BookingResponse``: Pro and actor names must never reach that
    shape.
    """
    return await BookingService(db).admin_timeline(booking_id)


async def _for_admin_with_parties(db: AsyncSession, booking: Booking) -> BookingResponse:
    """Serialise one booking for the admin side sheet + hydrate the party fields.

    ``BookingResponse.for_admin`` widens the base response with the address
    (SVC-170); this helper widens it further with ``consumer_phone`` and
    ``provider_name`` (SVC-299) so the admin sheet — and the response we hand
    back from POST /admin/bookings/{id}/cancel — can render "cancelled
    booking for +91XXXXXXXXXX" without a follow-up round trip. Assumes
    the ORM relationships (``consumer_address``, ``items``, ``status_logs``)
    are already hydrated by the caller.

    SVC-425 adds what the admin detail panel shows and no other caller gets:
    ``is_disputed`` (the list's rule, which ``for_viewer`` leaves False),
    ``cancellation_reason`` and the settlement state.
    """
    ids: set[UUID] = {booking.consumer_id}
    if booking.provider_id is not None:
        ids.add(booking.provider_id)
    rows = await db.execute(select(User).where(User.id.in_(ids)))
    users_by_id = {u.id: u for u in rows.scalars()}
    consumer = users_by_id.get(booking.consumer_id)
    provider = users_by_id.get(booking.provider_id) if booking.provider_id else None
    is_disputed = (await db.execute(select(_disputed_exists(booking.id)))).scalar_one()
    return BookingResponse.for_admin(booking).model_copy(
        update={
            "consumer_phone": consumer.phone if consumer else None,
            "provider_name": provider.name if provider else None,
            "is_disputed": is_disputed,
            "cancellation_reason": booking.cancellation_reason,
            "settlement_status": (
                booking.settlement_status.value if booking.settlement_status else None
            ),
            "settled_at": booking.settled_at,
        }
    )


# ---------------------------------------------------------------------------
# Provider payouts
# ---------------------------------------------------------------------------


# Declared before the /providers/{user_id}/... routes: "pending-settlements" is a
# literal segment, and if a GET /providers/{user_id} is ever added below, FastAPI
# would otherwise match this path as a user id.
@router.get(
    "/providers/pending-settlements",
    response_model=PendingSettlementsResponse,
    dependencies=[_any_admin_role_dep],
)
async def list_pending_settlements(db: AsyncSession = Depends(get_db)):
    """Providers awaiting settlement, with the amount and booking count owed.

    The discovery half of the payout flow: ``POST /providers/{user_id}/payout`` can
    only be triggered for a provider the admin already knows, so this answers "who
    do we owe, and how much" for the admin payouts screen. Read-only, so it takes
    the role guard rather than injecting the admin (nothing to audit).
    """
    return await PayoutService(db).list_pending_settlements()


@router.get(
    "/providers/{user_id}/payouts",
    response_model=ProviderPayoutHistoryResponse,
    dependencies=[_any_admin_role_dep],
)
async def list_provider_payout_history(
    user_id: UUID,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Payout audit trail for a single provider, newest-first (SVC-300).

    Paired with the pending-settlements list: the admin expands a row to see
    what has already been paid before triggering a fresh payout, closing the
    "did I pay this guy last week or not" gap that used to invite double
    dispatch. Read-only projection over ``payout_audits`` — no state changes.
    """
    return await PayoutService(db).list_provider_payout_history(
        user_id, page=page, per_page=per_page
    )


@router.post("/providers/{user_id}/payout", status_code=status.HTTP_202_ACCEPTED)
async def pay_out_provider(
    user_id: UUID,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Enqueue a RazorpayX payout for a provider's completed-but-unsettled bookings.

    Returns 202 with a summary of what will be paid and the Celery task id. The
    payout itself runs asynchronously (SVC-188 / G30): before this, the endpoint
    awaited a 30-90s RazorpayX HTTP call while holding a DB connection and a
    ``SELECT … FOR UPDATE`` lock on the provider's pending bookings, which
    puts pool pressure on the same workers serving consumer traffic during a
    RazorpayX slowdown.

    Before queuing, ``PayoutService.preflight_payout`` runs every check the task
    would refuse on (KYC, payout details, nothing pending, an unresolved
    attempt, wallet ≤ 0, the batch cap) with the same messages, read-only and
    lock-free. A refusal is a 402 and nothing is queued. Before this only the cap
    was checked here, and the rest came back as a 202 "queued" the task then
    rolled back without a trace. The task runs the full flow (record audit → lock
    rows → RazorpayX → resolve audit → update bookings → drain pushes) through
    the same ``PayoutService.create_provider_payout``, which re-runs the checks
    under the row lock and writes a ``refused`` audit row if one fails there.

    Every attempt is still recorded in ``payout_audits`` before the RazorpayX
    call fires; the admin payouts screen surfaces the outcome from there, and
    ``reconcile_payouts`` (nightly) resolves any breadcrumb whose task
    process died mid-flight.

    SVC-423: one ``admin_actions`` row per accepted request, committed before the
    enqueue, because an unaudited money move is worse than a row for a failed
    enqueue. A refused batch writes none, and the task writes none: each
    attempt's outcome stays in ``payout_audits``. That's why the preflight runs
    first: a 402/404 raises before the row is written.
    """
    from app.tasks.payment_tasks import execute_provider_payout

    # The 202 body's amount is what the task will send: min(wallet, batch sum).
    # An id with no provider profile is a 404 here, so past this line user_id is
    # always a valid sp_user_id (an FK to users).
    plan = await PayoutService(db).preflight_payout(user_id)

    await audit_admin_action(
        db,
        audit_actor(request, admin),
        action="payout.enqueue",
        target_type="provider",
        target_id=user_id,
        sp_user_id=user_id,
        details={
            "amount_paise": plan["amount_paise"],
            "booking_count": plan["booking_count"],
        },
    )
    await db.commit()

    task = execute_provider_payout.delay(str(user_id), str(admin.id))
    return {
        "status": "queued",
        "task_id": task.id,
        "amount_paise": plan["amount_paise"],
        "booking_count": plan["booking_count"],
    }


@router.post("/providers/{user_id}/payout/requeue-failed", status_code=status.HTTP_200_OK)
async def requeue_failed_settlements(
    user_id: UUID,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Reset a provider's FAILED settlements to PENDING so they can be paid again.

    Manual recovery for payouts that failed (bad bank details, KYC hold, ...): once
    the cause is fixed, an admin re-queues the failed settlements, which then become
    eligible for the next payout run. The triggering admin is recorded in the audit
    trail.
    """
    result = await PayoutService(db).requeue_settlements(
        user_id, admin.id, from_status=SettlementStatus.FAILED
    )
    await audit_admin_action(
        db,
        audit_actor(request, admin),
        action="payout.requeue_failed",
        target_type="provider",
        target_id=user_id,
        sp_user_id=user_id,
        before={"settlement_status": SettlementStatus.FAILED},
        after={"settlement_status": SettlementStatus.PENDING},
        details={"booking_count": result["requeued"]},
    )
    return result


@router.post("/providers/{user_id}/payout/requeue-reversed", status_code=status.HTTP_200_OK)
async def requeue_reversed_settlements(
    user_id: UUID,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Reset a provider's REVERSED settlements to PENDING so they can be paid again.

    A reversal means the payout reached the provider and was then clawed back —
    often a KYC/compliance signal, not a simple retry. Only use this once you've
    confirmed the provider is genuinely still owed and the underlying cause is
    fixed; otherwise the re-payment will likely bounce again. Admin-only and
    recorded in the audit trail.
    """
    result = await PayoutService(db).requeue_settlements(
        user_id, admin.id, from_status=SettlementStatus.REVERSED
    )
    await audit_admin_action(
        db,
        audit_actor(request, admin),
        action="payout.requeue_reversed",
        target_type="provider",
        target_id=user_id,
        sp_user_id=user_id,
        before={"settlement_status": SettlementStatus.REVERSED},
        after={"settlement_status": SettlementStatus.PENDING},
        details={"booking_count": result["requeued"]},
    )
    return result


# The 3 dashboard reads (SVC-426). Read-only, so not audited; open to every admin
# role, since support reads payouts too.
_EXPORT_MAX_DAYS = 366


@router.get(
    "/payouts/providers",
    response_model=PayoutDashboardResponse,
    dependencies=[_any_admin_role_dep],
)
async def list_payout_providers(
    status_: SettlementStatus | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """One row per SP with any payout-scope booking (completed, PAID, not cash).

    Count + paise per settlement status, ``earned_paise`` across all five, and
    ``payable_paise``, the net a payout would send now. ``status`` keeps SPs with a
    booking in that bucket; omit it for all. Largest pending amount first.
    """
    return await PayoutDashboardService(db).list_providers(
        status=status_, page=page, per_page=per_page
    )


@router.get(
    "/providers/{user_id}/payout-bookings",
    response_model=PayoutBookingsResponse,
    dependencies=[_any_admin_role_dep],
)
async def list_provider_payout_bookings(
    user_id: UUID,
    settlement_status: SettlementStatus | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """The bookings behind an SP's payout figures, newest completion first, with the
    price, commission and payout split each one contributed."""
    return await PayoutDashboardService(db).list_bookings(
        user_id, settlement_status=settlement_status, page=page, per_page=per_page
    )


@router.get(
    "/payouts/export.csv",
    dependencies=[_any_admin_role_dep],
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}, "description": "Payout CSV"}},
)
async def export_payouts_csv(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Payout-scope bookings completed on IST days ``from``..``to`` inclusive, as CSV
    for the accounting import. At most 366 days. Cash bookings are left out."""
    if from_ > to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "`from` is after `to`")
    if (to - from_).days + 1 > _EXPORT_MAX_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Range is longer than {_EXPORT_MAX_DAYS} days",
        )
    body = await PayoutDashboardService(db).export_csv(from_, to)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="payouts_{from_.isoformat()}_{to.isoformat()}.csv"'
            )
        },
    )


# ---------------------------------------------------------------------------
# App version releases (SVC-227)
# ---------------------------------------------------------------------------


async def _require_admin_or_release_token(
    request: Request,
    response: Response,
    x_internal_release_token: str | None = Header(default=None),
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> User | None:
    """Alt-auth for the release endpoint (SVC-227.2). Returns the admin, or None
    for the CI token, where no admin acted.

    Accepts EITHER:
      - `X-Internal-Release-Token` header matching
        ``settings.INTERNAL_RELEASE_TOKEN`` (CI path — `release-apk.yml`), OR
      - Standard admin JWT (human path — same as every other admin route).

    Header path is scoped to THIS ONE endpoint — nothing else in the
    app accepts it. `hmac.compare_digest` to defeat timing attacks.
    Empty ``settings.INTERNAL_RELEASE_TOKEN`` disables the header path
    entirely; the endpoint then behaves like every other admin route.

    Written as one dep (rather than two Depends parameters) because
    FastAPI runs deps eagerly — a `require_roles("admin")` dep alongside
    this one would hard-401 on missing JWT even when the header path
    already succeeded. The `HTTPBearer(auto_error=False)` in this
    signature is the trick that lets us decide the branch ourselves.

    ``request.state.release_auth`` records which path won ("token" or
    "jwt") so a Sentry event / access log can distinguish CI-driven
    releases from human ones — CI-driven releases lose the "who"
    otherwise.
    """
    import hmac

    settings = get_settings()
    if (
        x_internal_release_token
        and settings.INTERNAL_RELEASE_TOKEN
        and hmac.compare_digest(x_internal_release_token, settings.INTERNAL_RELEASE_TOKEN)
    ):
        request.state.release_auth = "token"
        return None

    # JWT path — not the standard `require_roles("admin")` dep, so we can
    # return a specific 401 when neither auth method was provided at all
    # (vs 403 for a wrong-role JWT).
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Auth required: either admin JWT via `Authorization: Bearer <jwt>` "
                "or CI token via `X-Internal-Release-Token`."
            ),
        )

    admin = await authorize_admin_token(credentials.credentials, ("admin",), response, db, redis)
    request.state.release_auth = "jwt"
    return admin


class PublishReleaseRequest(BaseModel):
    """Bump the current version + optionally push a nudge to every device.

    ``version`` is the customer-facing SemVer of the new build. The
    matching APK must already be at ``app_versions.url`` (usually
    ``s3://eazyserv-downloads/apk/latest.apk``, since ``release-apk.yml``
    always overwrites that key). If they don't match, users will nudge
    themselves onto a version we haven't shipped yet.

    ``min_supported_version``, when passed, atomically hard-blocks anyone
    below it — the SVC-162 emergency lever. Leave it unset to keep the
    current threshold.

    ``notify`` = true enqueues the FCM broadcast (``broadcast_update_available``).
    Set to false when releasing a build we don't want to interrupt users
    over (e.g. a silent metrics fix — the poll on next foreground will
    still surface the nudge).
    """

    version: str = Field(..., min_length=1, max_length=32)
    min_supported_version: str | None = Field(None, max_length=32)
    release_notes: str | None = Field(None, max_length=2000)
    url: str | None = Field(
        None,
        max_length=500,
        description=(
            "New APK URL. Usually stays the same because release-apk.yml "
            "overwrites apk/latest.apk in place."
        ),
    )
    notify: bool = Field(
        default=True,
        description="Fire the FCM broadcast so users learn about the update without opening the app.",
    )


class PublishReleaseResponse(BaseModel):
    platform: str
    version: str
    min_supported_version: str
    notified: bool
    task_id: str | None = Field(
        None,
        description="Celery task id for the FCM broadcast (only when notify=true).",
    )


@router.post(
    "/app-versions/{platform}/release",
    response_model=PublishReleaseResponse,
)
async def publish_app_release(
    platform: str,
    request: PublishReleaseRequest,
    http_request: Request,
    admin: User | None = Depends(_require_admin_or_release_token),
    db: AsyncSession = Depends(get_db),
):
    """One-shot publish: bump `app_versions.{platform}` + (optionally) push
    a "New version available" notification to every consumer with an
    FCM token.

    The two-step manual ritual — SQL update + separate broadcast call —
    is easy to get wrong (bump the version without notifying, or notify
    without bumping and users tap through to nothing new). This endpoint
    does both atomically: the DB write happens first (so the poll-on-
    foreground path immediately reflects the new version even if the
    broadcast fails to enqueue), then the Celery task is fired.

    Not the same as ``release-apk.yml`` — that builds and uploads the
    APK. This endpoint tells the running fleet about it after the APK
    is in place. The current publish ritual is:
      1. `gh workflow run release-apk.yml -f version=X.Y.Z`
      2. `POST /admin/app-versions/android/release {"version": "X.Y.Z"}`
    """
    try:
        platform_enum = AppPlatform(platform)
    except ValueError as exc:
        raise HTTPException(400, f"Unknown platform '{platform}'") from exc

    row = (
        await db.execute(select(AppVersion).where(AppVersion.platform == platform_enum))
    ).scalar_one_or_none()
    if row is None:
        # Seed row missing — treat as a deployment bug rather than
        # silently creating one on this hot path.
        raise HTTPException(
            503,
            f"No app_versions row for platform={platform}. Missing seed migration?",
        )

    before = snapshot("app_version", row)
    row.version = request.version
    if request.min_supported_version is not None:
        row.min_supported_version = request.min_supported_version
    if request.release_notes is not None:
        row.release_notes = request.release_notes
    if request.url is not None:
        row.url = request.url
    # released_at is the "when did we cut this" timestamp shown in the
    # nudge — bump it whenever we publish, even if only release_notes
    # changed, so the client can differentiate a re-published note from
    # a fresh release. See app_versions.released_at.
    row.released_at = datetime.now(UTC)
    await db.flush()

    # SVC-423: the CI token writes no row, because no admin acted. The log line
    # below is its record.
    if admin is not None:
        await audit_admin_action(
            db,
            audit_actor(http_request, admin),
            action="app_version.release",
            target_type="app_version",
            target_id=platform,
            before=before,
            after=snapshot("app_version", row),
            details={"notify": request.notify},
        )

    # Log auth path so we can tell CI vs human releases apart in Sentry /
    # CloudWatch — the alt-auth path has no user object to attribute it to.
    import structlog

    structlog.get_logger(__name__).info(
        "app_version_published",
        platform=platform,
        version=request.version,
        notify=request.notify,
        release_auth=getattr(http_request.state, "release_auth", "unknown"),
    )

    task_id: str | None = None
    if request.notify:
        # Import here rather than at module top: the Celery app pulls in
        # broker config, and importing it at admin-router import time
        # would slow the FastAPI cold start for every request path.
        from app.tasks.notification_tasks import broadcast_update_available

        result = broadcast_update_available.delay(version=request.version, platform=platform)
        task_id = result.id

    return PublishReleaseResponse(
        platform=platform,
        version=row.version,
        min_supported_version=row.min_supported_version,
        notified=request.notify,
        task_id=task_id,
    )
