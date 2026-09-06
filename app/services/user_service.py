"""
User service: profile management and address CRUD.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import UserNotFound
from app.models.address import Address
from app.models.user import User
from app.utils.geo import build_point_wkt


class UserService:
    """Manages user profile and address data."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    async def get_profile(self, user_id: uuid.UUID) -> User:
        user = await self.db.get(User, user_id)
        if not user:
            raise UserNotFound(user_id)
        return user

    async def update_profile(self, user_id: uuid.UUID, name: str | None, email: str | None) -> User:
        user = await self.get_profile(user_id)
        if name is not None:
            user.name = name
        if email is not None:
            user.email = email
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def update_fcm_token(self, user_id: uuid.UUID, token: str | None) -> None:
        """Set or clear the user's FCM token.

        `None` (or a blank string) stores NULL rather than "" — downstream send
        paths guard on truthiness, and an empty string would read as a live
        token. Clearing an already-NULL token is a no-op, not an error.

        Note: `users.fcm_token` is a single column, so a clear drops push for
        the user everywhere, not just on the calling device. Per-device tokens
        would need their own table — deliberately out of scope for SVC-164.
        """
        user = await self.get_profile(user_id)
        user.fcm_token = (token or "").strip() or None
        await self.db.flush()

    # ------------------------------------------------------------------
    # Addresses
    # ------------------------------------------------------------------

    def _live_addresses(self, user_id: uuid.UUID):
        """The user's address book — soft-deleted rows excluded (SVC-277).

        Every read of a user's *own* addresses goes through here, so the
        filtering rule is applied in one place instead of being remembered at
        four call sites.

        Deliberately NOT used for resolving a booking's address: that lookup
        goes by `consumer_address_id` and must still find a deleted row, or
        history stops rendering and — worse — the geo-fence stops matching
        providers, which surfaces as "no partners found nearby" with nothing
        logged anywhere. See `Address.deleted_at`.
        """
        return select(Address).where(
            Address.user_id == user_id,
            Address.deleted_at.is_(None),
        )

    async def list_addresses(self, user_id: uuid.UUID) -> list[Address]:
        result = await self.db.execute(self._live_addresses(user_id))
        return list(result.scalars().all())

    async def add_address(
        self,
        user_id: uuid.UUID,
        label: str,
        full_address: str,
        lat: float | None,
        lng: float | None,
        is_default: bool,
    ) -> Address:
        if is_default:
            await self._clear_default(user_id)

        location = build_point_wkt(lat, lng) if lat is not None and lng is not None else None
        address = Address(
            user_id=user_id,
            label=label,
            full_address=full_address,
            location=location,
            is_default=is_default,
        )
        self.db.add(address)
        await self.db.flush()
        return address

    async def update_address(
        self,
        user_id: uuid.UUID,
        address_id: uuid.UUID,
        label: str | None,
        full_address: str | None,
        lat: float | None,
        lng: float | None,
        is_default: bool | None,
    ) -> Address | None:
        """Edit an address without rewriting the past (SVC-277).

        Copy-on-write rather than an in-place update. `bookings` point at the
        address row, so mutating it changes where a *completed* job is recorded
        as having happened — and, if a job is live, changes the destination for
        a provider who may already be en route. Neither errors; both are just
        wrong afterwards.

        So: soft-delete the old row and create a new one. Old bookings keep
        pointing at the address their job actually went to, and the user sees
        their address book updated, which is all they asked for.

        Returns None when the address does not exist, is not theirs, or is
        already deleted — the router turns that into a 404.
        """
        result = await self.db.execute(
            self._live_addresses(user_id).where(Address.id == address_id)
        )
        current = result.scalar_one_or_none()
        if current is None:
            return None

        # Unset fields keep their current values — this is a PATCH.
        new_label = label if label is not None else current.label
        new_full = full_address if full_address is not None else current.full_address
        new_default = is_default if is_default is not None else current.is_default
        # Coordinates travel as a pair: sending one without the other would
        # produce a point half-derived from the old address.
        if lat is not None and lng is not None:
            new_location = build_point_wkt(lat, lng)
        else:
            new_location = current.location

        current.deleted_at = datetime.now(UTC)
        current.is_default = False
        await self.db.flush()

        if new_default:
            await self._clear_default(user_id)

        replacement = Address(
            user_id=user_id,
            label=new_label,
            full_address=new_full,
            location=new_location,
            is_default=new_default,
        )
        self.db.add(replacement)
        await self.db.flush()
        return replacement

    async def delete_address(self, user_id: uuid.UUID, address_id: uuid.UUID) -> None:
        result = await self.db.execute(
            self._live_addresses(user_id).where(Address.id == address_id)
        )
        address = result.scalar_one_or_none()
        if not address:
            return

        was_default = address.is_default
        # SVC-277: hide rather than remove. A booking may point at this row, and
        # the FK has no ON DELETE rule — `db.delete()` raises for any address
        # that has ever been used.
        #
        # `is_default` is cleared as a statement of intent, not a fix: every
        # query that could act on the flag already goes through
        # `_live_addresses`, so a hidden row is unreachable either way. Leaving
        # a deleted address flagged default would still be a lie in the data,
        # and the next person to add a query has one less trap to fall into.
        address.deleted_at = datetime.now(UTC)
        address.is_default = False
        await self.db.flush()

        # SVC-242: deleting the default leaves the user with no default at all,
        # which breaks the booking flow's `first-is_default` address lookup.
        # Promote the oldest remaining address to default so the user's next
        # booking still auto-fills an address. If they had exactly one address,
        # this is a no-op (nothing to promote).
        if was_default:
            promote = await self.db.execute(
                self._live_addresses(user_id).order_by(Address.created_at.asc()).limit(1)
            )
            successor = promote.scalar_one_or_none()
            if successor is not None:
                successor.is_default = True
                await self.db.flush()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _clear_default(self, user_id: uuid.UUID) -> None:
        result = await self.db.execute(
            self._live_addresses(user_id).where(Address.is_default.is_(True))
        )
        for addr in result.scalars().all():
            addr.is_default = False
