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


# ---------------------------------------------------------------------------
# Admin payouts dashboard (SVC-426)
#
# Every amount is paise, with the unit in the field name, like the rest of this
# screen: the trigger's 202 and pending-settlements are paise too.
# ---------------------------------------------------------------------------


class PayoutDashboardProvider(BaseModel):
    """One SP with any payout-scope booking (completed, PAID, not cash)."""

    provider_id: UUID
    name: str | None
    business_name: str | None
    phone: str
    is_active: bool
    payout_set: bool
    kyc_status: str
    # One count + amount per settlement status. ``pending_*`` is ``_owed_filter``,
    # so it equals pending-settlements' figures for the same SP.
    pending_count: int
    pending_paise: int
    processing_count: int
    processing_paise: int
    completed_count: int
    completed_paise: int
    failed_count: int
    failed_paise: int
    reversed_count: int
    reversed_paise: int
    # All five buckets: what the SP has earned through payout-scope bookings.
    earned_count: int
    earned_paise: int
    # What a payout would send right now: the wallet balance, which nets cash
    # commission and only counts T+1-settled credits (SVC-180). Equals the
    # trigger's 202 ``amount_paise``, and can be below ``pending_paise``.
    payable_paise: int
    # The newest failed/reversed booking's RazorpayX reason, else the error of the
    # SP's latest payout attempt when that attempt failed. Null otherwise.
    latest_failure_reason: str | None


class PayoutDashboardResponse(BaseModel):
    providers: list[PayoutDashboardProvider]
    total: int
    page: int
    per_page: int


class PayoutBookingRow(BaseModel):
    """One booking behind an SP's payout figures: the split that went into it."""

    booking_id: UUID
    # First COMPLETED status-log time. Null only for a booking with no such row.
    completed_at: datetime | None
    # The price fixed at booking time (the ticket's "locked_price").
    service_amount_paise: int
    discount_amount_paise: int
    platform_commission_paise: int
    provider_payout_paise: int
    tip_amount_paise: int
    settlement_status: str
    settlement_payout_id: str | None
    settlement_failure_reason: str | None
    # The Pro who did the job; null when no Pro was recorded.
    professional_name: str | None


class PayoutBookingsResponse(BaseModel):
    bookings: list[PayoutBookingRow]
    total: int
    page: int
    per_page: int
