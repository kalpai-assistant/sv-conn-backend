"""SP team management (SVC-320): invites, the team list, accept-invite."""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models.provider_profile import KYCStatus
from app.models.provider_team_invite import INVITE_CODE_DIGITS

# The form auth stores in ``users.phone`` (``SendOTPRequest``'s pattern).
_CANONICAL_PHONE_RE = re.compile(r"^\+91[6-9]\d{9}$")
_PHONE_NOISE_RE = re.compile(r"[\s\-()]")


def normalize_indian_phone(raw: str) -> str:
    """Canonicalise to ``+91XXXXXXXXXX``, the form ``users.phone`` holds.

    Auth only ever accepts that exact form, so an invite stored any other way
    could never match the invitee's account at accept time. An SP typing a
    number in by hand is less tidy than an OTP screen, so this forgives spaces,
    dashes, brackets and a missing ``+91`` / ``+`` before validating.
    """
    phone = _PHONE_NOISE_RE.sub("", raw)
    if re.fullmatch(r"\d{10}", phone):
        phone = f"+91{phone}"
    elif re.fullmatch(r"91\d{10}", phone):
        phone = f"+{phone}"
    if not _CANONICAL_PHONE_RE.fullmatch(phone):
        raise ValueError("must be an Indian mobile number, e.g. +919876543210")
    return phone


class TeamInviteCreate(BaseModel):
    invitee_phone: str = Field(..., max_length=20, examples=["+919876543210"])

    @field_validator("invitee_phone")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return normalize_indian_phone(value)


class TeamInviteResponse(BaseModel):
    """The SP's view of one invite.

    Carries the code (``token``) and the static ``/invite`` URL. The SP is the
    one sending it, so they can pass the code on another way if the SMS doesn't
    arrive; it only works from the invited phone.
    """

    id: UUID
    invitee_phone: str
    status: str
    expires_at: datetime
    created_at: datetime
    token: str
    invite_url: str

    model_config = {"from_attributes": True}


class AcceptInviteRequest(BaseModel):
    # The invite code. Named ``token`` to keep the agreed body shape.
    token: str = Field(..., pattern=rf"^\d{{{INVITE_CODE_DIGITS}}}$", examples=["004217"])


class ProfessionalResponse(BaseModel):
    """A Pro row. ``is_self`` marks an SP's Pro-for-self."""

    id: UUID
    service_provider_id: UUID
    user_id: UUID
    display_name: str
    profile_photo_url: str | None
    kyc_status: KYCStatus
    is_online: bool
    is_active: bool
    is_self: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Two-level dispatch (SVC-320 PR 2) -----------------------------------------


class AssignRequest(BaseModel):
    professional_id: UUID


class TeamPickPro(BaseModel):
    """One row of the pick-a-Pro modal."""

    id: UUID
    display_name: str
    profile_photo_url: str | None
    is_self: bool
    # Already on another unfinished job; /assign would answer 409 PRO_BUSY.
    busy: bool


class TeamPickResponse(BaseModel):
    """202 from ``PATCH /bookings/{id}/accept`` with ``X-Team-Pick: 1``, for a
    team SP. Nothing has been claimed: the claim is ``POST /bookings/{id}/assign``."""

    booking_id: UUID
    pros: list[TeamPickPro]


class DeclineJobResponse(BaseModel):
    booking_id: UUID
    # awaiting_repick: the SP has another Pro and has been asked to pick one.
    # reopened: nobody else on the team, so the job went back to the broadcast.
    outcome: Literal["awaiting_repick", "reopened"]
