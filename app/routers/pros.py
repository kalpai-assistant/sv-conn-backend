"""The Pro's own surface (SVC-320).

PR 2 adds only the decline; the rest of ``/pros/me/*`` (profile, jobs, online,
location, KYC, en-route/start/complete) is PR 3.

No ``require_role`` here, on purpose: a Pro is whoever has an active
``professionals`` row, and most of them signed up as consumers (the app's sign-up
always sends ``role=consumer``). Each endpoint authorizes on that row instead.
"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.team import DeclineJobResponse
from app.services.booking_service import BookingService
from app.tasks.notification_tasks import notify_reopened, send_booking_notification

router = APIRouter(prefix="/pros", tags=["Pros"])


@router.post(
    "/me/jobs/{booking_id}/decline",
    response_model=DeclineJobResponse,
    responses={
        404: {"description": "Not a job assigned to me (or no such booking)"},
        409: {"description": "Too late to decline: already en route, or finished"},
    },
)
async def decline_job(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn down a job I've been assigned, before setting off.

    If my SP has someone else on the team, the job stays with them and they're
    asked to pick another Pro (``awaiting_repick``). Otherwise it goes back to
    the broadcast for other SPs (``reopened``), and the consumer is told we're
    finding someone else.
    """
    booking, outcome = await BookingService(db).decline_job(booking_id, current_user.id)
    if outcome == "reopened":
        notify_reopened(booking.id)
    elif booking.provider_id != current_user.id:
        # An SP declining their own Pro-for-self's job doesn't need telling.
        send_booking_notification.delay(
            booking_id=str(booking.id), event="pro_declined", actor_id=str(current_user.id)
        )
    return DeclineJobResponse(booking_id=booking.id, outcome=outcome)
