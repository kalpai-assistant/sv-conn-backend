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
    # KYCStatus value (pending / submitted / verified / rejected). Surfaces the
    # SVC-189 (G16) gate on the admin payouts screen: the same admin who
    # authorises real money needs to see whether the recipient passed KYC
    # without opening the provider profile in another tab. The admin UI both
    # renders this as a badge and disables the Pay out button unless
    # `kyc_status == "verified"` — mirroring the existing `payout_set` pattern.
    kyc_status: str


class PendingSettlementsResponse(BaseModel):
    """Capped list of providers awaiting settlement.

    Not paginated by design (SVC-144): the admin panel shows one screen of who is
    owed. ``total`` is the true number of providers with pending settlements, so the
    UI can say "showing 200 of 247" when ``truncated`` is set.
    """

    providers: list[PendingSettlementProvider]
    total: int
    truncated: bool
