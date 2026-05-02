from pydantic import BaseModel, Field, EmailStr
from uuid import UUID
from datetime import datetime
from app.models.user import UserRole


class UserResponse(BaseModel):
    id: UUID
    phone: str
    name: str | None
    email: str | None
    role: UserRole
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UpdateProfileRequest(BaseModel):
    name: str | None = Field(None, max_length=100)
    email: str | None = Field(None, max_length=255)


class UpdateFCMTokenRequest(BaseModel):
    fcm_token: str = Field(..., max_length=255)


class AddressResponse(BaseModel):
    id: UUID
    label: str
    full_address: str
    is_default: bool

    model_config = {"from_attributes": True}


class CreateAddressRequest(BaseModel):
    label: str = Field(default="home", pattern=r"^(home|work|other)$")
    full_address: str = Field(..., min_length=5, max_length=500)
    lat: float | None = None
    lng: float | None = None
    is_default: bool = False
