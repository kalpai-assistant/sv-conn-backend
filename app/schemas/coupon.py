"""Response and request shapes for the coupon endpoints.

Deliberately narrow — the consumer only needs to see enough to render the
coupon card and to know why a code was accepted or rejected. Admin CRUD
lives in a different (unbuilt-for-beta) surface.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CouponResponse(BaseModel):
    """One coupon in a user's available list.

    ``discount_type`` lets the UI render the correct microcopy
    ("20% off" vs "₹100 off") without needing to know the value's units
    from context. ``expires_at`` is what the card renders — the internal
    ``valid_from`` is not exposed, a coupon that hasn't started yet simply
    doesn't appear in the list.
    """

    id: UUID
    code: str
    discount_type: str
    discount_value: float
    max_discount: float | None
    min_order_amount: float | None
    expires_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_coupon(cls, coupon) -> "CouponResponse":
        # `discount_type` is an enum; the response is a plain string so
        # the OpenAPI client doesn't need to import a Python enum name.
        return cls(
            id=coupon.id,
            code=coupon.code,
            discount_type=coupon.discount_type.value,
            discount_value=coupon.discount_value,
            max_discount=coupon.max_discount,
            min_order_amount=coupon.min_order_amount,
            expires_at=coupon.valid_until,
        )


class CouponValidateRequest(BaseModel):
    """User taps [Apply] on a coupon; the app asks the backend to price it."""

    code: str = Field(..., min_length=1, max_length=32)
    # The cart total the coupon should apply against. The client sends
    # this so the endpoint can compute the discount without having to
    # know about the (yet-unpersisted) cart — the actual Booking hasn't
    # been created yet.
    cart_amount: float = Field(..., gt=0)


class CouponValidateResponse(BaseModel):
    code: str
    discount_type: str
    discount_amount: float
