from decimal import Decimal
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    APP_NAME: str = "Eazyserv"
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
    # Decimal, not float: this rate is the source of every booking's money split, so
    # a binary-approximated 20.0 would seed rounding error into provider_payout
    # before it is ever stored (SVC-150).
    PLATFORM_COMMISSION_PERCENT: Decimal = Decimal("20.0")
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

    # CORS (B4). Comma-separated origins the browser is allowed to send
    # `Origin:` from. Default covers dev + Capacitor + prod web; staging
    # / other envs should override via env var. `*` alone is DELIBERATELY
    # NOT accepted — allow_credentials=True with a wildcard origin is a
    # spec violation the browser will silently reject.
    CORS_ALLOWED_ORIGINS: str = ",".join(
        [
            "http://localhost:3000",  # React dev server
            "http://localhost:8080",  # Vite dev (alternate port)
            "https://app.eazyserv.com",  # Production web
            # Capacitor mobile app origins
            "capacitor://localhost",  # iOS Capacitor WebView
            "https://localhost",  # Android Capacitor WebView (androidScheme=https)
            "http://localhost",  # Android Capacitor WebView (fallback)
        ]
    )

    # Admin IP allowlist (SVC-75) — comma-separated CIDRs. Empty = no restriction
    # (dev-safe default). Production MUST populate. See backend/ADMIN.md.
    ADMIN_ALLOWED_IPS: str = ""
    # Trust X-Forwarded-For / X-Real-IP for client IP resolution. Only enable
    # behind a load balancer / CDN that strips inbound XFF headers — otherwise
    # any client can spoof their IP.
    ADMIN_TRUST_FORWARDED_FOR: bool = False

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

    # E2E test OTP bypass (SVC-52). Skips SMS + uses a fixed OTP for a
    # whitelist of phone numbers, so nightly smoke tests can complete auth
    # against staging without an SMS provider in the loop. **Hard-refuses
    # to activate when ENVIRONMENT == "production"** — see AuthService.
    # Both empty in dev/prod defaults; only set in staging env.
    E2E_TEST_PHONES: str = ""  # comma-separated E.164 numbers
    E2E_TEST_OTP: str = ""  # fixed OTP to store for those numbers

    # SVC-161: SMS-a-link on the /download page. Off by default because
    # sending a URL via MSG91's OTP template produces "Your Eazyserv OTP is
    # https://..." — misleading. Flip on AFTER a dedicated MSG91 template
    # is approved (submit form → 1-3 business days). While off, the endpoint
    # returns 501 and the /download page falls back to QR-code-only.
    SMS_DOWNLOAD_LINK_ENABLED: bool = False

    # Observability (SVC-46)
    # Environment tag propagated to Sentry + log records. staging | production | dev.
    ENVIRONMENT: str = "dev"
    # Sentry DSN. Empty = Sentry disabled (dev-safe default).
    SENTRY_DSN: str = ""
    # Fraction of transactions sampled for perf tracing. Kept low in prod
    # (Sentry billing) — bump for a debugging session, don't leave at 1.0.
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    # JSON logs in staging/prod (parseable by CloudWatch / Loki / etc);
    # human-readable console renderer in dev.
    LOG_JSON: bool = False
    LOG_LEVEL: str = "INFO"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
