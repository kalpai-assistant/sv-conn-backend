"""SVC-437: request and response shapes for ``POST /catalog/similarity``."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.service_request import DESCRIPTION_MAX


class SimilarityRequest(BaseModel):
    # 200 = services.name. No min length: a short name returns [] (the SP app
    # calls this while the user types), not a 422.
    proposed_name: str = Field(max_length=200)
    # Same cap as a submitted request. Unbounded, one SP could make every row's
    # similarity() re-scan a megabyte of text, 30 times a minute.
    proposed_description: str = Field("", max_length=DESCRIPTION_MAX)
    category_id: uuid.UUID | None = None


class SimilarityHitResponse(BaseModel):
    """A catalog hit has ``service_id``; a pending hit has ``request_id``. A
    pending hit never carries the other SP's id, description or price."""

    service_id: uuid.UUID | None
    request_id: uuid.UUID | None
    service_name: str
    category_name: str
    score: float
    source: Literal["catalog", "pending_request"]
