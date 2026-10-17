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

    `platform` (SVC-371) says which app the token belongs to, so the release
    push can skip other platforms. It is optional, unlike the ticket's wording:
    installed APKs predate it and a required field would 422 their
    registration, losing them push entirely. Omitted means stored as NULL,
    which still gets every release push.
    """

    fcm_token: str | None = Field(..., max_length=255)
    platform: Literal["android", "ios", "web"] | None = None


class DeleteAccountRequest(BaseModel):
    """Optional body for ``DELETE /users/me`` (SVC-367).

    The refresh token, when sent, is revoked like ``/auth/logout`` does. It is
    optional because the account going inactive already makes every refresh
    token for it fail; this just puts the caller's on the blacklist too.
    """

    refresh_token: str | None = None


# Six digits, nothing else. Indian PIN codes are fixed-width, so a length check
# is a real check rather than a formality — it catches the common paste of a
# phone number or a partial code.
PINCODE_PATTERN = r"^\d{6}$"

# Matches `users.phone`, so a tap-to-call link does not have to care whether the
# number came from the account or from an address's contact person.
PHONE_PATTERN = r"^\+91\d{10}$"


class AddressResponse(BaseModel):
    id: UUID
    label: str
    full_address: str
    is_default: bool
    # SVC-281: the pin the consumer dropped, handed back.
    #
    # `CreateAddressRequest` has always accepted lat/lng and `addresses.location`
    # has always stored them, but no response returned them — write-only, the
    # same shape the provider service area had before #156. The cost showed up
    # on the address map: with nowhere to open, it fell back to a hardcoded
    # Bangalore for every user, including ones with a saved address 840km away.
    #
    # Nullable because `location` is: an address may be created without a pin
    # (the field is optional on create), and older rows predate it. `null` means
    # "no pin", which callers must not conflate with 0,0 — a real point in the
    # Gulf of Guinea.
    #
    # Read off the model's existing `lat`/`lng` properties, which decode the
    # geography column in Python — see the note there on why not ST_X/ST_Y.
    lat: float | None = None
    lng: float | None = None

    # SVC-287. Returned, not just accepted: without these the edit form has
    # nothing to prefill and the user retypes their flat number on every edit —
    # which is exactly the weakness that ruled out composing these fields into
    # one string on the client.
    house_floor: str | None = None
    building: str | None = None
    landmark_area: str | None = None
    pincode: str | None = None
    contact_name: str | None = None
    contact_phone: str | None = None

    model_config = {"from_attributes": True}


class UpdateAddressRequest(BaseModel):
    """Edit a saved address (SVC-277). Every field optional — this is a PATCH.

    Served by a copy-on-write update: the old row is soft-deleted and a new one
    created, so bookings that already point at the old address keep recording
    where the job actually happened. See `UserService.update_address`.
    """

    label: str | None = Field(default=None, pattern=r"^(home|work|other)$")
    full_address: str | None = Field(default=None, min_length=5, max_length=500)
    # A pair or neither: one without the other would build a point half from the
    # old address and half from the new.
    lat: float | None = None
    lng: float | None = None
    is_default: bool | None = None
    # SVC-287. Optional here even though `house_floor` is required on create:
    # this is a PATCH, and demanding it back would break editing the label
    # alone. Unset fields keep whatever the row already holds — see
    # `AddressDetails.merged_with`.
    house_floor: str | None = Field(default=None, max_length=120)
    building: str | None = Field(default=None, max_length=120)
    landmark_area: str | None = Field(default=None, max_length=200)
    pincode: str | None = Field(default=None, pattern=PINCODE_PATTERN)
    contact_name: str | None = Field(default=None, max_length=120)
    contact_phone: str | None = Field(default=None, pattern=PHONE_PATTERN)


class CreateAddressRequest(BaseModel):
    label: str = Field(default="home", pattern=r"^(home|work|other)$")
    full_address: str = Field(..., min_length=5, max_length=500)
    lat: float | None = None
    lng: float | None = None
    is_default: bool = False

    # SVC-287: the parts a map cannot supply.
    #
    # `house_floor` is required here and nowhere else. A pin finds the
    # building; it does not find the door, and a provider standing outside a
    # twelve-flat block with no number is the failure this whole ticket exists
    # to stop.
    #
    # It shipped optional first, deliberately: when the column landed, no
    # client sent the field, so requiring it would have turned every "add
    # address" on every installed build into a 422 for something the app had
    # no way to provide. The form ships it now, so the gate closes.
    #
    # Required at the *API*, never at the column. Every address saved before
    # this has NULL there and must stay valid, which is also why this needs no
    # migration — nothing about the table changes.
    #
    # `UpdateAddressRequest` deliberately leaves it optional: that is a PATCH,
    # and demanding a flat number to rename an address to "Work" would make
    # legacy rows uneditable.
    house_floor: str = Field(..., min_length=1, max_length=120)
    building: str | None = Field(default=None, max_length=120)
    landmark_area: str | None = Field(default=None, max_length=200)
    # Customer-typed. Never the geocoder's — Nominatim answers 411001 for
    # Kharadi, which is 411014, at every zoom level (SVC-284).
    pincode: str | None = Field(default=None, pattern=PINCODE_PATTERN)
    contact_name: str | None = Field(default=None, max_length=120)
    contact_phone: str | None = Field(default=None, pattern=PHONE_PATTERN)
