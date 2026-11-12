"""SVC-433: the support console's API. Roles in ADMIN.md: every route here is open
to admin, ops and support.

The booking and SP detail panels reuse ``GET /admin/bookings/{id}`` (+
``/timeline``), ``GET /admin/providers/{id}`` (+ ``/team``) and
``GET /admin/bookings?provider_id=`` / ``?consumer_id=``. Refunds are the SVC-431
dispute routes, unchanged. Every write here is audited.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_roles
from app.models.user import User
from app.routers.admin import _for_admin_with_parties
from app.schemas.admin_support import (
    ContactedCreate,
    CustomerProfile,
    EligibleProsResponse,
    LookupResponse,
    ReassignProRequest,
    RetryDispatchResponse,
    SupportNote,
    SupportNoteCreate,
)
from app.schemas.booking import BookingResponse
from app.services.admin_audit import audit_actor
from app.services.admin_support_service import AdminSupportService

router = APIRouter(prefix="/admin", tags=["Admin"])

_any_admin_role = require_roles("admin", "ops", "support")


@router.get(
    "/support/lookup", response_model=LookupResponse, dependencies=[Depends(_any_admin_role)]
)
async def support_lookup(
    q: str = Query(..., min_length=3, max_length=100, description="Phone, booking id or SP name"),
    db: AsyncSession = Depends(get_db),
):
    """One search box. The input is tried as every type it fits, and each one is
    searched:

    * **phone**: digits with optional spaces, dashes, brackets and a +91 / 91 / 0
      prefix, 10 digits after that → customers, SPs and team Pros with that
      number;
    * **booking_id**: a full uuid → that booking. **booking_id_suffix**: exactly
      6 hex characters → bookings whose id ends with them;
    * **name**: anything with a letter (not a full uuid) → SPs whose name or
      business name contains every word.

    "facade" is both a suffix and a name, so both run: ``interpreted_as`` lists
    what was searched, and results come back grouped by kind. At most 20 per
    kind; ``truncated`` names a kind that had more.
    """
    return await AdminSupportService(db).lookup(q)


@router.get(
    "/support/customers/{user_id}",
    response_model=CustomerProfile,
    dependencies=[Depends(_any_admin_role)],
    responses={404: {"description": "USER_NOT_FOUND: no such customer"}},
)
async def support_customer(user_id: UUID, db: AsyncSession = Depends(get_db)):
    """A customer's profile, lifetime numbers and last 10 bookings, newest first.
    Deleted accounts too (``deleted_at`` set). See ``CustomerProfile`` for what
    lifetime spend counts."""
    return await AdminSupportService(db).customer(user_id)


@router.get(
    "/bookings/{booking_id}/notes",
    response_model=list[SupportNote],
    dependencies=[Depends(_any_admin_role)],
    responses={404: {"description": "No such booking"}},
)
async def list_support_notes(booking_id: UUID, db: AsyncSession = Depends(get_db)):
    """Support's notes and "contacted" marks on a booking, newest first, each
    with its author."""
    return await AdminSupportService(db).list_notes(booking_id)


@router.post(
    "/bookings/{booking_id}/notes",
    response_model=SupportNote,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "No such booking"}},
)
async def add_support_note(
    booking_id: UUID,
    body: SupportNoteCreate,
    request: Request,
    admin: User = Depends(_any_admin_role),
    db: AsyncSession = Depends(get_db),
):
    """Add a note. Notes are append-only: there is no edit or delete. 422
    ``NOTE_EMPTY`` for only whitespace. Audited as ``booking.note_add``."""
    return await AdminSupportService(db).add_note(
        audit_actor(request, admin), booking_id, "note", body.body
    )


@router.post(
    "/bookings/{booking_id}/contacted",
    response_model=SupportNote,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "No such booking"}},
)
async def mark_contacted(
    booking_id: UUID,
    request: Request,
    body: ContactedCreate | None = None,
    admin: User = Depends(_any_admin_role),
    db: AsyncSession = Depends(get_db),
):
    """Record that support contacted the customer. Text is optional. Audited as
    ``booking.contacted``."""
    return await AdminSupportService(db).add_note(
        audit_actor(request, admin), booking_id, "contacted", body.body if body else None
    )


@router.post(
    "/bookings/{booking_id}/retry-dispatch",
    response_model=RetryDispatchResponse,
    responses={
        404: {"description": "No such booking"},
        409: {
            "description": "BOOKING_NOT_REQUESTED (details.status): only a booking still "
            "waiting for a provider; DISPATCH_RETRY_TOO_SOON (details.retry_after_seconds): "
            "retried in the last 30 s; NO_ELIGIBLE_PROVIDERS (details.reason, e.g. "
            "none_online): nobody to offer it to, and nothing was changed"
        },
    },
)
async def retry_dispatch(
    booking_id: UUID,
    request: Request,
    admin: User = Depends(_any_admin_role),
    db: AsyncSession = Depends(get_db),
):
    """Offer a waiting booking to providers again: the same broadcast a new
    booking gets, with a fresh offer window. No body. SPs who already declined
    it aren't asked again. The customer isn't notified. Audited as
    ``booking.retry_dispatch``."""
    return await AdminSupportService(db).retry_dispatch(audit_actor(request, admin), booking_id)


@router.get(
    "/bookings/{booking_id}/eligible-pros",
    response_model=EligibleProsResponse,
    dependencies=[Depends(_any_admin_role)],
    responses={
        404: {"description": "No such booking"},
        409: {"description": "BOOKING_NOT_REASSIGNABLE: not ACCEPTED with a provider"},
    },
)
async def eligible_pros(booking_id: UUID, db: AsyncSession = Depends(get_db)):
    """The booking's SP's active Pros, and for each whether a reassign would be
    accepted (``refusal_code`` is the 409 it would get)."""
    return await AdminSupportService(db).eligible_pros(booking_id)


@router.post(
    "/bookings/{booking_id}/reassign-pro",
    response_model=BookingResponse,
    responses={
        404: {"description": "No such booking; PRO_NOT_FOUND: not an active Pro of this SP"},
        409: {
            "description": "BOOKING_NOT_REASSIGNABLE (details.status): not ACCEPTED with a "
            "provider; or the Pro can't take it: PRO_DECLINED_THIS_BOOKING, "
            "SELF_NOT_TAKING_JOBS, PRO_KYC_INCOMPLETE, PRO_OFFLINE, PRO_BUSY"
        },
    },
)
async def reassign_pro(
    booking_id: UUID,
    body: ReassignProRequest,
    request: Request,
    admin: User = Depends(_any_admin_role),
    db: AsyncSession = Depends(get_db),
):
    """Give an accepted booking to another Pro of the same SP, by the same rules
    as the SP's own re-pick. The new team Pro is pushed, and so is the old one.
    The SP and the customer are not. The same Pro again is a 200 with no
    change. Audited as ``booking.reassign_pro``. Returns the admin booking
    detail."""
    booking = await AdminSupportService(db).reassign_pro(
        audit_actor(request, admin), booking_id, body.professional_id
    )
    return await _for_admin_with_parties(db, booking)
