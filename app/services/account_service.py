"""
In-app account deletion (SVC-367): Play's User Data policy and App Store 5.1.1(v).

The ``users`` row is tombstoned, never deleted. About twenty FKs reference it
(bookings, reviews, offers, ledger, settlements, payout audit, coupons, admin
actions, professionals) and none cascades, so a DELETE would fail, and the
financial rows must stay for the retention period anyway. What goes is the
personal data: the user's name, email, phone, push token, saved addresses, an
SP's business identity, payout and KYC data, and the S3 objects behind them.

The phone becomes a placeholder that can never match a real number, so the
same number signing up again gets a fresh account (option (a) in SVC-367). No
hash of the old number is kept; that is the point.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import status
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException
from app.models.address import Address
from app.models.booking import ACTIVE_JOB_STATUSES, Booking, BookingStatus
from app.models.professional import Professional
from app.models.provider_ledger_entry import ProviderLedgerEntry
from app.models.provider_profile import ProviderProfile
from app.models.provider_team_invite import InviteStatus, ProviderTeamInvite
from app.models.user import User
from app.services.team_service import TeamService
from app.utils import storage

logger = logging.getLogger(__name__)

# Stable codes the frontend branches on.
ACTIVE_BOOKINGS = "ACTIVE_BOOKINGS"
TEAM_HAS_PROS = "TEAM_HAS_PROS"
UNPAID_BALANCE = "UNPAID_BALANCE"

# A booking in any of these blocks deletion, whichever side the user is on.
# REQUESTED counts too: the consumer's own unclaimed request is still live.
_LIVE_STATUSES = (BookingStatus.REQUESTED, *ACTIVE_JOB_STATUSES)

DELETED_ADDRESS = "Deleted address"
DELETED_PROVIDER = "Deleted provider"
DELETED_PROFESSIONAL = "Deleted professional"

# The KYC image keys ``store_kyc_submission`` writes, plus the pre-SVC-307 name.
_KYC_KEY_FIELDS = ("aadhaar_front_key", "aadhaar_back_key", "selfie_key", "aadhaar_key")


@dataclass
class S3Cleanup:
    """What to remove from S3 once the deletion has committed.

    ``keys`` are the objects the rows name; deleting them needs only
    ``s3:DeleteObject``. ``prefixes`` are the user's folders, swept for what the
    rows no longer name: a photo replaced by a re-upload, or the images of a
    KYC submission that was rejected and redone. Both folders are written per
    user (``provider-photos/{user_id}/``, ``kyc/{user_id}/``,
    ``kyc/pros/{pro_id}/``), so everything in them is this user's.
    """

    keys: list[str] = field(default_factory=list)
    prefixes: list[str] = field(default_factory=list)


def tombstone_phone(user_id: uuid.UUID) -> str:
    """15 chars (``users.phone`` is String(15)), unique because it is derived
    from the id, and never a valid ``+91…`` number, so no OTP can reach it."""
    return f"D{user_id.hex[:14]}"


class AccountService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def delete_account(self, user_id: uuid.UUID) -> S3Cleanup:
        """Tombstone the caller's account. Returns what to delete from S3 once
        the transaction commits; the caller commits.

        Refused, with nothing written, while the user:
        - has a live booking as consumer, SP or assigned Pro (409 ``ACTIVE_BOOKINGS``);
        - is an SP with an active team Pro besides their own (409 ``TEAM_HAS_PROS``);
        - is an SP the platform still owes money (409 ``UNPAID_BALANCE``), since
          the payout details are about to be erased. A negative balance (they
          owe us) does not block.

        The user row is locked FOR UPDATE first, the lock ``accept_invite`` and
        admin deactivation take, so neither can change the team underneath.
        """
        user = await self.db.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        pros = list(
            (await self.db.execute(select(Professional).where(Professional.user_id == user_id)))
            .scalars()
            .all()
        )
        profile = await self.db.scalar(
            select(ProviderProfile).where(ProviderProfile.user_id == user_id)
        )

        await self._refuse_if_blocked(user_id, pros, profile)

        now = datetime.now(UTC)
        old_phone = user.phone
        keys: list[str] = []
        prefixes: list[str] = []

        # A team Pro leaves the way an SP removing them would, so the other
        # SP's SVC-326 invariant (an SP off tools keeps another Pro) holds.
        team = TeamService(self.db)
        for pro in pros:
            if pro.is_active and not pro.is_self:
                await team.remove_pro(pro.service_provider_id, pro.id)
        for pro in pros:
            keys += _kyc_keys(pro.kyc_docs) + _photo_keys(pro.profile_photo_url)
            prefixes.append(f"kyc/pros/{pro.id}/")
            pro.is_active = False
            pro.is_online = False
            pro.display_name = DELETED_PROFESSIONAL
            pro.profile_photo_url = None
            pro.kyc_docs = None
            pro.kyc_ai_verdict = None
            pro.last_location = None
            pro.last_location_at = None

        if profile is not None:
            keys += _kyc_keys(profile.kyc_docs)
            keys += _photo_keys(profile.profile_photo_url, profile.business_image_url)
            prefixes += [f"provider-photos/{user_id}/", f"kyc/{user_id}/"]
            _scrub_profile(profile)
            await self.db.execute(
                update(ProviderTeamInvite)
                .where(
                    ProviderTeamInvite.invited_by == user_id,
                    ProviderTeamInvite.status == InviteStatus.PENDING.value,
                )
                .values(status=InviteStatus.REVOKED.value, revoked_at=now)
            )

        # Invites addressed to this person: the phone is theirs, so it goes,
        # and a pending one is revoked in the same statement. The partial
        # unique indexes only cover PENDING rows, so several rows can share
        # the placeholder.
        pending = ProviderTeamInvite.status == InviteStatus.PENDING.value
        await self.db.execute(
            update(ProviderTeamInvite)
            .where(ProviderTeamInvite.invitee_phone == old_phone)
            .values(
                invitee_phone=tombstone_phone(user_id),
                status=case((pending, InviteStatus.REVOKED.value), else_=ProviderTeamInvite.status),
                revoked_at=case((pending, now), else_=ProviderTeamInvite.revoked_at),
            )
        )

        # Every address, including ones already soft-deleted: past bookings
        # keep pointing at the rows, but nothing on them identifies anyone.
        await self.db.execute(
            update(Address)
            .where(Address.user_id == user_id)
            .values(
                full_address=DELETED_ADDRESS,
                house_floor=None,
                building=None,
                landmark_area=None,
                pincode=None,
                contact_name=None,
                contact_phone=None,
                location=None,
                is_default=False,
                deleted_at=func.coalesce(Address.deleted_at, now),
            )
        )

        user.phone = tombstone_phone(user_id)
        user.name = None
        user.email = None
        user.fcm_token = None
        user.fcm_platform = None
        user.is_active = False
        user.deleted_at = now
        await self.db.flush()

        logger.warning("account.deleted user_id=%s role=%s", user_id, user.role.value)
        # Pro-for-self and SP rows can name the same object; delete it once.
        return S3Cleanup(keys=list(dict.fromkeys(keys)), prefixes=prefixes)

    async def _refuse_if_blocked(
        self,
        user_id: uuid.UUID,
        pros: list[Professional],
        profile: ProviderProfile | None,
    ) -> None:
        held = or_(Booking.consumer_id == user_id, Booking.provider_id == user_id)
        if pros:
            held = held | Booking.professional_id.in_([p.id for p in pros])
        booking_ids = list(
            (
                await self.db.execute(
                    select(Booking.id)
                    .where(held, Booking.status.in_(_LIVE_STATUSES))
                    .order_by(Booking.created_at, Booking.id)
                )
            )
            .scalars()
            .all()
        )
        if booking_ids:
            raise AppException(
                "Finish or cancel your active bookings before deleting your account.",
                status_code=status.HTTP_409_CONFLICT,
                code=ACTIVE_BOOKINGS,
                details={"booking_ids": [str(i) for i in booking_ids]},
            )

        if profile is None:
            return

        team_ids = list(
            (
                await self.db.execute(
                    select(Professional.id)
                    .where(
                        Professional.service_provider_id == user_id,
                        Professional.user_id != user_id,
                        Professional.is_active.is_(True),
                    )
                    .order_by(Professional.created_at, Professional.id)
                )
            )
            .scalars()
            .all()
        )
        if team_ids:
            raise AppException(
                "Remove your team members before deleting your account.",
                status_code=status.HTTP_409_CONFLICT,
                code=TEAM_HAS_PROS,
                details={"professional_ids": [str(i) for i in team_ids]},
            )

        # Every entry, settled or not: an unsettled completion credit is still
        # money owed, and wallet_balance_paise's T+1 gate would hide it.
        balance = int(
            (
                await self.db.execute(
                    select(func.coalesce(func.sum(ProviderLedgerEntry.amount_paise), 0)).where(
                        ProviderLedgerEntry.provider_id == user_id
                    )
                )
            ).scalar_one()
        )
        if balance > 0:
            raise AppException(
                "You have earnings still to be paid out. "
                "You can delete your account after your next payout.",
                status_code=status.HTTP_409_CONFLICT,
                code=UNPAID_BALANCE,
                details={"balance_paise": balance},
            )


def _scrub_profile(profile: ProviderProfile) -> None:
    """Everything that identifies the SP or pays them. Ratings, job counts and
    the KYC status stay: they describe the history, not the person."""
    profile.business_name = DELETED_PROVIDER
    profile.business_image_url = None
    profile.profile_photo_url = None
    profile.aadhaar_encrypted = None
    profile.pan_encrypted = None
    profile.upi_id_encrypted = None
    profile.bank_account_encrypted = None
    profile.payout_vpa_encrypted = None
    profile.payout_account_number_encrypted = None
    profile.payout_ifsc_encrypted = None
    profile.payout_method = None
    profile.payout_display = None
    profile.razorpayx_contact_id = None
    profile.razorpayx_fund_account_id = None
    profile.kyc_docs = None
    profile.kyc_ai_verdict = None
    profile.kyc_rejection_reason = None
    profile.service_area_center = None
    profile.is_online = False


def _kyc_keys(docs: dict | None) -> list[str]:
    return [docs[f] for f in _KYC_KEY_FIELDS if docs and docs.get(f)]


def _photo_keys(*urls: str | None) -> list[str]:
    keys = []
    for url in urls:
        if not url:
            continue
        key = storage.key_from_url(url)
        if key is None:
            logger.warning("account.delete_unknown_photo_url url=%s", url)
        else:
            keys.append(key)
    return keys


def delete_objects(user_id: uuid.UUID, cleanup: S3Cleanup) -> None:
    """Best-effort, after commit: the account is already gone, so a failed
    S3 call is logged for a manual sweep, never surfaced to the user.

    The named keys go first, so they are removed even if the role lacks
    ``s3:ListBucket`` and the folder sweep fails.
    """
    for key in cleanup.keys:
        try:
            storage.delete_file(key)
        except Exception:
            logger.warning(
                "account.s3_delete_failed user_id=%s key=%s", user_id, key, exc_info=True
            )
    for prefix in cleanup.prefixes:
        try:
            swept = storage.delete_prefix(prefix)
        except Exception:
            logger.warning(
                "account.s3_sweep_failed user_id=%s prefix=%s", user_id, prefix, exc_info=True
            )
        else:
            logger.info("account.s3_swept user_id=%s prefix=%s objects=%d", user_id, prefix, swept)
