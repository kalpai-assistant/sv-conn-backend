# Low-Level Design — ServiceConnect Backend

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.0 / PostgreSQL + PostGIS / Redis / Celery  
**Document Version:** 1.0  
**Date:** April 14, 2026

---

## 1. Project Structure

```
serviceconnect/
├── alembic/                          # Database migrations
│   ├── versions/
│   └── env.py
├── app/
│   ├── __init__.py
│   ├── main.py                       # FastAPI app factory, lifespan, middleware
│   ├── config.py                     # Pydantic Settings (env-based config)
│   ├── database.py                   # SQLAlchemy engine, session factory
│   ├── dependencies.py               # Shared FastAPI dependencies (get_db, get_current_user)
│   ├── exceptions.py                 # Custom exception classes + handlers
│   ├── middleware/
│   │   ├── __init__.py
│   │   ├── auth.py                   # JWT verification middleware
│   │   ├── rate_limit.py             # Redis-based rate limiter
│   │   └── request_id.py            # X-Request-ID injection for tracing
│   ├── models/                       # SQLAlchemy ORM models
│   │   ├── __init__.py
│   │   ├── user.py
│   │   ├── provider_profile.py
│   │   ├── address.py
│   │   ├── category.py
│   │   ├── service.py
│   │   ├── booking.py
│   │   ├── booking_status_log.py
│   │   ├── review.py
│   │   └── payment.py
│   ├── schemas/                      # Pydantic request/response schemas
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── user.py
│   │   ├── provider.py
│   │   ├── service.py
│   │   ├── booking.py
│   │   ├── payment.py
│   │   └── review.py
│   ├── routers/                      # FastAPI APIRouter modules
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── users.py
│   │   ├── providers.py
│   │   ├── services.py
│   │   ├── bookings.py
│   │   ├── payments.py
│   │   ├── reviews.py
│   │   ├── tracking.py              # WebSocket endpoint
│   │   └── admin.py
│   ├── services/                     # Business logic layer
│   │   ├── __init__.py
│   │   ├── auth_service.py
│   │   ├── user_service.py
│   │   ├── provider_service.py
│   │   ├── catalog_service.py
│   │   ├── booking_service.py
│   │   ├── payment_service.py
│   │   ├── tracking_service.py
│   │   ├── notification_service.py
│   │   └── review_service.py
│   ├── tasks/                        # Celery async tasks
│   │   ├── __init__.py
│   │   ├── celery_app.py
│   │   ├── notification_tasks.py
│   │   ├── payment_tasks.py
│   │   └── cleanup_tasks.py
│   └── utils/
│       ├── __init__.py
│       ├── security.py               # JWT encode/decode, password hashing
│       ├── otp.py                    # OTP generation + verification
│       ├── razorpay_client.py        # Razorpay SDK wrapper
│       ├── sms_client.py             # MSG91/Twilio abstraction
│       ├── fcm_client.py             # Firebase push notification client
│       ├── storage.py                # S3/MinIO file upload helper
│       └── geo.py                    # PostGIS query helpers
├── tests/
│   ├── conftest.py                   # Fixtures: test DB, client, auth headers
│   ├── test_auth.py
│   ├── test_bookings.py
│   ├── test_payments.py
│   └── factories/                    # Test data factories (factory_boy)
│       ├── user_factory.py
│       └── booking_factory.py
├── scripts/
│   ├── seed_categories.py            # Seed initial categories + services
│   └── create_admin.py               # Create super admin user
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── alembic.ini
├── .env.example
└── pyproject.toml
```

---

## 2. Configuration

### 2.1 `app/config.py`

```python
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # App
    APP_NAME: str = "ServiceConnect"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # Database
    DATABASE_URL: str  # postgresql+asyncpg://user:pass@host:5432/db
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # Redis
    REDIS_URL: str  # redis://localhost:6379/0

    # JWT
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # OTP
    OTP_LENGTH: int = 6
    OTP_EXPIRY_SECONDS: int = 600  # 10 minutes
    OTP_MAX_ATTEMPTS_PER_HOUR: int = 5
    DOORSTEP_OTP_LENGTH: int = 4

    # SMS Provider
    SMS_PROVIDER: str = "msg91"  # msg91 | twilio
    MSG91_AUTH_KEY: str = ""
    MSG91_TEMPLATE_ID: str = ""
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_PHONE_NUMBER: str = ""

    # Razorpay
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    PLATFORM_COMMISSION_PERCENT: float = 20.0

    # Firebase
    FCM_CREDENTIALS_PATH: str = ""

    # Storage
    S3_BUCKET_NAME: str = ""
    S3_ENDPOINT_URL: str = ""  # MinIO endpoint for local dev
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""

    # Google Maps
    GOOGLE_MAPS_API_KEY: str = ""

    # Booking
    BOOKING_REQUEST_TIMEOUT_SECONDS: int = 120  # 2 minutes
    CANCELLATION_FEE: float = 50.0

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
```

---

## 3. Database Layer

### 3.1 `app/database.py`

```python
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
)
from sqlalchemy.orm import DeclarativeBase
from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    echo=settings.DEBUG,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

### 3.2 ORM Models

#### `app/models/user.py`

```python
import uuid
import enum
from datetime import datetime
from sqlalchemy import String, Boolean, Enum, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class UserRole(str, enum.Enum):
    CONSUMER = "consumer"
    PROVIDER = "provider"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    phone: Mapped[str] = mapped_column(
        String(15), unique=True, nullable=False, index=True
    )
    name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    fcm_token: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    provider_profile = relationship(
        "ProviderProfile", back_populates="user", uselist=False
    )
    addresses = relationship("Address", back_populates="user")
    bookings_as_consumer = relationship(
        "Booking", back_populates="consumer", foreign_keys="Booking.consumer_id"
    )
    bookings_as_provider = relationship(
        "Booking", back_populates="provider", foreign_keys="Booking.provider_id"
    )
```

#### `app/models/provider_profile.py`

```python
import uuid
from sqlalchemy import String, Boolean, Integer, Float, ForeignKey, Enum
from sqlalchemy.dialects.postgresql import UUID
from geoalchemy2 import Geography
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
import enum


class KYCStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ProviderProfile(Base):
    __tablename__ = "provider_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), unique=True
    )
    aadhaar_encrypted: Mapped[str | None] = mapped_column(String(512))
    pan_encrypted: Mapped[str | None] = mapped_column(String(512))
    upi_id_encrypted: Mapped[str | None] = mapped_column(String(512))
    bank_account_encrypted: Mapped[str | None] = mapped_column(String(1024))
    profile_photo_url: Mapped[str | None] = mapped_column(String(512))

    # PostGIS geography column for location-based queries
    service_area_center = mapped_column(
        Geography(geometry_type="POINT", srid=4326), nullable=True
    )
    service_area_radius_km: Mapped[int] = mapped_column(Integer, default=10)

    avg_rating: Mapped[float] = mapped_column(Float, default=0.0)
    total_completed_jobs: Mapped[int] = mapped_column(Integer, default=0)
    razorpay_linked_account_id: Mapped[str | None] = mapped_column(String(255))
    kyc_status: Mapped[KYCStatus] = mapped_column(
        Enum(KYCStatus), default=KYCStatus.PENDING
    )
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)

    # Relationships
    user = relationship("User", back_populates="provider_profile")
    services = relationship("Service", back_populates="provider")
```

#### `app/models/booking.py`

```python
import uuid
import enum
from datetime import datetime
from sqlalchemy import String, Float, ForeignKey, Enum, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class BookingStatus(str, enum.Enum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    EN_ROUTE = "en_route"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PaymentMethod(str, enum.Enum):
    CASH = "cash"
    ONLINE = "online"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    REFUNDED = "refunded"


# Valid state transitions — enforced in booking_service.py
VALID_TRANSITIONS: dict[BookingStatus, list[BookingStatus]] = {
    BookingStatus.REQUESTED: [BookingStatus.ACCEPTED, BookingStatus.EXPIRED, BookingStatus.CANCELLED],
    BookingStatus.ACCEPTED: [BookingStatus.EN_ROUTE, BookingStatus.CANCELLED],
    BookingStatus.EN_ROUTE: [BookingStatus.IN_PROGRESS, BookingStatus.CANCELLED],
    BookingStatus.IN_PROGRESS: [BookingStatus.COMPLETED],
    BookingStatus.COMPLETED: [],
    BookingStatus.CANCELLED: [],
    BookingStatus.EXPIRED: [],
}


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    consumer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id")
    )
    consumer_address_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus), default=BookingStatus.REQUESTED, index=True
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    doorstep_otp: Mapped[str] = mapped_column(String(4))
    service_amount: Mapped[float] = mapped_column(Float, nullable=False)
    platform_commission: Mapped[float] = mapped_column(Float, nullable=False)
    provider_payout: Mapped[float] = mapped_column(Float, nullable=False)
    payment_method: Mapped[PaymentMethod | None] = mapped_column(Enum(PaymentMethod))
    payment_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.PENDING
    )
    razorpay_order_id: Mapped[str | None] = mapped_column(String(255))
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    consumer = relationship(
        "User", back_populates="bookings_as_consumer", foreign_keys=[consumer_id]
    )
    provider = relationship(
        "User", back_populates="bookings_as_provider", foreign_keys=[provider_id]
    )
    service = relationship("Service")
    consumer_address = relationship("Address")
    status_logs = relationship("BookingStatusLog", back_populates="booking")
    review = relationship("Review", back_populates="booking", uselist=False)
    payment = relationship("Payment", back_populates="booking", uselist=False)
```

#### `app/models/booking_status_log.py`

```python
import uuid
from datetime import datetime
from sqlalchemy import String, ForeignKey, DateTime, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class BookingStatusLog(Base):
    __tablename__ = "booking_status_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), index=True
    )
    from_status: Mapped[str] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    changed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    booking = relationship("Booking", back_populates="status_logs")
```

*Remaining models (`Address`, `Category`, `Service`, `Review`, `Payment`) follow the same pattern as defined in the HLD database schema.*

---

## 4. Pydantic Schemas

### 4.1 `app/schemas/auth.py`

```python
from pydantic import BaseModel, Field
import re


class SendOTPRequest(BaseModel):
    phone: str = Field(
        ...,
        pattern=r"^\+91[6-9]\d{9}$",
        description="Indian phone number with +91 prefix"
    )


class VerifyOTPRequest(BaseModel):
    phone: str = Field(..., pattern=r"^\+91[6-9]\d{9}$")
    otp: str = Field(..., min_length=6, max_length=6)
    role: str = Field(default="consumer", pattern=r"^(consumer|provider)$")


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: str
    role: str
    is_new_user: bool


class RefreshTokenRequest(BaseModel):
    refresh_token: str
```

### 4.2 `app/schemas/booking.py`

```python
from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from enum import Enum


class CreateBookingRequest(BaseModel):
    service_id: UUID
    address_id: UUID
    scheduled_at: datetime | None = None  # None = instant booking
    payment_method: str = Field(
        default="online", pattern=r"^(cash|online)$"
    )


class BookingResponse(BaseModel):
    id: UUID
    consumer_id: UUID
    provider_id: UUID
    service_name: str
    status: str
    service_amount: float
    platform_commission: float
    provider_payout: float
    payment_method: str | None
    payment_status: str
    doorstep_otp: str | None = None  # Only visible to consumer
    scheduled_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class AcceptBookingRequest(BaseModel):
    pass  # Provider just hits the endpoint; booking_id is in URL


class StartBookingRequest(BaseModel):
    otp: str = Field(..., min_length=4, max_length=4)


class CancelBookingRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)


class BookingListResponse(BaseModel):
    bookings: list[BookingResponse]
    total: int
    page: int
    per_page: int
```

### 4.3 `app/schemas/payment.py`

```python
from pydantic import BaseModel
from uuid import UUID


class CreatePaymentOrderRequest(BaseModel):
    booking_id: UUID


class PaymentOrderResponse(BaseModel):
    razorpay_order_id: str
    amount: int           # In paise (₹100 = 10000)
    currency: str = "INR"
    razorpay_key_id: str  # Public key for frontend checkout


class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class RazorpayWebhookPayload(BaseModel):
    event: str
    payload: dict
```

---

## 5. Business Logic Layer (Services)

### 5.1 `app/services/booking_service.py`

This is the core of the application — the booking state machine.

```python
import uuid
import secrets
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.booking import (
    Booking, BookingStatus, PaymentStatus, VALID_TRANSITIONS,
)
from app.models.booking_status_log import BookingStatusLog
from app.models.service import Service
from app.exceptions import (
    InvalidStateTransition,
    BookingNotFound,
    UnauthorizedAction,
    InvalidOTP,
)
from app.config import get_settings

settings = get_settings()


class BookingService:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_booking(
        self,
        consumer_id: uuid.UUID,
        service_id: uuid.UUID,
        address_id: uuid.UUID,
        scheduled_at: datetime | None = None,
        payment_method: str = "online",
    ) -> Booking:
        """Create a new booking and calculate commission split."""
        service = await self.db.get(Service, service_id)
        if not service or not service.is_active:
            raise ValueError("Service not found or inactive")

        commission_rate = settings.PLATFORM_COMMISSION_PERCENT / 100
        service_amount = service.base_price
        platform_commission = round(service_amount * commission_rate, 2)
        provider_payout = round(service_amount - platform_commission, 2)

        booking = Booking(
            consumer_id=consumer_id,
            provider_id=service.provider_id,
            service_id=service_id,
            consumer_address_id=address_id,
            status=BookingStatus.REQUESTED,
            scheduled_at=scheduled_at,
            doorstep_otp=self._generate_doorstep_otp(),
            service_amount=service_amount,
            platform_commission=platform_commission,
            provider_payout=provider_payout,
            payment_method=payment_method,
        )

        self.db.add(booking)
        await self.db.flush()

        await self._log_transition(
            booking_id=booking.id,
            from_status="none",
            to_status=BookingStatus.REQUESTED.value,
            changed_by=consumer_id,
        )

        return booking

    async def transition_status(
        self,
        booking_id: uuid.UUID,
        new_status: BookingStatus,
        changed_by: uuid.UUID,
        otp: str | None = None,
        cancellation_reason: str | None = None,
        metadata: dict | None = None,
    ) -> Booking:
        """
        Enforce state machine transitions.
        Raises InvalidStateTransition if the move is illegal.
        """
        booking = await self.db.get(Booking, booking_id)
        if not booking:
            raise BookingNotFound(booking_id)

        # Validate transition
        allowed = VALID_TRANSITIONS.get(booking.status, [])
        if new_status not in allowed:
            raise InvalidStateTransition(
                f"Cannot move from {booking.status.value} to {new_status.value}. "
                f"Allowed: {[s.value for s in allowed]}"
            )

        # Authorization checks
        if new_status == BookingStatus.ACCEPTED:
            if changed_by != booking.provider_id:
                raise UnauthorizedAction("Only the assigned provider can accept")

        if new_status == BookingStatus.IN_PROGRESS:
            if not otp or otp != booking.doorstep_otp:
                raise InvalidOTP("Doorstep OTP does not match")

        if new_status == BookingStatus.CANCELLED:
            booking.cancellation_reason = cancellation_reason

        # Apply transition
        old_status = booking.status
        booking.status = new_status

        # Side effects
        if new_status == BookingStatus.COMPLETED:
            booking.payment_status = PaymentStatus.PENDING
            # Trigger payment flow (handled by caller or celery task)

        await self._log_transition(
            booking_id=booking.id,
            from_status=old_status.value,
            to_status=new_status.value,
            changed_by=changed_by,
            metadata=metadata,
        )

        return booking

    async def _log_transition(
        self,
        booking_id: uuid.UUID,
        from_status: str,
        to_status: str,
        changed_by: uuid.UUID,
        metadata: dict | None = None,
    ):
        log = BookingStatusLog(
            booking_id=booking_id,
            from_status=from_status,
            to_status=to_status,
            changed_by=changed_by,
            metadata_=metadata or {},
        )
        self.db.add(log)

    @staticmethod
    def _generate_doorstep_otp() -> str:
        return "".join(
            [str(secrets.randbelow(10)) for _ in range(4)]
        )
```

### 5.2 `app/services/payment_service.py`

```python
import hmac
import hashlib
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.booking import Booking, PaymentStatus
from app.models.payment import Payment
from app.utils.razorpay_client import razorpay_client
from app.config import get_settings
from app.exceptions import BookingNotFound, PaymentError

settings = get_settings()


class PaymentService:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_order(self, booking_id: UUID) -> dict:
        """
        Create a Razorpay order with Route transfer for 80/20 split.
        Returns order details for frontend checkout.
        """
        booking = await self.db.get(Booking, booking_id)
        if not booking:
            raise BookingNotFound(booking_id)

        provider_profile = await self._get_provider_profile(booking.provider_id)

        amount_paise = int(booking.service_amount * 100)
        provider_paise = int(booking.provider_payout * 100)

        order_data = {
            "amount": amount_paise,
            "currency": "INR",
            "receipt": str(booking.id),
            "transfers": [
                {
                    "account": provider_profile.razorpay_linked_account_id,
                    "amount": provider_paise,
                    "currency": "INR",
                    "on_hold": False,
                }
            ],
        }

        order = razorpay_client.order.create(data=order_data)
        booking.razorpay_order_id = order["id"]
        await self.db.flush()

        return {
            "razorpay_order_id": order["id"],
            "amount": amount_paise,
            "currency": "INR",
            "razorpay_key_id": settings.RAZORPAY_KEY_ID,
        }

    async def verify_payment(
        self,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
    ) -> Payment:
        """
        Verify Razorpay payment signature (HMAC SHA256).
        On success, create Payment record and update booking status.
        """
        expected_signature = hmac.new(
            key=settings.RAZORPAY_KEY_SECRET.encode(),
            msg=f"{razorpay_order_id}|{razorpay_payment_id}".encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_signature, razorpay_signature):
            raise PaymentError("Payment signature verification failed")

        # Find booking by order ID
        booking = await self._find_booking_by_order(razorpay_order_id)

        payment = Payment(
            booking_id=booking.id,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_order_id=razorpay_order_id,
            amount=booking.service_amount,
            platform_fee=booking.platform_commission,
            provider_settlement=booking.provider_payout,
            status="captured",
            method="online",
        )
        self.db.add(payment)

        booking.payment_status = PaymentStatus.PAID
        await self.db.flush()

        return payment

    async def handle_webhook(self, event: str, payload: dict):
        """
        Handle Razorpay webhook events.
        Primary events: payment.captured, payment.failed, transfer.processed
        """
        if event == "payment.captured":
            payment_entity = payload.get("payment", {}).get("entity", {})
            order_id = payment_entity.get("order_id")
            # Idempotent: check if already processed
            booking = await self._find_booking_by_order(order_id)
            if booking and booking.payment_status != PaymentStatus.PAID:
                booking.payment_status = PaymentStatus.PAID
                await self.db.flush()

        elif event == "payment.failed":
            payment_entity = payload.get("payment", {}).get("entity", {})
            order_id = payment_entity.get("order_id")
            # Log failure, notify consumer to retry
            pass
```

### 5.3 `app/services/tracking_service.py`

```python
import json
from uuid import UUID
from redis.asyncio import Redis
from app.config import get_settings

settings = get_settings()

TRACKING_CHANNEL_PREFIX = "tracking:"
LOCATION_TTL_SECONDS = 60  # Expire stale locations after 60s


class TrackingService:
    """
    Redis pub/sub for real-time provider location streaming.

    Flow:
    1. Provider WebSocket publishes location to Redis channel
    2. Consumer WebSocket subscribes to the same channel
    3. Each location update is also cached as latest known position
    """

    def __init__(self, redis: Redis):
        self.redis = redis

    def channel_name(self, booking_id: UUID) -> str:
        return f"{TRACKING_CHANNEL_PREFIX}{booking_id}"

    async def publish_location(
        self,
        booking_id: UUID,
        lat: float,
        lng: float,
        heading: float | None = None,
    ):
        """Provider publishes their current location."""
        payload = json.dumps({
            "lat": lat,
            "lng": lng,
            "heading": heading,
            "timestamp": __import__("time").time(),
        })

        channel = self.channel_name(booking_id)

        # Publish for real-time subscribers
        await self.redis.publish(channel, payload)

        # Cache latest position (for late-joining consumers)
        await self.redis.setex(
            f"location:{booking_id}",
            LOCATION_TTL_SECONDS,
            payload,
        )

    async def get_latest_location(self, booking_id: UUID) -> dict | None:
        """Get cached last known position."""
        data = await self.redis.get(f"location:{booking_id}")
        if data:
            return json.loads(data)
        return None

    async def subscribe(self, booking_id: UUID):
        """
        Returns an async generator that yields location updates.
        Used by the consumer WebSocket endpoint.
        """
        pubsub = self.redis.pubsub()
        channel = self.channel_name(booking_id)
        await pubsub.subscribe(channel)

        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    yield json.loads(message["data"])
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
```

---

## 6. API Routers

### 6.1 `app/routers/auth.py`

```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.schemas.auth import (
    SendOTPRequest,
    VerifyOTPRequest,
    TokenResponse,
    RefreshTokenRequest,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/send-otp", status_code=status.HTTP_200_OK)
async def send_otp(
    request: SendOTPRequest,
    db: AsyncSession = Depends(get_db),
):
    auth_service = AuthService(db)
    await auth_service.send_otp(request.phone)
    return {"message": "OTP sent successfully"}


@router.post("/verify-otp", response_model=TokenResponse)
async def verify_otp(
    request: VerifyOTPRequest,
    db: AsyncSession = Depends(get_db),
):
    auth_service = AuthService(db)
    result = await auth_service.verify_otp(
        phone=request.phone,
        otp=request.otp,
        role=request.role,
    )
    return result


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    auth_service = AuthService(db)
    return await auth_service.refresh_tokens(request.refresh_token)
```

### 6.2 `app/routers/bookings.py`

```python
from uuid import UUID
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User, UserRole
from app.models.booking import BookingStatus
from app.schemas.booking import (
    CreateBookingRequest,
    BookingResponse,
    BookingListResponse,
    StartBookingRequest,
    CancelBookingRequest,
)
from app.services.booking_service import BookingService
from app.tasks.notification_tasks import send_booking_notification

router = APIRouter(prefix="/bookings", tags=["Bookings"])


@router.post("/", response_model=BookingResponse, status_code=status.HTTP_201_CREATED)
async def create_booking(
    request: CreateBookingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = BookingService(db)
    booking = await service.create_booking(
        consumer_id=current_user.id,
        service_id=request.service_id,
        address_id=request.address_id,
        scheduled_at=request.scheduled_at,
        payment_method=request.payment_method,
    )

    # Async: notify provider of new booking request
    send_booking_notification.delay(
        booking_id=str(booking.id),
        event="new_request",
    )

    return booking


@router.patch("/{booking_id}/accept", response_model=BookingResponse)
async def accept_booking(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = BookingService(db)
    booking = await service.transition_status(
        booking_id=booking_id,
        new_status=BookingStatus.ACCEPTED,
        changed_by=current_user.id,
    )

    send_booking_notification.delay(
        booking_id=str(booking.id),
        event="accepted",
    )

    return booking


@router.patch("/{booking_id}/start", response_model=BookingResponse)
async def start_booking(
    booking_id: UUID,
    request: StartBookingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Provider enters doorstep OTP to start the service."""
    service = BookingService(db)

    # First transition to EN_ROUTE if not already
    # Then transition to IN_PROGRESS with OTP verification
    booking = await service.transition_status(
        booking_id=booking_id,
        new_status=BookingStatus.IN_PROGRESS,
        changed_by=current_user.id,
        otp=request.otp,
    )

    send_booking_notification.delay(
        booking_id=str(booking.id),
        event="in_progress",
    )

    return booking


@router.patch("/{booking_id}/complete", response_model=BookingResponse)
async def complete_booking(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = BookingService(db)
    booking = await service.transition_status(
        booking_id=booking_id,
        new_status=BookingStatus.COMPLETED,
        changed_by=current_user.id,
    )

    send_booking_notification.delay(
        booking_id=str(booking.id),
        event="completed",
    )

    return booking


@router.patch("/{booking_id}/cancel", response_model=BookingResponse)
async def cancel_booking(
    booking_id: UUID,
    request: CancelBookingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = BookingService(db)
    booking = await service.transition_status(
        booking_id=booking_id,
        new_status=BookingStatus.CANCELLED,
        changed_by=current_user.id,
        cancellation_reason=request.reason,
    )

    return booking


@router.get("/", response_model=BookingListResponse)
async def list_bookings(
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = BookingService(db)
    return await service.list_bookings(
        user_id=current_user.id,
        role=current_user.role,
        status_filter=status,
        page=page,
        per_page=per_page,
    )
```

### 6.3 `app/routers/tracking.py` (WebSocket)

```python
import json
from uuid import UUID
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from redis.asyncio import Redis
from app.dependencies import get_redis, verify_ws_token
from app.services.tracking_service import TrackingService

router = APIRouter(prefix="/tracking", tags=["Live Tracking"])


@router.websocket("/consumer/{booking_id}")
async def consumer_tracking_ws(
    websocket: WebSocket,
    booking_id: UUID,
    redis: Redis = Depends(get_redis),
):
    """Consumer subscribes to live provider location updates."""
    await websocket.accept()
    user = await verify_ws_token(websocket)

    tracking = TrackingService(redis)

    # Send last known location immediately
    latest = await tracking.get_latest_location(booking_id)
    if latest:
        await websocket.send_json(latest)

    # Stream real-time updates
    try:
        async for location in tracking.subscribe(booking_id):
            await websocket.send_json(location)
    except WebSocketDisconnect:
        pass


@router.websocket("/provider")
async def provider_tracking_ws(
    websocket: WebSocket,
    redis: Redis = Depends(get_redis),
):
    """Provider publishes their GPS location for active bookings."""
    await websocket.accept()
    user = await verify_ws_token(websocket)

    tracking = TrackingService(redis)

    try:
        while True:
            data = await websocket.receive_json()
            # Expected: { "booking_id": "...", "lat": ..., "lng": ..., "heading": ... }
            await tracking.publish_location(
                booking_id=UUID(data["booking_id"]),
                lat=data["lat"],
                lng=data["lng"],
                heading=data.get("heading"),
            )
    except WebSocketDisconnect:
        pass
```

---

## 7. Celery Tasks

### 7.1 `app/tasks/celery_app.py`

```python
from celery import Celery
from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "serviceconnect",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_default_queue="default",
    task_routes={
        "app.tasks.notification_tasks.*": {"queue": "notifications"},
        "app.tasks.payment_tasks.*": {"queue": "payments"},
    },
    beat_schedule={
        "expire-stale-bookings": {
            "task": "app.tasks.cleanup_tasks.expire_stale_bookings",
            "schedule": 30.0,  # Every 30 seconds
        },
    },
)
```

### 7.2 `app/tasks/notification_tasks.py`

```python
from app.tasks.celery_app import celery_app
from app.utils.fcm_client import send_push_notification
from app.utils.sms_client import send_sms


NOTIFICATION_TEMPLATES = {
    "new_request": {
        "title": "New Booking Request!",
        "body": "You have a new service request. Accept within 2 minutes.",
    },
    "accepted": {
        "title": "Booking Confirmed",
        "body": "Your provider has accepted the booking and will arrive soon.",
    },
    "in_progress": {
        "title": "Service Started",
        "body": "Your service is now in progress.",
    },
    "completed": {
        "title": "Service Completed",
        "body": "Your service has been completed. Please proceed to payment.",
    },
}


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    name="app.tasks.notification_tasks.send_booking_notification",
)
def send_booking_notification(self, booking_id: str, event: str):
    """Send push notification for booking events."""
    from app.database import SyncSessionLocal  # Sync session for Celery
    from app.models.booking import Booking

    try:
        with SyncSessionLocal() as db:
            booking = db.get(Booking, booking_id)
            if not booking:
                return

            template = NOTIFICATION_TEMPLATES.get(event)
            if not template:
                return

            # Determine recipient
            if event == "new_request":
                recipient = booking.provider
            else:
                recipient = booking.consumer

            if recipient.fcm_token:
                send_push_notification(
                    token=recipient.fcm_token,
                    title=template["title"],
                    body=template["body"],
                    data={"booking_id": booking_id, "event": event},
                )

    except Exception as exc:
        self.retry(exc=exc)
```

### 7.3 `app/tasks/cleanup_tasks.py`

```python
from datetime import datetime, timezone, timedelta
from sqlalchemy import update
from app.tasks.celery_app import celery_app
from app.models.booking import Booking, BookingStatus
from app.config import get_settings

settings = get_settings()


@celery_app.task(name="app.tasks.cleanup_tasks.expire_stale_bookings")
def expire_stale_bookings():
    """
    Celery Beat task: runs every 30 seconds.
    Expires REQUESTED bookings older than BOOKING_REQUEST_TIMEOUT_SECONDS.
    """
    from app.database import SyncSessionLocal

    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.BOOKING_REQUEST_TIMEOUT_SECONDS
    )

    with SyncSessionLocal() as db:
        result = db.execute(
            update(Booking)
            .where(Booking.status == BookingStatus.REQUESTED)
            .where(Booking.created_at < cutoff)
            .values(status=BookingStatus.EXPIRED)
        )
        db.commit()
        return f"Expired {result.rowcount} stale bookings"
```

---

## 8. Dependencies & Middleware

### 8.1 `app/dependencies.py`

```python
from uuid import UUID
from fastapi import Depends, HTTPException, status, WebSocket
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis as AsyncRedis
from app.database import get_db
from app.models.user import User
from app.utils.security import decode_jwt
from app.config import get_settings

settings = get_settings()
security = HTTPBearer()

# Redis connection pool (initialized in lifespan)
_redis_pool: AsyncRedis | None = None


async def get_redis() -> AsyncRedis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = AsyncRedis.from_url(
            settings.REDIS_URL, decode_responses=True
        )
    return _redis_pool


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract and validate JWT, return User object."""
    payload = decode_jwt(credentials.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user = await db.get(User, UUID(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


async def require_role(*roles: str):
    """Factory for role-based access control."""
    async def checker(user: User = Depends(get_current_user)):
        if user.role.value not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role: {', '.join(roles)}",
            )
        return user
    return checker


async def verify_ws_token(websocket: WebSocket) -> User:
    """Verify JWT from WebSocket query params."""
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return None

    payload = decode_jwt(token)
    if not payload:
        await websocket.close(code=4001, reason="Invalid token")
        return None

    return payload
```

### 8.2 `app/middleware/rate_limit.py`

```python
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from redis.asyncio import Redis


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Redis-based sliding window rate limiter.
    Applied per-IP for public endpoints, per-user for authenticated.
    """

    def __init__(self, app, redis: Redis, requests_per_minute: int = 60):
        super().__init__(app)
        self.redis = redis
        self.rpm = requests_per_minute

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health checks
        if request.url.path in ("/health", "/docs", "/openapi.json"):
            return await call_next(request)

        client_ip = request.client.host
        key = f"ratelimit:{client_ip}:{request.url.path}"

        current = await self.redis.incr(key)
        if current == 1:
            await self.redis.expire(key, 60)

        if current > self.rpm:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Try again in a minute.",
            )

        return await call_next(request)
```

---

## 9. App Factory

### `app/main.py`

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from app.config import get_settings
from app.routers import (
    auth, users, providers, services,
    bookings, payments, reviews, tracking, admin,
)
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIDMiddleware
from app.exceptions import register_exception_handlers

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    app.state.redis = Redis.from_url(
        settings.REDIS_URL, decode_responses=True
    )
    yield
    # Shutdown
    await app.state.redis.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version="1.0.0",
        lifespan=lifespan,
    )

    # Middleware (order matters — outermost first)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",       # React dev
            "https://app.serviceconnect.in",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    prefix = settings.API_V1_PREFIX
    app.include_router(auth.router, prefix=prefix)
    app.include_router(users.router, prefix=prefix)
    app.include_router(providers.router, prefix=prefix)
    app.include_router(services.router, prefix=prefix)
    app.include_router(bookings.router, prefix=prefix)
    app.include_router(payments.router, prefix=prefix)
    app.include_router(reviews.router, prefix=prefix)
    app.include_router(tracking.router, prefix=prefix)
    app.include_router(admin.router, prefix=prefix)

    # Exception handlers
    register_exception_handlers(app)

    return app


app = create_app()
```

---

## 10. Docker & Deployment

### `docker-compose.yml`

```yaml
services:
  api:
    build: .
    command: >
      uvicorn app.main:app
      --host 0.0.0.0
      --port 8000
      --workers 2
      --loop uvloop
    ports:
      - "8000:8000"
    env_file: .env
    depends_on:
      - postgres
      - redis
    volumes:
      - ./app:/code/app

  celery-worker:
    build: .
    command: >
      celery -A app.tasks.celery_app worker
      --loglevel=info
      --concurrency=2
      -Q default,notifications,payments
    env_file: .env
    depends_on:
      - redis
      - postgres

  celery-beat:
    build: .
    command: >
      celery -A app.tasks.celery_app beat
      --loglevel=info
    env_file: .env
    depends_on:
      - redis

  postgres:
    image: postgis/postgis:16-3.4
    environment:
      POSTGRES_DB: serviceconnect
      POSTGRES_USER: sc_user
      POSTGRES_PASSWORD: sc_password
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

volumes:
  pgdata:
```

### `Dockerfile`

```dockerfile
FROM python:3.11-slim

WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### `requirements.txt`

```
fastapi==0.115.*
uvicorn[standard]==0.34.*
uvloop==0.21.*
sqlalchemy[asyncio]==2.0.*
asyncpg==0.30.*
psycopg2-binary==2.9.*
geoalchemy2==0.15.*
alembic==1.14.*
pydantic==2.10.*
pydantic-settings==2.7.*
redis[hiredis]==5.2.*
celery==5.4.*
python-jose[cryptography]==3.3.*
passlib[bcrypt]==1.7.*
razorpay==1.4.*
firebase-admin==6.6.*
boto3==1.36.*
httpx==0.28.*
python-multipart==0.0.*
```

---

## 11. Error Handling

### `app/exceptions.py`

```python
from uuid import UUID
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse


class AppException(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code


class BookingNotFound(AppException):
    def __init__(self, booking_id: UUID):
        super().__init__(
            f"Booking {booking_id} not found",
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


def register_exception_handlers(app: FastAPI):
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
```

---

## 12. Testing Strategy

### 12.1 `tests/conftest.py`

```python
import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.main import create_app
from app.database import Base, get_db
from app.utils.security import create_access_token

TEST_DATABASE_URL = "postgresql+asyncpg://test:test@localhost:5432/sc_test"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest.fixture
async def db_session(engine):
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest.fixture
def auth_headers():
    """Generate JWT headers for test user."""
    def _make(user_id: str, role: str = "consumer"):
        token = create_access_token({"sub": user_id, "role": role})
        return {"Authorization": f"Bearer {token}"}
    return _make
```

### 12.2 Test Example

```python
import pytest
from uuid import uuid4


@pytest.mark.asyncio
async def test_create_booking_success(client, auth_headers, db_session):
    # Setup: create user, provider, service, address in DB
    consumer_id = str(uuid4())
    # ... factory setup ...

    response = await client.post(
        "/api/v1/bookings/",
        json={
            "service_id": str(service_id),
            "address_id": str(address_id),
            "payment_method": "online",
        },
        headers=auth_headers(consumer_id, "consumer"),
    )

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "requested"
    assert data["platform_commission"] == 20.0  # 20% of 100
    assert data["provider_payout"] == 80.0


@pytest.mark.asyncio
async def test_invalid_state_transition(client, auth_headers):
    # Try to complete a booking that's still in REQUESTED state
    response = await client.patch(
        f"/api/v1/bookings/{booking_id}/complete",
        headers=auth_headers(provider_id, "provider"),
    )

    assert response.status_code == 409
    assert "Cannot move from requested to completed" in response.json()["message"]
```

---

## 13. Key Design Decisions Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Async framework | FastAPI + asyncpg | Non-blocking I/O for WebSocket + HTTP in same process |
| ORM | SQLAlchemy 2.0 async | Mature, type-safe, async support, Alembic migrations |
| Auth | JWT (phone + OTP) | Stateless, mobile-friendly, no session cookies |
| State machine | In-code dict + service layer | Simple, auditable, no external library needed |
| Background jobs | Celery + Redis | Battle-tested, separate queues for notifications vs payments |
| Real-time tracking | WebSocket + Redis pub/sub | Low latency, no polling, Redis handles fan-out |
| Payment splits | Razorpay Route API | Native 80/20 split, handles KYC + settlement |
| Location queries | PostGIS (ST_DWithin) | Industry standard for geo queries, index-backed |
| File storage | S3-compatible (MinIO for dev) | Cheap, scalable, CDN-ready |
| Testing | pytest + httpx + factory_boy | Async-first, real DB testing, clean fixtures |
