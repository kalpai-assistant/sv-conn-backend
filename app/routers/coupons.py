"""Consumer coupon endpoints — list and validate.

Only two endpoints, deliberately. Creating a coupon happens off-platform
(admin DB insert or the future referral job); redeeming a coupon happens
inline with booking creation (``POST /bookings`` takes an optional
``coupon_code``). This router is what the app calls to show a user their
coupons and to price one against a cart before committing.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.coupon import (
    CouponResponse,
    CouponValidateRequest,
    CouponValidateResponse,
)
from app.services.coupon_service import CouponService

router = APIRouter(prefix="/coupons", tags=["Coupons"])


@router.get("/available", response_model=list[CouponResponse])
async def list_available_coupons(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Coupons the current user can apply to a new booking today.

    The list matches what ``POST /coupons/validate`` will accept — the
    filter set is shared through ``CouponService`` so a coupon shown here
    is never refused there.
    """
    coupons = await CouponService(db).list_available(current_user.id)
    return [CouponResponse.from_coupon(c) for c in coupons]


@router.post(
    "/validate",
    response_model=CouponValidateResponse,
    responses={400: {"description": "Coupon is not applicable to this cart"}},
)
async def validate_coupon(
    request: CouponValidateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Price a coupon against a cart amount.

    The cart hasn't been persisted as a Booking yet — the app calls this
    while the user is on the Cart screen so the bill preview updates
    live. On success we return the computed discount; a subsequent
    ``POST /bookings`` with ``coupon_code`` recomputes on the server side
    and applies the *authoritative* number, so a client that hoards a
    stale discount can't over-apply.
    """
    coupon, discount = await CouponService(db).validate(
        code=request.code, user_id=current_user.id, cart_amount=request.cart_amount
    )
    return CouponValidateResponse(
        code=coupon.code,
        discount_type=coupon.discount_type.value,
        discount_amount=discount,
    )
