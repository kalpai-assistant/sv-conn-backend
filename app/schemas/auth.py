from pydantic import BaseModel, Field


class SendOTPRequest(BaseModel):
    phone: str = Field(
        ...,
        pattern=r"^\+91[6-9]\d{9}$",
        description="Indian phone number with +91 prefix",
    )


class VerifyOTPRequest(BaseModel):
    phone: str = Field(..., pattern=r"^\+91[6-9]\d{9}$")
    otp: str = Field(..., min_length=6, max_length=6)
    role: str = Field(default="consumer", pattern=r"^(consumer|provider)$")


class AdminVerifyOTPRequest(BaseModel):
    """SVC-345: admin verify-otp body. No `role` field — the dedicated admin
    endpoint only ever issues an admin token, and the role is derived from the
    user row, not the request."""

    phone: str = Field(..., pattern=r"^\+91[6-9]\d{9}$")
    otp: str = Field(..., min_length=6, max_length=6)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: str
    role: str
    is_new_user: bool


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    """SVC-241: refresh token to blacklist. The access token is short-lived
    and expires on its own; the refresh token is the durable credential a
    thief could otherwise use to mint fresh access tokens for 30 days.
    """

    refresh_token: str
