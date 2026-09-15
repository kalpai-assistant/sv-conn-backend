"""
Admin router: user management, booking oversight, and KYC approval.
All endpoints require the "admin" role.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_role
from app.exceptions import BookingNotFound
from app.models.app_version import AppPlatform, AppVersion
from app.models.booking import Booking, BookingStatus, SettlementStatus
from app.models.payment import Payment
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.user import User
from app.schemas.booking import BookingListResponse, BookingResponse, ForceCancelBookingRequest
from app.schemas.payout import PendingSettlementsResponse
from app.schemas.provider import (
    KYCDocsResponse,
    KYCQueueRow,
    KYCRejectionRequest,
    KYCReviewResponse,
)
from app.schemas.user import UserResponse
from app.services.booking_service import BookingService
from app.services.payout_service import PayoutService
from app.services.provider_service import ProviderService

router = APIRouter(prefix="/admin", tags=["Admin"])

_admin_dep = Depends(require_role("admin"))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[UserResponse], dependencies=[_admin_dep])
async def list_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User).offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.patch(
    "/users/{user_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_admin_dep]
)
async def deactivate_user(user_id: UUID, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if user:
        user.is_active = False
        await db.commit()


# ---------------------------------------------------------------------------
# Provider KYC (SC-31)
# ---------------------------------------------------------------------------


@router.get("/providers/kyc/pending", response_model=list[KYCQueueRow], dependencies=[_admin_dep])
async def list_kyc_queue(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    kyc_status_filter: KYCStatus = Query(
        KYCStatus.SUBMITTED,
        alias="status",
        description="Filter by KYC status. Default 'submitted' preserves the pre-SVC-298 shape.",
    ),
    db: AsyncSession = Depends(get_db),
):
    """List providers filtered by KYC status.

    SVC-298: same endpoint answers both the pending review queue (default) and
    the rejected view — one query param instead of a sibling route keeps the
    pagination + response shape DRY. Rejected rows sort by most-recent review
    first (that's the useful order when hunting a specific past rejection);
    submitted rows keep oldest-first so the admin drains the queue FIFO.
    """
    order_col = (
        ProviderProfile.kyc_reviewed_at.desc()
        if kyc_status_filter == KYCStatus.REJECTED
        else ProviderProfile.kyc_submitted_at.asc()
    )
    stmt = (
        select(User, ProviderProfile)
        .join(ProviderProfile, ProviderProfile.user_id == User.id)
        .where(ProviderProfile.kyc_status == kyc_status_filter)
        .order_by(order_col)
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    result = await db.execute(stmt)
    return [
        KYCQueueRow(
            id=user.id,
            phone=user.phone,
            name=user.name,
            email=user.email,
            kyc_submitted_at=profile.kyc_submitted_at,
            kyc_reviewed_at=profile.kyc_reviewed_at,
            kyc_rejection_reason=profile.kyc_rejection_reason,
        )
        for user, profile in result.all()
    ]


@router.get(
    "/providers/{user_id}/kyc",
    response_model=KYCDocsResponse,
    dependencies=[_admin_dep],
)
async def get_provider_kyc_review(user_id: UUID, db: AsyncSession = Depends(get_db)):
    """
    Full KYC review payload for an admin: profile metadata + presigned URLs for
    the uploaded docs (1h TTL). Powers the admin KYC review page (SVC-90).
    """
    return await ProviderService(db).get_kyc_review_payload(user_id)


@router.post(
    "/providers/{user_id}/kyc/verify",
    response_model=KYCReviewResponse,
    dependencies=[_admin_dep],
)
async def verify_provider_kyc(user_id: UUID, db: AsyncSession = Depends(get_db)):
    profile = await ProviderService(db).verify_kyc(user_id)
    await db.commit()
    return profile


@router.post(
    "/providers/{user_id}/kyc/reject",
    response_model=KYCReviewResponse,
    dependencies=[_admin_dep],
)
async def reject_provider_kyc(
    user_id: UUID,
    body: KYCRejectionRequest,
    db: AsyncSession = Depends(get_db),
):
    profile = await ProviderService(db).reject_kyc(user_id, reason=body.reason)
    await db.commit()
    return profile


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------


@router.get("/bookings", response_model=BookingListResponse, dependencies=[_admin_dep])
async def list_all_bookings(
    booking_status: str | None = Query(None, alias="status"),
    disputed: bool | None = Query(
        None, description="Filter to bookings with (or without) a charged-back payment"
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    # A booking can have several payment rows (failed attempts + the captured one), so
    # "is this booking disputed" is an EXISTS over its payments rather than a join —
    # a join would duplicate booking rows once per payment (SVC-134).
    disputed_exists = (
        select(Payment.id)
        .where(Payment.booking_id == Booking.id, Payment.disputed_at.is_not(None))
        .exists()
    )

    stmt = select(Booking, disputed_exists.label("is_disputed"))
    count_stmt = select(func.count()).select_from(Booking)

    if booking_status:
        try:
            status_enum = BookingStatus(booking_status)
            stmt = stmt.where(Booking.status == status_enum)
            count_stmt = count_stmt.where(Booking.status == status_enum)
        except ValueError:
            pass

    if disputed is not None:
        clause = disputed_exists if disputed else ~disputed_exists
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)

    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
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
    admin: User = Depends(require_role("admin")),
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
    with the same string. The other party gets the standard cancelled push
    (SVC-278) with the admin as actor so the notification tone matches
    consumer-initiated cancels — the customer/provider hears "your booking
    was cancelled", not the internal detail that support did it.
    """
    booking = await BookingService(db).force_cancel(
        booking_id=booking_id, admin_id=admin.id, reason=body.reason
    )
    # Mirror the consumer-cancel push. Both consumer and provider need to know,
    # and passing the admin's id keeps the notification worker's actor→recipient
    # routing coherent: whoever the admin is not is who gets pushed. Import here
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
    dependencies=[_admin_dep],
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
    return await _for_admin_with_parties(db, booking)


async def _for_admin_with_parties(db: AsyncSession, booking: Booking) -> BookingResponse:
    """Serialise one booking for the admin side sheet + hydrate the party fields.

    ``BookingResponse.for_admin`` widens the base response with the address
    (SVC-170); this helper widens it further with ``consumer_phone`` and
    ``provider_name`` (SVC-299) so the admin sheet — and the response we hand
    back from POST /admin/bookings/{id}/cancel — can render "cancelled
    booking for +91XXXXXXXXXX" without a follow-up round trip. Assumes
    the ORM relationships (``consumer_address``, ``items``, ``status_logs``)
    are already hydrated by the caller.
    """
    ids: set[UUID] = {booking.consumer_id}
    if booking.provider_id is not None:
        ids.add(booking.provider_id)
    rows = await db.execute(select(User).where(User.id.in_(ids)))
    users_by_id = {u.id: u for u in rows.scalars()}
    consumer = users_by_id.get(booking.consumer_id)
    provider = users_by_id.get(booking.provider_id) if booking.provider_id else None
    return BookingResponse.for_admin(booking).model_copy(
        update={
            "consumer_phone": consumer.phone if consumer else None,
            "provider_name": provider.name if provider else None,
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
    dependencies=[_admin_dep],
)
async def list_pending_settlements(db: AsyncSession = Depends(get_db)):
    """Providers awaiting settlement, with the amount and booking count owed.

    The discovery half of the payout flow: ``POST /providers/{user_id}/payout`` can
    only be triggered for a provider the admin already knows, so this answers "who
    do we owe, and how much" for the admin payouts screen. Read-only, so it takes
    the role guard rather than injecting the admin (nothing to audit).
    """
    return await PayoutService(db).list_pending_settlements()


@router.post("/providers/{user_id}/payout", status_code=status.HTTP_202_ACCEPTED)
async def pay_out_provider(
    user_id: UUID,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Enqueue a RazorpayX payout for a provider's completed-but-unsettled bookings.

    Returns 202 with a summary of what will be paid and the Celery task id. The
    payout itself runs asynchronously (SVC-188 / G30): before this, the endpoint
    awaited a 30-90s RazorpayX HTTP call while holding a DB connection and a
    ``SELECT … FOR UPDATE`` lock on the provider's pending bookings, which
    puts pool pressure on the same workers serving consumer traffic during a
    RazorpayX slowdown.

    The upfront reads compute the batch total + booking count without a row
    lock, so the admin gets an accurate estimate immediately. The task then
    runs the full flow (record audit → lock rows → RazorpayX → resolve audit
    → update bookings → drain pushes) — every guarantee from the synchronous
    path is preserved because the task calls the same
    ``PayoutService.create_provider_payout``.

    Every attempt is still recorded in ``payout_audits`` before the RazorpayX
    call fires; the admin payouts screen surfaces the outcome from there, and
    ``reconcile_payouts`` (nightly) resolves any breadcrumb whose task
    process died mid-flight.
    """
    from app.config import get_settings
    from app.exceptions import PaymentError
    from app.tasks.payment_tasks import execute_provider_payout

    settings = get_settings()

    service = PayoutService(db)
    # Cheap read-only estimate — no FOR UPDATE, so no lock is held across the
    # enqueue. The task's own SELECT is the authoritative one; this is only for
    # the 202 response body so the UI can show "queued ₹X for N bookings"
    # rather than "queued ...something".
    estimate = await service.estimate_pending_payout(user_id)

    # SVC-191 (G18 half): refuse over-cap batches at the wire, before enqueue.
    # The task also enforces the same cap (defence in depth), but catching
    # here gives the admin immediate feedback instead of a delayed
    # audit-only failure they only see later on the audits screen.
    if estimate["amount_paise"] > settings.PAYOUT_MAX_BATCH_PAISE:
        raise PaymentError(
            f"Payout batch of {estimate['amount_paise']} paise exceeds cap of "
            f"{settings.PAYOUT_MAX_BATCH_PAISE} paise "
            "(PAYOUT_MAX_BATCH_PAISE). Wait for the backlog to drain or "
            "ask ops to lift the cap."
        )

    task = execute_provider_payout.delay(str(user_id), str(admin.id))
    return {
        "status": "queued",
        "task_id": task.id,
        "amount_paise": estimate["amount_paise"],
        "booking_count": estimate["booking_count"],
    }


@router.post("/providers/{user_id}/payout/requeue-failed", status_code=status.HTTP_200_OK)
async def requeue_failed_settlements(
    user_id: UUID,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Reset a provider's FAILED settlements to PENDING so they can be paid again.

    Manual recovery for payouts that failed (bad bank details, KYC hold, ...): once
    the cause is fixed, an admin re-queues the failed settlements, which then become
    eligible for the next payout run. The triggering admin is recorded in the audit
    trail.
    """
    return await PayoutService(db).requeue_settlements(
        user_id, admin.id, from_status=SettlementStatus.FAILED
    )


@router.post("/providers/{user_id}/payout/requeue-reversed", status_code=status.HTTP_200_OK)
async def requeue_reversed_settlements(
    user_id: UUID,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Reset a provider's REVERSED settlements to PENDING so they can be paid again.

    A reversal means the payout reached the provider and was then clawed back —
    often a KYC/compliance signal, not a simple retry. Only use this once you've
    confirmed the provider is genuinely still owed and the underlying cause is
    fixed; otherwise the re-payment will likely bounce again. Admin-only and
    recorded in the audit trail.
    """
    return await PayoutService(db).requeue_settlements(
        user_id, admin.id, from_status=SettlementStatus.REVERSED
    )


# ---------------------------------------------------------------------------
# App version releases (SVC-227)
# ---------------------------------------------------------------------------


async def _require_admin_or_release_token(
    request: Request,
    x_internal_release_token: str | None = Header(default=None),
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Alt-auth for the release endpoint (SVC-227.2).

    Accepts EITHER:
      - `X-Internal-Release-Token` header matching
        ``settings.INTERNAL_RELEASE_TOKEN`` (CI path — `release-apk.yml`), OR
      - Standard admin JWT (human path — same as every other admin route).

    Header path is scoped to THIS ONE endpoint — nothing else in the
    app accepts it. `hmac.compare_digest` to defeat timing attacks.
    Empty ``settings.INTERNAL_RELEASE_TOKEN`` disables the header path
    entirely; the endpoint then behaves like every other admin route.

    Written as one dep (rather than two Depends parameters) because
    FastAPI runs deps eagerly — a `require_role("admin")` dep alongside
    this one would hard-401 on missing JWT even when the header path
    already succeeded. The `HTTPBearer(auto_error=False)` in this
    signature is the trick that lets us decide the branch ourselves.

    ``request.state.release_auth`` records which path won ("token" or
    "jwt") so a Sentry event / access log can distinguish CI-driven
    releases from human ones — CI-driven releases lose the "who"
    otherwise.
    """
    import hmac

    from app.config import get_settings
    from app.dependencies import _tag_sentry_user

    settings = get_settings()
    if (
        x_internal_release_token
        and settings.INTERNAL_RELEASE_TOKEN
        and hmac.compare_digest(x_internal_release_token, settings.INTERNAL_RELEASE_TOKEN)
    ):
        request.state.release_auth = "token"
        return

    # JWT path — inline the check rather than delegating to the standard
    # `require_role("admin")` dep so we can return a specific 401 when
    # neither auth method was provided at all (vs 403 for a wrong-role
    # JWT). Same JWT decode + user lookup as `get_current_user`.
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Auth required: either admin JWT via `Authorization: Bearer <jwt>` "
                "or CI token via `X-Internal-Release-Token`."
            ),
        )

    from app.utils.security import decode_jwt

    payload = decode_jwt(credentials.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )
    user = await db.get(User, UUID(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive"
        )
    if user.role.value != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    _tag_sentry_user(str(user.id), user.role.value)
    request.state.release_auth = "jwt"


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
    dependencies=[Depends(_require_admin_or_release_token)],
)
async def publish_app_release(
    platform: str,
    request: PublishReleaseRequest,
    http_request: Request,
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
