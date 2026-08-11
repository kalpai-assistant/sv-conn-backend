from uuid import UUID

from pydantic import BaseModel


class PendingSettlementProvider(BaseModel):
    """One provider with money owed, as shown in the admin payouts list."""

    provider_id: UUID
    # Nullable on the User model — the UI must handle a provider who never set a name.
    name: str | None
    phone: str
    pending_booking_count: int
    # Paise, not rupees: POST /admin/providers/{id}/payout returns paise too, and the
    # admin UI calls both in one flow — mixing units across that screen invites a
    # 100x display bug. The field name carries the unit so it can't be misread.
    pending_payout_paise: int
    # False when the provider has no usable payout destination on file, so the UI can
    # warn *before* the admin clicks Pay out (the payout itself would raise).
    payout_set: bool
    # A deactivated provider who earned money is still owed it, so they stay in the
    # list — flagged rather than hidden, which would conceal a real debt.
    is_active: bool


class PendingSettlementsResponse(BaseModel):
    """Capped list of providers awaiting settlement.

    Not paginated by design (SVC-144): the admin panel shows one screen of who is
    owed. ``total`` is the true number of providers with pending settlements, so the
    UI can say "showing 200 of 247" when ``truncated`` is set.
    """

    providers: list[PendingSettlementProvider]
    total: int
    truncated: bool
