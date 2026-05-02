from uuid import UUID
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse


# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------

class AppException(Exception):
    """Base for all application-level exceptions."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
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


class InvalidOTP(AppException):
    def __init__(self, message: str = "Invalid OTP"):
        super().__init__(message, status_code=status.HTTP_400_BAD_REQUEST)


class PaymentError(AppException):
    def __init__(self, message: str):
        super().__init__(message, status_code=status.HTTP_402_PAYMENT_REQUIRED)


class RateLimitExceeded(AppException):
    def __init__(self):
        super().__init__(
            "Too many requests",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )


class DuplicateReview(AppException):
    def __init__(self):
        super().__init__(
            "A review for this booking already exists",
            status_code=status.HTTP_409_CONFLICT,
        )


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": True,
                "message": exc.message,
                "status_code": exc.status_code,
            },
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
