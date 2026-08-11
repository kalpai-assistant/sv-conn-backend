"""Request-level idempotency (SVC-158).

Backs the ``Idempotency-Key`` header on side-effecting endpoints.
Immediate motivation: the create-booking flow. Users on flaky mobile
networks double-tap the Book button; without this, both requests reach
the server and we create two bookings + charge twice.

Design mirrors Stripe:
    - Client generates a UUID per user intent, sends as ``Idempotency-Key``.
    - Same key + same body inside the TTL window returns the cached response.
    - Same key + different body is a client bug → 422.
    - Concurrent requests (same key still in flight) → 409, client retries.

Storage is Redis, TTL 24h, key ``idempotency:{user_id}:{key}``. Scoped by
user so a collision between two users' keys can't leak responses.
"""

import hashlib
import json
from typing import Any

from redis.asyncio import Redis

from app.exceptions import IdempotencyInProgress, IdempotencyKeyReused

# 24h matches Stripe's window — long enough that a user closing + reopening
# the app hours later still gets the same response, short enough that Redis
# doesn't grow unboundedly.
TTL_SECONDS = 24 * 60 * 60

# TTL on the pending sentinel — cap on how long we'll wait for a stuck
# request. A handler that takes >60s to complete has bigger problems; if
# it does die without a rollback, a stale lock only blocks the same key
# for a minute before the sentinel expires.
PENDING_TTL_SECONDS = 60


def hash_body(body: dict[str, Any]) -> str:
    """Deterministic short hash of a request body, for reuse-with-different-body
    detection. sort_keys=True so JSON key ordering doesn't flip the hash."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]


class IdempotencyStore:
    def __init__(self, redis: Redis, user_id: str, key: str) -> None:
        self.redis = redis
        self.cache_key = f"idempotency:{user_id}:{key}"

    async def check(self, body_hash: str) -> tuple[int, Any] | None:
        """Return the cached ``(status_code, body)`` for a completed response,
        or ``None`` if this key hasn't been seen.

        Raises ``IdempotencyKeyReused`` on body-hash mismatch,
        ``IdempotencyInProgress`` if a concurrent request holds the lock.
        """
        raw = await self.redis.get(self.cache_key)
        if raw is None:
            return None
        entry = json.loads(raw)
        if entry["body_hash"] != body_hash:
            raise IdempotencyKeyReused()
        if entry["status"] == "pending":
            raise IdempotencyInProgress()
        return entry["response_status"], entry["response_body"]

    async def reserve(self, body_hash: str) -> bool:
        """Atomically claim the lock. Returns True if we got it (caller runs
        the handler), False if someone else did (caller should re-check)."""
        entry = json.dumps({"status": "pending", "body_hash": body_hash})
        return bool(await self.redis.set(self.cache_key, entry, nx=True, ex=PENDING_TTL_SECONDS))

    async def commit(self, body_hash: str, status_code: int, response_body: Any) -> None:
        """Overwrite the pending sentinel with the completed response."""
        entry = json.dumps(
            {
                "status": "done",
                "body_hash": body_hash,
                "response_status": status_code,
                "response_body": response_body,
            }
        )
        await self.redis.set(self.cache_key, entry, ex=TTL_SECONDS)

    async def rollback(self) -> None:
        """Drop the pending sentinel on handler failure so a retry isn't
        blocked by a stale lock (would otherwise wait out PENDING_TTL_SECONDS)."""
        await self.redis.delete(self.cache_key)
