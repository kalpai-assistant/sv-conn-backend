"""The support console (SVC-433). Roles in ADMIN.md: every route is open to admin,
ops and support.

Reads: one lookup box, and the customer profile. The booking and SP detail
panels reuse ``GET /admin/bookings/{id}`` (+ ``/timeline``) and
``GET /admin/providers/{id}`` (+ ``/team``).

Writes, each audited in the same transaction as the change:

* notes and "contacted" marks (``booking_support_notes``, append-only);
* retry dispatch: the existing ``new_request`` broadcast again, with a fresh
  offer window (``bookings.dispatch_retried_at``);
* reassign a Pro within the booking's SP: ``BookingService.assign``'s re-pick,
  with the admin as actor, so the rules are assign's own.
"""

import math
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import status
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import call_after_commit
from app.exceptions import AppException, BookingNotFound, UserNotFound
from app.models.address import Address
from app.models.admin_action import AdminAction
from app.models.booking import Booking, BookingStatus
from app.models.booking_dispute import BookingDispute
from app.models.booking_support_note import BookingSupportNote
from app.models.professional import Professional
from app.models.provider_profile import ProviderProfile
from app.models.user import User, UserRole
from app.schemas.admin_support import (
    CustomerBooking,
    CustomerBookingPro,
    CustomerBookingProvider,
    CustomerProfile,
    EligiblePro,
    EligibleProsResponse,
    LookupBooking,
    LookupCustomer,
    LookupPro,
    LookupProOwner,
    LookupProvider,
    LookupResponse,
    RetryDispatchResponse,
    SupportNote,
    SupportNoteAuthor,
)
from app.services.admin_audit import AuditActor, audit_admin_action
from app.services.admin_team_service import _escape_like
from app.services.booking_service import BookingService, _summarise_service_names

# Rows per kind in a lookup.
LOOKUP_LIMIT = 20
RECENT_BOOKINGS = 10
# A second retry this soon after the last one is refused (a double click).
RETRY_COOLDOWN_SECONDS = 30

BOOKING_NOT_REQUESTED = "BOOKING_NOT_REQUESTED"
DISPATCH_RETRY_TOO_SOON = "DISPATCH_RETRY_TOO_SOON"
NO_ELIGIBLE_PROVIDERS = "NO_ELIGIBLE_PROVIDERS"
BOOKING_NOT_REASSIGNABLE = "BOOKING_NOT_REASSIGNABLE"

_PHONE_CHARS_RE = re.compile(r"[\d\s\-+().]+")
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.IGNORECASE
)
_SUFFIX_RE = re.compile(r"[0-9a-f]{6}", re.IGNORECASE)
_LETTER_RE = re.compile(r"[a-z]", re.IGNORECASE)


def phone_digits(q: str) -> str | None:
    """The 10-digit number in ``q``, or None when ``q`` isn't a phone.

    Tolerant of how a number is read out: spaces, dashes, brackets, dots and a
    ``+91`` / ``91`` / ``0`` prefix. Anything else in it (a letter) means it
    isn't one.
    """
    if not _PHONE_CHARS_RE.fullmatch(q):
        return None
    digits = re.sub(r"\D", "", q)
    if len(digits) == 10:
        return digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    return None


def interpret(q: str) -> dict[str, object]:
    """Every type ``q`` fits, keyed by kind: the phone's 10 digits, the full
    booking id, the 6-char id suffix, or the name tokens."""
    found: dict[str, object] = {}
    digits = phone_digits(q)
    if digits is not None:
        found["phone"] = digits
    if _UUID_RE.fullmatch(q):
        found["booking_id"] = uuid.UUID(q)
    elif _SUFFIX_RE.fullmatch(q):
        found["booking_id_suffix"] = q.lower()
    if "booking_id" not in found and _LETTER_RE.search(q):
        found["name"] = q.split()
    return found


def _money(value: Decimal | None) -> float:
    return float(value or 0)


def _total(booking: Booking) -> float:
    return float(booking.service_amount - booking.discount_amount + booking.tip_amount)


def _conflict(message: str, code: str, **details) -> AppException:
    return AppException(
        message, status_code=status.HTTP_409_CONFLICT, code=code, details=details or None
    )


class AdminSupportService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    async def lookup(self, q: str) -> LookupResponse:
        q = q.strip()
        kinds = interpret(q)
        customers: list[LookupCustomer] = []
        providers: list[LookupProvider] = []
        pros: list[LookupPro] = []
        bookings: list[LookupBooking] = []
        truncated: set[str] = set()

        def capped(rows: list, kind: str) -> list:
            if len(rows) > LOOKUP_LIMIT:
                truncated.add(kind)
            return rows[:LOOKUP_LIMIT]

        if "phone" in kinds:
            phone = f"+91{kinds['phone']}"
            customers = capped(await self._customers_by_phone(phone), "customers")
            providers = capped(await self._providers_by_phone(phone), "providers")
            pros = capped(await self._pros_by_phone(phone), "pros")
        if "name" in kinds:
            seen = {p.user_id for p in providers}
            by_name = [
                p for p in await self._providers_by_name(kinds["name"]) if p.user_id not in seen
            ]
            providers = capped(providers + by_name, "providers")
        if "booking_id" in kinds:
            booking = await self.db.get(Booking, kinds["booking_id"])
            bookings = await self._lookup_bookings([booking] if booking else [], "id")
        elif "booking_id_suffix" in kinds:
            pattern = f"%{kinds['booking_id_suffix']}"
            rows = await self.db.scalars(
                select(Booking)
                .where(cast(Booking.id, String).like(pattern))
                .order_by(Booking.created_at.desc(), Booking.id)
                .limit(LOOKUP_LIMIT + 1)
            )
            bookings = capped(await self._lookup_bookings(list(rows), "id_suffix"), "bookings")

        return LookupResponse(
            query=q,
            interpreted_as=list(kinds),
            customers=customers,
            providers=providers,
            pros=pros,
            bookings=bookings,
            truncated=sorted(truncated),
        )

    async def _customers_by_phone(self, phone: str) -> list[LookupCustomer]:
        booking_count = (
            select(func.count(Booking.id))
            .where(Booking.consumer_id == User.id)
            .correlate(User)
            .scalar_subquery()
        )
        rows = await self.db.execute(
            select(User, booking_count)
            .where(
                User.phone == phone,
                User.role != UserRole.ADMIN,
                or_(User.role == UserRole.CONSUMER, booking_count > 0),
            )
            .order_by(User.created_at)
            .limit(LOOKUP_LIMIT + 1)
        )
        return [
            LookupCustomer(
                user_id=user.id,
                name=user.name,
                phone=user.phone,
                created_at=user.created_at,
                is_active=bool(user.is_active),
                deleted=user.deleted_at is not None,
                booking_count=count,
            )
            for user, count in rows.all()
        ]

    @staticmethod
    def _provider_row(user: User, profile: ProviderProfile, matched_by: str) -> LookupProvider:
        return LookupProvider(
            user_id=user.id,
            name=user.name,
            business_name=profile.business_name,
            phone=user.phone,
            kyc_status=profile.kyc_status,
            is_online=bool(profile.is_online),
            matched_by=matched_by,
        )

    async def _providers_by_phone(self, phone: str) -> list[LookupProvider]:
        rows = await self.db.execute(
            select(User, ProviderProfile)
            .join(ProviderProfile, ProviderProfile.user_id == User.id)
            .where(User.phone == phone)
            .limit(LOOKUP_LIMIT + 1)
        )
        return [self._provider_row(u, p, "phone") for u, p in rows.all()]

    async def _providers_by_name(self, tokens: list[str]) -> list[LookupProvider]:
        """Token-AND substring: every word must appear in the SP's name or
        business name, case-insensitively, so "ramesh plumb" finds "Ramesh Kumar
        Plumbing". No typo tolerance (that would need pg_trgm). Deleted accounts
        are left out: their name is now "Deleted provider"."""
        clauses = [
            or_(
                User.name.ilike(f"%{_escape_like(token)}%", escape="\\"),
                ProviderProfile.business_name.ilike(f"%{_escape_like(token)}%", escape="\\"),
            )
            for token in tokens
        ]
        rows = await self.db.execute(
            select(User, ProviderProfile)
            .join(ProviderProfile, ProviderProfile.user_id == User.id)
            .where(User.deleted_at.is_(None), *clauses)
            .order_by(func.lower(func.coalesce(ProviderProfile.business_name, User.name)), User.id)
            .limit(LOOKUP_LIMIT + 1)
        )
        return [self._provider_row(u, p, "name") for u, p in rows.all()]

    async def _pros_by_phone(self, phone: str) -> list[LookupPro]:
        rows = (
            await self.db.execute(
                select(Professional, User)
                .join(User, User.id == Professional.user_id)
                .where(
                    User.phone == phone, Professional.user_id != Professional.service_provider_id
                )
                .order_by(Professional.is_active.desc(), Professional.created_at.desc())
                .limit(LOOKUP_LIMIT + 1)
            )
        ).all()
        owner_ids = {pro.service_provider_id for pro, _ in rows}
        owners: dict[uuid.UUID, tuple[User, ProviderProfile | None]] = {}
        if owner_ids:
            owner_rows = await self.db.execute(
                select(User, ProviderProfile)
                .outerjoin(ProviderProfile, ProviderProfile.user_id == User.id)
                .where(User.id.in_(owner_ids))
            )
            owners = {u.id: (u, p) for u, p in owner_rows.all()}
        result = []
        for pro, user in rows:
            owner, profile = owners[pro.service_provider_id]
            result.append(
                LookupPro(
                    professional_id=pro.id,
                    display_name=pro.display_name,
                    phone=user.phone,
                    is_online=bool(pro.is_online),
                    is_active=bool(pro.is_active),
                    service_provider=LookupProOwner(
                        user_id=owner.id,
                        name=owner.name,
                        business_name=profile.business_name if profile else None,
                    ),
                )
            )
        return result

    async def _lookup_bookings(self, bookings: list[Booking], matched_by: str) -> list:
        if not bookings:
            return []
        await BookingService(self.db).attach_items(bookings)
        party_ids = {b.consumer_id for b in bookings} | {
            b.provider_id for b in bookings if b.provider_id
        }
        users = {u.id: u for u in await self.db.scalars(select(User).where(User.id.in_(party_ids)))}
        return [
            LookupBooking(
                id=b.id,
                status=b.status,
                created_at=b.created_at,
                scheduled_at=b.scheduled_at,
                service_summary=_summarise_service_names(b.items),
                consumer_name=users[b.consumer_id].name,
                consumer_phone=users[b.consumer_id].phone,
                provider_name=users[b.provider_id].name if b.provider_id else None,
                matched_by=matched_by,
            )
            for b in bookings
        ]

    # ------------------------------------------------------------------
    # Customer profile
    # ------------------------------------------------------------------

    async def customer(self, user_id: uuid.UUID) -> CustomerProfile:
        user = await self.db.get(User, user_id)
        if user is None or user.role is UserRole.ADMIN:
            raise UserNotFound(user_id)

        address_count = await self.db.scalar(
            select(func.count(Address.id)).where(
                Address.user_id == user_id, Address.deleted_at.is_(None)
            )
        )
        total, completed, gross = (
            await self.db.execute(
                select(
                    func.count(Booking.id),
                    func.count(Booking.id).filter(Booking.status == BookingStatus.COMPLETED),
                    func.coalesce(
                        func.sum(
                            Booking.service_amount - Booking.discount_amount + Booking.tip_amount
                        ).filter(Booking.status == BookingStatus.COMPLETED),
                        0,
                    ),
                ).where(Booking.consumer_id == user_id)
            )
        ).one()
        refunded = await self.db.scalar(
            select(func.coalesce(func.sum(BookingDispute.refund_amount), 0))
            .join(Booking, Booking.id == BookingDispute.booking_id)
            .where(
                Booking.consumer_id == user_id,
                Booking.status == BookingStatus.COMPLETED,
                BookingDispute.refund_amount.is_not(None),
                or_(
                    BookingDispute.refund_status == "PROCESSED",
                    BookingDispute.refund_channel == "MANUAL",
                ),
            )
        )

        recent = list(
            await self.db.scalars(
                select(Booking)
                .where(Booking.consumer_id == user_id)
                .order_by(Booking.created_at.desc(), Booking.id)
                .limit(RECENT_BOOKINGS)
            )
        )
        return CustomerProfile(
            user_id=user.id,
            name=user.name,
            phone=user.phone,
            email=user.email,
            created_at=user.created_at,
            deleted_at=user.deleted_at,
            is_active=bool(user.is_active),
            address_count=address_count,
            lifetime_bookings=total,
            completed_bookings=completed,
            lifetime_spend=_money(gross) - _money(refunded),
            recent_bookings=await self._customer_bookings(recent),
        )

    async def _customer_bookings(self, bookings: list[Booking]) -> list[CustomerBooking]:
        if not bookings:
            return []
        await BookingService(self.db).attach_items(bookings)
        sp_ids = {b.provider_id for b in bookings if b.provider_id}
        sps: dict[uuid.UUID, tuple[User, ProviderProfile | None]] = {}
        if sp_ids:
            rows = await self.db.execute(
                select(User, ProviderProfile)
                .outerjoin(ProviderProfile, ProviderProfile.user_id == User.id)
                .where(User.id.in_(sp_ids))
            )
            sps = {u.id: (u, p) for u, p in rows.all()}
        pro_ids = {b.professional_id for b in bookings if b.professional_id}
        pros = (
            {
                p.id: p
                for p in await self.db.scalars(
                    select(Professional).where(Professional.id.in_(pro_ids))
                )
            }
            if pro_ids
            else {}
        )
        result = []
        for b in bookings:
            provider = None
            if b.provider_id in sps:
                sp_user, profile = sps[b.provider_id]
                provider = CustomerBookingProvider(
                    user_id=sp_user.id,
                    name=sp_user.name,
                    business_name=profile.business_name if profile else None,
                )
            pro = pros.get(b.professional_id)
            result.append(
                CustomerBooking(
                    id=b.id,
                    status=b.status,
                    created_at=b.created_at,
                    scheduled_at=b.scheduled_at,
                    service_summary=_summarise_service_names(b.items),
                    total=_total(b),
                    payment_method=b.payment_method,
                    provider=provider,
                    professional=(
                        CustomerBookingPro(id=pro.id, display_name=pro.display_name)
                        if pro
                        else None
                    ),
                )
            )
        return result

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------

    async def list_notes(self, booking_id: uuid.UUID) -> list[SupportNote]:
        if await self.db.get(Booking, booking_id) is None:
            raise BookingNotFound(booking_id)
        rows = await self.db.execute(
            select(BookingSupportNote, User)
            .join(User, User.id == BookingSupportNote.author_user_id)
            .where(BookingSupportNote.booking_id == booking_id)
            .order_by(BookingSupportNote.created_at.desc(), BookingSupportNote.id.desc())
        )
        return [self._note(note, author) for note, author in rows.all()]

    async def add_note(
        self, actor: AuditActor, booking_id: uuid.UUID, kind: str, body: str | None
    ) -> SupportNote:
        """Append a note or a "contacted" mark. The audit row carries the note's
        id and kind, never its text."""
        if await self.db.get(Booking, booking_id) is None:
            raise BookingNotFound(booking_id)
        body = body.strip() if body else None
        if kind == "note" and not body:
            raise AppException(
                "A note needs some text.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="NOTE_EMPTY",
            )
        note = BookingSupportNote(
            booking_id=booking_id, author_user_id=actor.admin_id, kind=kind, body=body or None
        )
        self.db.add(note)
        await self.db.flush()
        await self.db.refresh(note)
        await audit_admin_action(
            self.db,
            actor,
            action="booking.note_add" if kind == "note" else "booking.contacted",
            target_type="booking",
            target_id=booking_id,
            details={"note_id": str(note.id), "kind": kind},
        )
        author = await self.db.get(User, actor.admin_id)
        return self._note(note, author)

    @staticmethod
    def _note(note: BookingSupportNote, author: User) -> SupportNote:
        return SupportNote(
            id=note.id,
            booking_id=note.booking_id,
            kind=note.kind,
            body=note.body,
            created_at=note.created_at,
            author=SupportNoteAuthor(
                user_id=author.id,
                name=author.name,
                email=author.email,
                admin_role=author.admin_role,
            ),
        )

    # ------------------------------------------------------------------
    # Retry dispatch
    # ------------------------------------------------------------------

    async def retry_dispatch(
        self, actor: AuditActor, booking_id: uuid.UUID
    ) -> RetryDispatchResponse:
        """Broadcast a REQUESTED booking again, with a fresh offer window.

        The broadcast is the existing ``new_request`` task, enqueued after
        commit; this only decides whether to run it. Who it reaches is
        ``broadcast_recipients_stmt``, counted here first: with nobody to reach,
        the task would expire the booking, so instead this answers 409
        ``NO_ELIGIBLE_PROVIDERS`` with the funnel's reason and changes nothing.

        The window restarts from ``dispatch_retried_at``. A paid booking's
        second offer (``last_chance_at``) is cleared, as a reopen does, so it
        gets the full window and then its second offer again.

        The booking row lock serialises two clicks; the second, within
        ``RETRY_COOLDOWN_SECONDS`` of the last retry, gets 409
        ``DISPATCH_RETRY_TOO_SOON``.
        """
        from app.services.provider_matching import (
            broadcast_funnel,
            broadcast_recipients_stmt,
            funnel_reason,
        )

        booking = await self.db.get(
            Booking, booking_id, with_for_update=True, populate_existing=True
        )
        if booking is None:
            raise BookingNotFound(booking_id)
        if booking.status is not BookingStatus.REQUESTED:
            raise _conflict(
                f"Only a booking waiting for a provider can be dispatched again; "
                f"this one is {booking.status.value}.",
                BOOKING_NOT_REQUESTED,
                status=booking.status.value,
            )

        since_last = await self.db.scalar(
            select(func.extract("epoch", func.now() - func.max(AdminAction.created_at))).where(
                AdminAction.action == "booking.retry_dispatch",
                AdminAction.target_type == "booking",
                AdminAction.target_id == str(booking_id),
            )
        )
        if since_last is not None and since_last < RETRY_COOLDOWN_SECONDS:
            raise _conflict(
                "This booking was dispatched again moments ago.",
                DISPATCH_RETRY_TOO_SOON,
                retry_after_seconds=max(1, math.ceil(RETRY_COOLDOWN_SECONDS - float(since_last))),
            )

        address = await self.db.get(Address, booking.consumer_address_id)
        await BookingService(self.db).attach_items([booking])
        service_ids = [item.service_id for item in booking.items]
        reason = None
        recipients = 0
        if address is None or address.location is None:
            reason = "no-address-location"
        elif not service_ids:
            reason = "empty-cart"
        else:
            location = address.location
            recipients = len(
                (
                    await self.db.execute(
                        broadcast_recipients_stmt(service_ids, location, booking.id)
                    )
                )
                .scalars()
                .all()
            )
            if recipients == 0:
                counts = await self.db.run_sync(
                    lambda session: broadcast_funnel(session, service_ids, location, booking.id)
                )
                reason = funnel_reason(counts)
        if reason is not None:
            raise _conflict(
                "No provider can be offered this booking right now.",
                NO_ELIGIBLE_PROVIDERS,
                reason=reason,
            )

        now = datetime.now(UTC)
        booking.dispatch_retried_at = now
        booking.last_chance_at = None
        await audit_admin_action(
            self.db,
            actor,
            action="booking.retry_dispatch",
            target_type="booking",
            target_id=booking.id,
            details={"recipients": recipients},
            sp_user_id=None,
        )
        booking_key = str(booking.id)

        def _broadcast() -> None:
            from app.tasks.notification_tasks import send_booking_notification

            send_booking_notification.delay(booking_id=booking_key, event="new_request")

        call_after_commit(self.db, _broadcast)
        return RetryDispatchResponse(
            booking_id=booking.id,
            status=booking.status,
            recipients=recipients,
            retried_at=now,
            offer_expires_at=booking.offer_expires_at,
        )

    # ------------------------------------------------------------------
    # Reassign a Pro within the SP
    # ------------------------------------------------------------------

    @staticmethod
    def _require_reassignable(booking: Booking) -> None:
        if booking.status is not BookingStatus.ACCEPTED or booking.provider_id is None:
            raise _conflict(
                "Only an accepted booking, before anyone is on the way, can be given "
                f"to another professional; this one is {booking.status.value}.",
                BOOKING_NOT_REASSIGNABLE,
                status=booking.status.value,
            )

    async def eligible_pros(self, booking_id: uuid.UUID) -> EligibleProsResponse:
        """The booking's SP's active Pros, each with what a reassign to them
        would answer: ``BookingService.pro_refusal``, the rule assign raises."""
        booking = await self.db.get(Booking, booking_id)
        if booking is None:
            raise BookingNotFound(booking_id)
        self._require_reassignable(booking)
        svc = BookingService(self.db)
        pros = await svc._active_pros(booking.provider_id)
        ids = [pro.id for pro in pros]
        declined = await svc.declined_pro_ids(booking.id, ids)
        busy = await svc._busy_pro_ids(ids, except_booking=booking.id)
        phones = dict(
            (
                await self.db.execute(
                    select(User.id, User.phone).where(User.id.in_({p.user_id for p in pros}))
                )
            ).all()
        )
        rows = []
        for pro in pros:
            current = pro.id == booking.professional_id
            refusal = (
                None
                if current
                else svc.pro_refusal(pro, declined=pro.id in declined, busy=pro.id in busy)
            )
            rows.append(
                EligiblePro(
                    professional_id=pro.id,
                    display_name=pro.display_name,
                    phone=phones.get(pro.user_id, ""),
                    is_self=pro.is_self,
                    is_online=bool(pro.is_online),
                    kyc_status=pro.kyc_status,
                    is_current=current,
                    eligible=not current and refusal is None,
                    refusal_code=refusal.code if refusal else None,
                )
            )
        return EligibleProsResponse(
            booking_id=booking.id,
            service_provider_id=booking.provider_id,
            current_professional_id=booking.professional_id,
            pros=rows,
        )

    async def reassign_pro(
        self, actor: AuditActor, booking_id: uuid.UUID, professional_id: uuid.UUID
    ) -> Booking:
        """Give the booking to another Pro of the same SP.

        Runs ``BookingService.assign`` as the booking's SP with the admin as
        actor. The SP holds it at ACCEPTED (checked under the row lock first),
        so that is always assign's re-pick: same refusals, a ``repicked``
        assignment event, and the old team Pro's ``unassigned`` push from
        ``record_assignment``. The new team Pro gets ``assigned``, as on the SP
        path. Neither the SP nor the customer is pushed. The same Pro again
        changes nothing and writes no audit row.
        """
        booking = await self.db.get(
            Booking, booking_id, with_for_update=True, populate_existing=True
        )
        if booking is None:
            raise BookingNotFound(booking_id)
        self._require_reassignable(booking)
        sp_id = booking.provider_id
        from_pro_id = booking.professional_id

        booking, _, pro = await BookingService(self.db).assign(
            booking_id=booking_id,
            sp_id=sp_id,
            professional_id=professional_id,
            actor_id=actor.admin_id,
        )
        if pro is None:
            return booking

        await audit_admin_action(
            self.db,
            actor,
            action="booking.reassign_pro",
            target_type="booking",
            target_id=booking.id,
            sp_user_id=sp_id,
            before={"professional_id": from_pro_id},
            after={"professional_id": pro.id},
            details={
                "from_professional_id": str(from_pro_id) if from_pro_id else None,
                "to_professional_id": str(pro.id),
            },
        )
        if not pro.is_self:
            booking_key = str(booking.id)

            def _assigned() -> None:
                from app.tasks.notification_tasks import send_booking_notification

                send_booking_notification.delay(booking_id=booking_key, event="assigned")

            call_after_commit(self.db, _assigned)
        return booking
