from pydantic import field_validator
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # App
    APP_NAME: str = "ServiceConnect"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # Database
    DATABASE_URL: str  # postgresql+asyncpg://user:pass@host:5432/db
    SYNC_DATABASE_URL: str = ""  # postgresql+psycopg2://... used by Celery
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
    RAZORPAY_WEBHOOK_SECRET: str = ""  # Set in Razorpay dashboard → Webhooks
    PLATFORM_COMMISSION_PERCENT: float = 20.0
    # RazorpayX virtual account that funds provider payouts. Blank until RazorpayX
    # is activated; payout creation requires it.
    RAZORPAYX_ACCOUNT_NUMBER: str = ""
    # RazorpayX (payouts) is a separate product with its own webhook secret,
    # configured in the RazorpayX dashboard independently of the Payments webhook.
    RAZORPAYX_WEBHOOK_SECRET: str = ""
    # Look-back window (seconds) for the nightly payout reconciliation. Overlaps
    # previous runs so nothing slips between them. Default 48h.
    PAYOUT_RECONCILE_WINDOW_SECONDS: int = 172_800
    # Bank-payout mode threshold (in paise). IMPS is instant but capped at ~₹5L per
    # transaction; bank payouts at/under this go IMPS, above it use the high-value
    # mode below. Configurable so the cap can be tuned without a code change if the
    # limit moves.
    IMPS_MAX_PAYOUT_PAISE: int = 50_000_000  # ₹5,00,000
    # Mode for bank payouts above the IMPS cap: "NEFT" (default) or "RTGS" (real-time,
    # for high value). RTGS clears faster but is a distinct rail — switch here without
    # a code change.
    BANK_PAYOUT_ABOVE_CAP_MODE: str = "NEFT"  # "NEFT" | "RTGS"

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

    # Encryption (AES-256) for PII at rest
    ENCRYPTION_KEY: str = ""

    @field_validator("BANK_PAYOUT_ABOVE_CAP_MODE")
    @classmethod
    def _validate_above_cap_mode(cls, v: str) -> str:
        allowed = {"NEFT", "RTGS"}
        if v not in allowed:
            raise ValueError(
                f"BANK_PAYOUT_ABOVE_CAP_MODE must be one of {sorted(allowed)}, got {v!r}"
            )
        return v

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
