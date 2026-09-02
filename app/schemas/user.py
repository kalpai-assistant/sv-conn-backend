from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

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


class PushCapabilityReport(BaseModel):
    """What the client can tell us about its ability to receive push (SVC-255).

    Sent on every registration attempt, successful or not. That "or not" is the
    whole point: Sentry already records failures, but with no denominator —
    "12 failures this week" is unreadable without knowing whether that is 12 out
    of 12 or 12 out of 400. Reporting every attempt supplies the denominator.

    Nothing here is stored. The handler logs one line and returns; the question
    it answers ("how many providers can actually receive push, and on what?") is
    a one-off scoping question, not a per-user fact worth a column.
    """

    platform: Literal["android", "ios", "desktop"]
    # Native Capacitor build versus a browser. Native Android push works, so
    # this field alone separates the safe population from the exposed one.
    is_native: bool
    # iOS only, and the field the whole ticket turns on: Apple refuses a Web
    # Push subscription until the site is installed to the Home Screen.
    ios_standalone: bool = False
    # Chrome on iOS cannot install to the Home Screen at all, so its remedy
    # differs from Safari's and it must be counted separately.
    is_chrome_ios: bool = False
    push_permission: Literal["granted", "denied", "prompt", "unsupported"]
    # Whether a token actually reached us. Only meaningful once the Firebase
    # config is deployed everywhere — before that it reads 0% for config
    # reasons rather than platform ones.
    token_obtained: bool
    # Whether the user asked for this (tapped Enable) or it ran on app start.
    # Separates "never tried" from "tried and it still didn't work", which are
    # different problems with different fixes.
    interactive: bool = False


class UpdateFCMTokenRequest(BaseModel):
    """Register or clear the caller's FCM token.

    `fcm_token` is nullable but *required* — `null` clears the stored token
    (sign-out), while an omitted field is a 422. Without that distinction an
    empty body would silently wipe push for the user.
    """

    fcm_token: str | None = Field(..., max_length=255)


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
