"""
Redis pub/sub real-time location tracking service — exact from LLD.
"""
import json
import time
from uuid import UUID

from redis.asyncio import Redis

TRACKING_CHANNEL_PREFIX = "tracking:"
LOCATION_TTL_SECONDS = 60  # Expire stale locations after 60 s


class TrackingService:
    """
    Redis pub/sub for real-time provider location streaming.

    Flow:
    1. Provider WebSocket publishes location to Redis channel.
    2. Consumer WebSocket subscribes to the same channel.
    3. Each update is also cached as the latest known position (for late joiners).
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
    ) -> None:
        """Provider publishes their current location."""
        payload = json.dumps({
            "lat": lat,
            "lng": lng,
            "heading": heading,
            "timestamp": time.time(),
        })

        channel = self.channel_name(booking_id)
        await self.redis.publish(channel, payload)

        # Cache latest position for late-joining consumers
        await self.redis.setex(f"location:{booking_id}", LOCATION_TTL_SECONDS, payload)

    async def get_latest_location(self, booking_id: UUID) -> dict | None:
        """Return the last cached position, or None if expired."""
        data = await self.redis.get(f"location:{booking_id}")
        if data:
            return json.loads(data)
        return None

    async def subscribe(self, booking_id: UUID):
        """
        Async generator that yields location updates from Redis pub/sub.
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
