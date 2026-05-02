from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime


class CreateServiceRequest(BaseModel):
    category_id: UUID
    name: str = Field(..., min_length=3, max_length=200)
    description: str | None = Field(None, max_length=2000)
    base_price: float = Field(..., gt=0)
    duration_minutes: int = Field(default=60, ge=15, le=480)


class UpdateServiceRequest(BaseModel):
    name: str | None = Field(None, min_length=3, max_length=200)
    description: str | None = None
    base_price: float | None = Field(None, gt=0)
    duration_minutes: int | None = Field(None, ge=15, le=480)
    is_active: bool | None = None


class ServiceResponse(BaseModel):
    id: UUID
    category_id: UUID
    name: str
    description: str | None
    base_price: float
    duration_minutes: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class CategoryResponse(BaseModel):
    id: UUID
    name: str
    slug: str
    icon_url: str | None
    description: str | None
    sort_order: int

    model_config = {"from_attributes": True}
