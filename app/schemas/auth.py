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


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: str
    role: str
    is_new_user: bool


class RefreshTokenRequest(BaseModel):
    refresh_token: str
