"""SVC-436: shapes for SP service requests and request bans.

No route uses these yet. SA3 (submission) and SA5-SA7 (admin review) build on
them. Money is rupees: ``Decimal`` on input, so a price is never rounded through
a float, and a JSON number (``float``) on output, never a Decimal string.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.price_unit import PriceUnit
from app.models.service_request import ServiceRequestStatus

DESCRIPTION_MAX = 2000


def _required_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


class ServiceRequestCreate(BaseModel):
    """What an SP proposes."""

    proposed_name: str = Field(max_length=200)
    proposed_description: str = Field(max_length=DESCRIPTION_MAX)
    proposed_category_id: uuid.UUID
    # Numeric(10, 2): up to 99,999,999.99.
    proposed_price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    proposed_price_unit: PriceUnit

    _strip = field_validator("proposed_name", "proposed_description")(_required_text)


class ServiceRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sp_id: uuid.UUID
    proposed_name: str
    proposed_description: str
    proposed_category_id: uuid.UUID
    proposed_price: float
    proposed_price_unit: PriceUnit
    status: ServiceRequestStatus
    rejection_reason: str | None
    recommended_service_id: uuid.UUID | None
    created_service_id: uuid.UUID | None
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SpRequestBanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sp_id: uuid.UUID
    banned_until: datetime
    reason: str
    created_at: datetime
    cleared_at: datetime | None
    cleared_by: uuid.UUID | None
