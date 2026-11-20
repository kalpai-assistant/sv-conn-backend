from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ServiceAlertRequest(BaseModel):
    """The point the tile was shown for: the same lat/lng the app sent to
    ``GET /services/from-prices``."""

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class ServiceAlertResponse(BaseModel):
    """One open "Notify me" alert (SVC-484). ``kind`` is what the tile said
    when the customer tapped: ``coming_soon`` or ``no_pros_online``. After
    ``expires_at`` (created + 45 days) it's gone without a push."""

    service_id: UUID
    service_name: str
    kind: Literal["coming_soon", "no_pros_online"]
    created_at: datetime
    expires_at: datetime
