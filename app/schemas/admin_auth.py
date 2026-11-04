from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.user import AdminRole


class AdminLoginRequest(BaseModel):
    # A plain string, not EmailStr: a malformed address gets the same 401 as any
    # other failed login. The cap only stops absurd bodies.
    email: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=1024)


class AdminLoginResponse(BaseModel):
    requires_totp: bool
    totp_setup_required: bool
    challenge_token: str


class TOTPSetupRequest(BaseModel):
    challenge_token: str


class TOTPSetupResponse(BaseModel):
    otpauth_url: str


class TOTPVerifyRequest(BaseModel):
    challenge_token: str
    code: str = Field(..., pattern=r"^\d{6}$")


class AdminUserResponse(BaseModel):
    id: UUID
    email: str
    # admin_role on the row: users.role is ADMIN for all three.
    role: AdminRole = Field(validation_alias="admin_role")
    last_login_at: datetime | None

    model_config = {"from_attributes": True}


class AdminSessionResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    admin: AdminUserResponse
