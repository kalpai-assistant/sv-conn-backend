"""Marketplace analytics events from the customer app (SVC-419). ADMIN.md lists them.

One event per request, login required. ``user_id`` is the caller's, never the
body's. 204 whether the event was stored or was a retry of one already stored.
"""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.analytics import TYPED_ID_FIELDS, ClientEventBody
from app.services.marketplace_events import record_client_event
from app.utils.rate_limiter import rate_limit

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.post(
    "/events",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(rate_limit("analytics_events", max_requests=600))],
)
async def ingest_event(
    body: ClientEventBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Store one marketplace event. 422 for a name the app may not send, a field
    the event doesn't declare, a value out of bounds, or an id that names no row
    (``ANALYTICS_UNKNOWN_REFERENCE``, nothing stored). A repeated ``event_id``
    stores nothing and is still 204."""
    event = body.root
    payload = event.properties.model_dump(mode="json")
    ids = {field: getattr(event.properties, field, None) for field in TYPED_ID_FIELDS}
    for field in TYPED_ID_FIELDS:
        payload.pop(field, None)
    await record_client_event(
        db,
        event_id=event.event_id,
        name=event.name,
        occurred_at=event.occurred_at,
        user_id=current_user.id,
        payload=payload,
        **ids,
    )
