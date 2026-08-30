from uuid import UUID

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class AppException(Exception):
    """Base for all application-level exceptions.

    ``code`` is optional and, when set, is included in the JSON response body
    so the frontend can branch on a stable identifier instead of parsing the
    message. Existing subclasses that don't set one keep the same wire shape.
    """

    def __init__(self, message: str, status_code: int = 400, code: str | None = None):
        self.message = message
        self.status_code = status_code
        self.code = code
        super().__init__(message)


class BookingNotFound(AppException):
    def __init__(self, booking_id: UUID):
        super().__init__(
            f"Booking {booking_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class ServiceNotFound(AppException):
    def __init__(self, service_id: UUID):
        super().__init__(
            f"Service {service_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class UserNotFound(AppException):
    def __init__(self, user_id: UUID):
        super().__init__(
            f"User {user_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class InvalidStateTransition(AppException):
    def __init__(self, message: str):
        super().__init__(message, status_code=status.HTTP_409_CONFLICT)


class UnauthorizedAction(AppException):
    def __init__(self, message: str):
        super().__init__(message, status_code=status.HTTP_403_FORBIDDEN)


class BookingOfferClosed(AppException):
    """The broadcast offer is over — claimed by someone else, cancelled or expired.

    410 rather than 403 on purpose (SVC-153): the provider *was* entitled to see
    this job, and the card needs to tell "someone beat me to it" (dismiss quietly)
    apart from "you should never have seen this" (a real error), without parsing
    the message. The status code is the discriminator.
    """

    def __init__(self, message: str = "This booking is no longer available"):
        super().__init__(message, status_code=status.HTTP_410_GONE)


class InvalidOTP(AppException):
    def __init__(self, message: str = "Invalid OTP"):
        super().__init__(message, status_code=status.HTTP_400_BAD_REQUEST)


class PaymentError(AppException):
    def __init__(self, message: str):
        super().__init__(message, status_code=status.HTTP_402_PAYMENT_REQUIRED)


class InvalidPayoutMethod(AppException):
    """Asked to activate a payout method the provider has nothing stored for.

    400 rather than 402: the request itself is wrong (activate a destination that
    does not exist), not a payment that could not be completed.
    """

    def __init__(self, message: str):
        super().__init__(message, status_code=status.HTTP_400_BAD_REQUEST)


class ServiceAreaMissing(AppException):
    """SVC-257: provider tried to go online without a service_area_center set.

    Going online with a NULL geo makes the provider invisible to the PostGIS
    dispatch query — every incoming booking silently doesn't match. Refusing
    at the wire, with a distinct code the frontend can catch, means the app
    nudges the provider to complete their profile instead of appearing active
    while receiving zero offers.
    """

    def __init__(self):
        super().__init__(
            "Set your service area before going online.",
            status_code=status.HTTP_400_BAD_REQUEST,
            code="SERVICE_AREA_MISSING",
        )


class CouponInvalid(AppException):
    """The requested coupon can't be used against this cart.

    One exception type covers every reason (expired, min-order, per-user
    limit, doesn't apply to this user). The client renders a single
    generic message; a finer enumeration would need to stay in sync
    between server and client for no visible user benefit at beta scale.
    """

    def __init__(self, message: str = "Coupon not applicable"):
        super().__init__(message, status_code=status.HTTP_400_BAD_REQUEST)


class SMSDeliveryError(AppException):
    """The SMS provider rejected an OTP send (bad key, unapproved template, dropped by DLT, etc.).

    502 rather than 500: the failure is downstream (MSG91 / Twilio / DLT
    stack), not our code. Client apps can retry after the operator fixes
    the provider — no permanent server bug.
    """

    def __init__(self, message: str = "SMS delivery failed"):
        super().__init__(message, status_code=status.HTTP_502_BAD_GATEWAY)


class RateLimitExceeded(AppException):
    """
    Raised when a request exceeds a per-key rate limit.

    `retry_after` is exposed to callers via the standard `Retry-After` HTTP
    header (see the handler in `register_exception_handlers`). A value of `None`
    means we couldn't compute a meaningful wait — the handler defaults to the
    full policy window.

    `limit` and `reset_at` optionally carry the same X-RateLimit-* context
    that a within-limit response emits (SVC-159), so clients see a consistent
    policy on both 200 and 429 and don't have to look in two places.
    """

    def __init__(
        self,
        retry_after: int | None = None,
        message: str = "Too many requests",
        limit: int | None = None,
        reset_at: int | None = None,
    ):
        super().__init__(
            message,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
        self.retry_after = retry_after
        self.limit = limit
        self.reset_at = reset_at


class DuplicateReview(AppException):
    def __init__(self):
        super().__init__(
            "A review for this booking already exists",
            status_code=status.HTTP_409_CONFLICT,
        )


class DuplicateSubscription(AppException):
    def __init__(self, service_id: UUID):
        super().__init__(
            f"Provider is already subscribed to service {service_id}",
            status_code=status.HTTP_409_CONFLICT,
        )


class SubscriptionNotFound(AppException):
    def __init__(self, service_id: UUID):
        super().__init__(
            f"Subscription to service {service_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class IdempotencyKeyReused(AppException):
    """Same Idempotency-Key sent with a different request body (SVC-158)."""

    def __init__(self):
        super().__init__(
            "Idempotency-Key was reused with a different request body. "
            "Generate a new key for each distinct request.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class IdempotencyInProgress(AppException):
    """Concurrent request with the same Idempotency-Key is still in flight (SVC-158)."""

    def __init__(self):
        super().__init__(
            "A request with this Idempotency-Key is already in progress. " "Retry in a moment.",
            status_code=status.HTTP_409_CONFLICT,
        )


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException):
        headers: dict[str, str] = {}
        # Rate-limit responses always carry Retry-After per RFC 6585 §4 so
        # clients can back off intelligently instead of hammering. When the
        # limiter passed us richer context (SVC-159), mirror the same
        # X-RateLimit-* headers the 200 path emits so a client sees a
        # consistent policy view whether they're inside or past the ceiling.
        if isinstance(exc, RateLimitExceeded):
            headers["Retry-After"] = str(exc.retry_after or 60)
            if exc.limit is not None:
                headers["X-RateLimit-Limit"] = str(exc.limit)
                # Remaining is definitionally 0 at 429.
                headers["X-RateLimit-Remaining"] = "0"
            if exc.reset_at is not None:
                headers["X-RateLimit-Reset"] = str(exc.reset_at)
        content: dict = {
            "error": True,
            "message": exc.message,
            "status_code": exc.status_code,
        }
        if exc.code is not None:
            content["code"] = exc.code
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
            headers=headers or None,
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": True,
                "message": "Internal server error",
                "status_code": 500,
            },
        )
