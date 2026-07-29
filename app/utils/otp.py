"""
OTP generation + Redis-backed storage/verification.
All Redis key naming is centralised here — no other module builds OTP keys.
"""
import secrets

from redis.asyncio import Redis

from app.config import get_settings

settings = get_settings()

_ATTEMPT_SUFFIX = ":attempts"


def _otp_key(phone: str) -> str:
    """Canonical Redis key for the OTP value."""
    return f"otp:{phone}"


def _attempt_key(phone: str) -> str:
    """Canonical Redis key for the hourly attempt counter."""
    return f"otp:{phone}{_ATTEMPT_SUFFIX}"


def generate_otp(length: int | None = None) -> str:
    """Generate a cryptographically secure numeric OTP of the given length."""
    n = length or settings.OTP_LENGTH
    return "".join(str(secrets.randbelow(10)) for _ in range(n))


async def store_otp(redis: Redis, phone: str, otp: str) -> None:
    """Persist OTP with TTL and increment the hourly rate-limit counter."""
    pipe = redis.pipeline()
    pipe.setex(_otp_key(phone), settings.OTP_EXPIRY_SECONDS, otp)
    pipe.incr(_attempt_key(phone))
    pipe.expire(_attempt_key(phone), 3600)  # Reset counter every hour
    await pipe.execute()


async def is_rate_limited(redis: Redis, phone: str) -> bool:
    """Return True if the phone has exceeded OTP_MAX_ATTEMPTS_PER_HOUR."""
    count = await redis.get(_attempt_key(phone))
    return int(count or 0) >= settings.OTP_MAX_ATTEMPTS_PER_HOUR


async def otp_retry_after_seconds(redis: Redis, phone: str) -> int:
    """
    How long the caller should wait before another OTP request will succeed.

    Reads the TTL on the hourly-attempts counter. If the key has already
    vanished (rare race after a limit-hit) we fall back to 60s rather than
    returning 0 or a negative number — this ends up in the `Retry-After`
    header on a 429 response.
    """
    ttl = await redis.ttl(_attempt_key(phone))
    return ttl if ttl > 0 else 60


async def verify_otp(redis: Redis, phone: str, otp: str) -> bool:
    """
    Verify the OTP. On success, delete the stored value immediately
    (makes it single-use). Returns False on mismatch or expiry.
    """
    stored = await redis.get(_otp_key(phone))
    if not stored:
        return False
    if stored == otp:
        await redis.delete(_otp_key(phone))
        return True
    return False
