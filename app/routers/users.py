import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_redis
from app.models.user import User
from app.schemas.user import (
    AddressResponse,
    CreateAddressRequest,
    DeleteAccountRequest,
    PushCapabilityReport,
    UpdateAddressRequest,
    UpdateFCMTokenRequest,
    UpdateProfileRequest,
    UserResponse,
)
from app.services.account_service import AccountService, delete_objects
from app.services.auth_service import AuthService
from app.services.user_service import AddressDetails, UserService

router = APIRouter(prefix="/users", tags=["Users"])

logger = logging.getLogger(__name__)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserResponse)
async def update_me(
    request: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await UserService(db).update_profile(
        user_id=current_user.id,
        name=request.name,
        email=request.email,
    )


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    body: DeleteAccountRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """Delete the caller's account (SVC-367). No undo.

    409 ``ACTIVE_BOOKINGS`` (``details.booking_ids``) while any booking they are
    on is requested/accepted/en route/in progress; for an SP, 409
    ``TEAM_HAS_PROS`` (``details.professional_ids``) while a team member is
    still active, and 409 ``UNPAID_BALANCE`` (``details.balance_paise``) while
    a payout is owed. Nothing is changed on a 409.

    Otherwise the account is anonymised in place (see ``AccountService``) and
    its photos and KYC images are deleted from S3 after the commit.
    """
    keys = await AccountService(db).delete_account(current_user.id)
    await db.commit()
    if body is not None and body.refresh_token:
        await AuthService(db, redis).logout(body.refresh_token)
    await asyncio.to_thread(delete_objects, current_user.id, keys)


@router.patch("/me/fcm-token", status_code=status.HTTP_204_NO_CONTENT)
async def update_fcm_token(
    request: UpdateFCMTokenRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register the caller's FCM token, or clear it by sending `null` on sign-out.

    The field is required, so an empty body is rejected — clearing must be explicit.
    """
    await UserService(db).update_fcm_token(current_user.id, request.fcm_token)


@router.post("/me/push-capability", status_code=status.HTTP_204_NO_CONTENT)
async def report_push_capability(
    report: PushCapabilityReport,
    current_user: User = Depends(get_current_user),
):
    """Record what the caller's device can do about push (SVC-255). Logs only.

    SVC-255 is about providers who never receive notifications, and nobody can
    currently say how many that is. Sentry captures registration *failures*, but
    with no denominator — and no existing request carries the platform, so there
    is nothing to mine retroactively. The version check looks like it would help
    and does not: it reads a static `latest.json` and picks the platform branch
    on the client, so the server never sees which one.

    Deliberately stores nothing. One structured line, counted in CloudWatch
    Insights. The question is "should we invest in the iOS path at all", which
    needs answering once — a column on `users` would answer a different
    question ("which providers should we chase") and is only worth adding if
    the distribution says the problem is real.

    Fire-and-forget from the client's side: a failure here must never affect
    whether push itself registers.
    """
    logger.info(
        "push.capability user=%s role=%s platform=%s native=%s standalone=%s "
        "chrome_ios=%s permission=%s token=%s interactive=%s",
        current_user.id,
        current_user.role.value,
        report.platform,
        report.is_native,
        report.ios_standalone,
        report.is_chrome_ios,
        report.push_permission,
        report.token_obtained,
        report.interactive,
    )


@router.get("/me/addresses", response_model=list[AddressResponse])
async def list_addresses(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await UserService(db).list_addresses(current_user.id)


def _details_from(
    request: CreateAddressRequest | UpdateAddressRequest,
) -> AddressDetails | None:
    """The structured fields off a request, or None when it carried none.

    None is meaningful rather than an empty object: on a PATCH it is the
    difference between "this edit did not mention the flat number" and "clear
    the flat number", and only the first is ever what a client means here.
    """
    values = {
        f: getattr(request, f, None)
        for f in (
            "house_floor",
            "building",
            "landmark_area",
            "pincode",
            "contact_name",
            "contact_phone",
        )
    }
    return AddressDetails(**values) if any(v is not None for v in values.values()) else None


def compose_full_address(details: AddressDetails | None, fallback: str | None) -> str:
    """Build the address line a provider reads (SVC-287).

    Ordered the way an address is spoken on the way to a door: the flat first,
    then the building, then what to look for, then the area and PIN. A provider
    reading this on a job card is navigating, not filing.

    `fallback` — whatever the client sent as `full_address` — carries the
    locality and city, which none of the structured fields hold. It is appended
    rather than replaced, and returned alone when there is nothing structured
    to prepend, so an older app build behaves exactly as it did before.
    """
    if details is None:
        return fallback or ""

    parts = [
        details.house_floor,
        details.building,
        details.landmark_area,
        fallback,
        details.pincode,
    ]
    composed = ", ".join(p.strip() for p in parts if p and p.strip())
    # The column is String(500) and the request fields sum to more than that.
    # Truncating here beats a 500 from the driver on an address a user has
    # already typed.
    return composed[:500] if composed else (fallback or "")


@router.post("/me/addresses", response_model=AddressResponse, status_code=status.HTTP_201_CREATED)
async def add_address(
    request: CreateAddressRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    details = _details_from(request)
    return await UserService(db).add_address(
        user_id=current_user.id,
        label=request.label,
        # Composed from the structured fields when they are present, so the
        # provider's job card reads "Flat 302, 3rd floor, Amogh Residency…"
        # rather than a geocoded street. Falls back to whatever the client sent
        # when they are not — an older app build still works unchanged.
        full_address=compose_full_address(details, request.full_address),
        lat=request.lat,
        lng=request.lng,
        is_default=request.is_default,
        details=details,
    )


@router.patch("/me/addresses/{address_id}", response_model=AddressResponse)
async def update_address(
    address_id: uuid.UUID,
    request: UpdateAddressRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Edit a saved address (SVC-277).

    Copy-on-write: the old row is soft-deleted and a new one returned. Bookings
    that already point at the old address keep recording where the job actually
    happened, and a provider en route to it is not silently redirected.
    """
    details = _details_from(request)
    updated = await UserService(db).update_address(
        user_id=current_user.id,
        address_id=address_id,
        label=request.label,
        # Recomposed only when this PATCH actually carried structured fields.
        # Otherwise the stored string is left exactly as it is: an edit that
        # only changes the label must not rewrite the address line, and rows
        # written before SVC-287 have nothing to compose from.
        full_address=(
            compose_full_address(details, request.full_address)
            if details is not None
            else request.full_address
        ),
        lat=request.lat,
        lng=request.lng,
        is_default=request.is_default,
        details=details,
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address not found")
    return updated


@router.delete("/me/addresses/{address_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_address(
    address_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a saved address (SVC-277: now a soft delete).

    The row is hidden, not deleted — `bookings.consumer_address_id` is an
    unqualified FK, so removing an address any booking has ever used raises.
    """
    await UserService(db).delete_address(current_user.id, address_id)
