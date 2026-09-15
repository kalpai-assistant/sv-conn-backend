from datetime import datetime
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


class ProviderPayoutHistoryEntry(BaseModel):
    """One row from the payout audit trail for a single provider (SVC-300).

    Mirrors the ``payout_audits`` row directly rather than inventing new field
    names — the admin history view is the ledger, and drift between the two
    invites "the row I see is not the row that moved" bugs.
    """

    audit_id: UUID
    # RazorpayX ``pout_XXXXX`` id. Null when the attempt failed before RazorpayX
    # returned one (see ``PayoutService._resolve_audit`` outcome=="unknown").
    payout_id: str | None
    # Batch key (also sent to RazorpayX as reference_id). Lets reconciliation
    # map an orphaned payout back — the admin sees it so a manual RazorpayX
    # lookup during an incident carries the same string.
    reference_id: str | None
    amount_paise: int
    booking_count: int
    # Free-form ``payout_audits.outcome`` — attempting / unknown / initiated /
    # processing / processed / completed / failed / rejected / reversed /
    # requeued. Passed through verbatim so the UI can add cases without a
    # backend enum change.
    outcome: str
    # "admin" | "reconciliation" — a reconciliation row is a repair by the
    # nightly job, not an admin action, and reads differently in the audit
    # history than a triggered payout.
    source: str
    # Null for reconciliation-authored rows (``PayoutAudit.admin_user_id`` is
    # nullable for exactly this reason).
    admin_user_id: UUID | None
    admin_name: str | None
    # Redacted failure reason; null on success. Already capped at 500 chars in
    # ``_resolve_audit``.
    error: str | None
    created_at: datetime


class ProviderPayoutHistoryResponse(BaseModel):
    """Paginated payout history for one provider, newest-first."""

    entries: list[ProviderPayoutHistoryEntry]
    total: int
    page: int
    per_page: int
