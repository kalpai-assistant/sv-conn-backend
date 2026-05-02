"""
WebSocket endpoints for real-time provider location tracking — exact from LLD.
"""
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
    user_payload = await verify_ws_token(websocket)
    if not user_payload:
        return  # Connection already closed by verify_ws_token

    tracking = TrackingService(redis)

    # Send last known location immediately (for late joiners)
    latest = await tracking.get_latest_location(booking_id)
    if latest:
        await websocket.send_json(latest)

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
    """Provider publishes GPS location for active bookings."""
    await websocket.accept()
    user_payload = await verify_ws_token(websocket)
    if not user_payload:
        return

    tracking = TrackingService(redis)

    try:
        while True:
            data = await websocket.receive_json()
            # Expects: { "booking_id": "...", "lat": ..., "lng": ..., "heading": ... }
            await tracking.publish_location(
                booking_id=UUID(data["booking_id"]),
                lat=data["lat"],
                lng=data["lng"],
                heading=data.get("heading"),
            )
    except WebSocketDisconnect:
        pass
