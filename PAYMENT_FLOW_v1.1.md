# PAYMENT_FLOW_v1.1 — Payment-at-Booking Proposal v2

**Jira:** [SVC-135](https://kalpai.atlassian.net/browse/SVC-135) · PAY-6 · DESIGN · Post-Beta · 3 SP · Assignee: Tushar
**Supersedes:** `PAYMENT_FLOW_PROPOSED.md` (v1 draft)
**Status:** Draft for Lead review. **No implementation tickets should be cut until this doc is approved.**

## Purpose

v1 (`PAYMENT_FLOW_PROPOSED.md`) sketched a pay-at-booking flow that assumed Razorpay
**Route** `transfers` for provider settlement. The architecture review rejected that
(PA/escrow compliance, linked-account overhead) and beta was already built the other
way. This v2 reconciles the doc with **what is actually shipped**, then designs the
gaps that beta deliberately left open.

Every section is tagged:

- 🟢 **AS-BUILT** — already in `main`/beta. Documented here; **no code change**.
- 🟡 **PARTIAL** — a stub or wrong mechanism exists; v1.1 completes/replaces it.
- 🔴 **NEW** — no code today; this doc is the design that spawns implementation tickets.

---

## 0. Money-movement model (context)

Beta deliberately does **not** use Razorpay Route. Consumer money is collected in
full to the **platform account**; the provider's share is paid out separately via
**RazorpayX**. That gives us two independent legs:

```
                 (collect leg — Razorpay Payments)          (settle leg — RazorpayX Payouts)
  Consumer  ──────────────────────────────────▶  Platform  ──────────────────────────────▶  Provider
   card/UPI         full service_amount          settlement          provider_payout
                                                  account            (service_amount − platform_commission)
```

The three-axis booking status is the backbone the whole flow hangs off (SVC-130/131):

| Axis | Enum | Meaning |
|---|---|---|
| `status` | `BookingStatus` | Fulfilment lifecycle (requested → … → completed) |
| `payment_status` | `PaymentStatus` | Collect leg (pending → paid → refunded …) |
| `settlement_status` | `SettlementStatus` | Settle leg (pending → processing → completed/failed) |

> **Doc-correctness note:** `app/models/booking.py` comments still call
> `SettlementStatus` / `settlement_status` *"not read at runtime yet."* That is
> **stale** — `PayoutService` reads and writes both. Fix the comments when the next
> payout change lands.

---

## 1. 🟢 Pay-at-booking trigger + Razorpay order creation (AS-BUILT)

**Code:** `PaymentService.create_order` / `verify_payment` (`app/services/payment_service.py`),
`POST /payments/orders`, `POST /payments/verify` (`app/routers/payments.py`).

- Order is created for `service_amount * 100` paise into the platform account.
  **No `transfers` array** — provider needs no linked account. `razorpay_order_id`
  is stored on the booking.
- Frontend runs Razorpay Checkout, returns `order_id` + `payment_id` + `signature`.
- `verify_payment` recomputes `HMAC_SHA256(order_id|payment_id, key_secret)` and
  `compare_digest`s it. On success it writes a **`CAPTURED` `Payment` ledger row**
  (`amount`, `platform_fee`, `provider_settlement` copied from the booking) and flips
  `payment_status → PAID`.
- Redundant `payment.captured` webhook is idempotent (only flips if not already PAID).
- `payment.failed` writes a `FAILED` ledger row for audit.

### Open design point — the *trigger*
Today the order is created by an **explicit client call**, decoupled from booking
creation. "Pay-**at**-booking" implies the order should be minted **at booking
creation** (or first, before a provider is even matched) so payment gates fulfilment.

**Decision needed (Lead):** does payment happen *before* `requested` (pay-to-book) or
*after* `completed` (pay-on-completion)? Beta code supports both timings; the doc must
pick one as the v1.1 default. **Recommendation:** pay-to-book for online, so an unpaid
request never consumes provider capacity — see §9 (abandonment) which only makes sense
under pay-to-book.

---

## 2. 🟢 Provider settlement via RazorpayX Payouts (AS-BUILT)

**Code:** `PayoutService` (`app/services/payout_service.py`),
`create_payout` (`app/utils/razorpayx_client.py`), `POST /payments/payout-webhook`,
`PayoutAudit` (`app/models/payout_audit.py`).

- **Explicitly NOT Razorpay Route.** RazorpayX is a separate product; payouts are
  direct REST calls (`/v1/payouts` composite API), funded from `RAZORPAYX_ACCOUNT_NUMBER`.
- Admin-triggered, weekly cadence (beta). Sums every `COMPLETED` booking whose
  `settlement_status = PENDING` for a provider, sends **one** payout, flips them to
  `PROCESSING`. `payout.processed` → `COMPLETED`, `payout.failed` → `FAILED`.
- **Idempotency:** batch key = `sha256(sorted booking ids)`; `X-Payout-Idempotency`
  header means a retry never double-pays. Rows are `SELECT … FOR UPDATE SKIP LOCKED`
  so two concurrent admin clicks can't issue divergent batches.
- **Audit:** every attempt (success *and* failure) writes a `PayoutAudit` row; the
  failure audit commits in its own session so it survives the request rollback.
- Payouts destination is the provider's encrypted VPA or bank account (SVC-147),
  UPI preferred.

### 🔴 v1.1 improvement — cache contact / fund-account per provider (NEW)
The composite payout call creates a **fresh RazorpayX contact + fund account on every
payout** (`razorpayx_client.py` NOTE), accumulating duplicate contacts. **Design:**
create-and-cache a `razorpayx_contact_id` + `razorpayx_fund_account_id` once per
provider (on payout-details set), reuse thereafter. Store on `ProviderProfile`.
Invalidate/rotate when payout details change.

---

## 3. 🟢 Cash flow (AS-BUILT)

**Code:** `process_cash_payment` Celery task (`app/tasks/payment_tasks.py`).

- Cash bookings **never enter the online flow**. On completion with
  `payment_method = cash`, the task writes a `CAPTURED` `Payment` row (`method=CASH`)
  and flips `payment_status → PAID`. Idempotent (returns early if already PAID),
  retries ×3.
- Provider already holds the cash, so the platform's commission on cash jobs is a
  **receivable** (running balance owed *to* the platform), not a payout. v1.1 must
  state how that receivable is reconciled/settled against future online payouts —
  see §8. **This is the one substantive cash open-question.**

---

## 4. 🔴 RBI PA / escrow positions (NEW — design)

No "wallet" or escrow position exists in code today; money conceptually jumps from
`booking.provider_payout` straight to a payout. Under RBI's Payment Aggregator rules
the money sits in defined positions and must be modeled explicitly:

```
   ┌─────────────┐   settle T+1   ┌──────────────────────┐  earmark  ┌────────────────┐  payout  ┌──────────┐
   │ PA escrow   │ ─────────────▶ │ Platform settlement  │ ────────▶ │ Provider wallet │ ───────▶ │ Provider │
   │ (Razorpay)  │                │ account (our bank)   │           │ (ledger balance)│          │  bank    │
   └─────────────┘                └──────────────────────┘           └────────────────┘          └──────────┘
     held by PA,                   real cash we hold;                 liability we owe the         RazorpayX
     not our money                 platform commission recognised     provider; not yet paid       payout leg
```

**Design deliverables:**
- A **provider wallet / ledger** (new table, e.g. `provider_ledger_entries`): append-only
  double-entry rows (credit on booking completion, debit on payout, debit on
  dispute-loss, credit/debit on cash-commission reconciliation). Wallet balance =
  sum of entries; it is the source of truth for "what we owe each provider."
- Map each position to a status we already track: PA-escrow ↔ `payment_status=PAID`
  but funds not yet in our account; settlement-account ↔ funds landed T+1;
  provider-wallet ↔ `settlement_status=PENDING` (owed, not sent); paid-out ↔
  `settlement_status=COMPLETED`.
- **T+1 timing:** payout eligibility must respect Razorpay's T+1 settlement to our
  account — don't pay a provider out of money we haven't received yet. Gate payouts on
  settlement-received, not just on booking completion.

**Open (Lead):** do we model an explicit wallet table now, or keep deriving "owed" from
`bookings.provider_payout` where `settlement_status=PENDING` (cheaper, but no room for
tips/adjustments/dispute clawbacks)? Tips (§6) and dispute-loss (§5) both need a wallet
to land on — recommend building the ledger.

---

## 5. 🟡 Disputes / chargebacks state machine (PARTIAL → design target)

**Code today (SVC-134):** `PaymentService._handle_dispute`, `Payment.disputed_at`.
Beta is **flag-only**: `payment.dispute.created` stamps `disputed_at` (idempotent) and
logs; `.won`/`.lost` only log. **No** automated refund or settlement reversal — an
admin resolves out of band. There is intentionally **no state machine yet.**

**v1.1 target state machine** (new `dispute_status` on `Payment`, or a `disputes` table):

```
        payment.dispute.created
   (none) ───────────────────────▶ OPEN ──── payment.dispute.under_review ───▶ UNDER_REVIEW
                                     │                                              │
                       .lost         │                          .won               │  .lost
                 ┌───────────────────┴──────────────────────────┬─────────────────┘
                 ▼                                               ▼
               LOST                                            WON
   (funds pulled by issuer:                       (funds retained:
    reverse provider settlement →                  no ledger change;
    debit provider wallet / clawback;              clear disputed flag)
    payment_status → REFUNDED;
    settlement clawback entry)
```

**Design deliverables:**
- Add `dispute_status` enum + transitions; keep `disputed_at` as the open-timestamp.
- **`.lost` is the money event:** the platform has already (or will) pay the provider;
  a lost dispute claws that back via a **debit ledger entry** on the provider wallet
  (§4) and moves `payment_status → REFUNDED`. If already settled `COMPLETED`, the
  clawback becomes a negative balance carried against future payouts.
- **`.won`:** clear dispute, no money movement.
- Keep the idempotency + "unknown payment → log, don't 5xx" guarantees from SVC-134.
- **Alerting** (Sentry/Slack) is still the `TODO(SVC-134)` net-new infra — reference,
  don't re-specify here.

---

## 6. 🔴 Tips / extras at completion (NEW — design)

No tips/extras fields exist anywhere today.

**Design deliverables:**
- Capture at **completion** (consumer adds a tip; provider may add pre-approved
  "extras" e.g. additional parts/labour). Two new amount fields — recommend a small
  `booking_adjustments` table (`type ∈ {tip, extra}`, `amount`, `added_by`, `approved`)
  rather than columns, so multiple adjustments and audit are possible.
- **Tips are 100% provider** (no platform commission) → credit the provider wallet (§4)
  in full, separate from the commission-split `provider_payout`.
- **Extras** re-run the 80/20 split and increase both `service_amount`-equivalent and
  the payout.
- Collection: if online + pay-to-book, a tip added at completion needs a **second
  capture** (new small order) — cannot retro-add to a captured order. Design the
  second-order flow, or hold tips to the cash/next-payout path. **Open (Lead).**

---

## 7. 🟡 Cancellation fee policy (PARTIAL → design; ticket marks TBD)

**Code today:** the `→ CANCELLED` transition exists (`booking_service.transition_status`)
and stores `cancellation_reason`, but there is **no refund and no fee logic**.
`PaymentStatus.REFUNDED / REFUND_PROCESSING / REFUND_FAILED` are defined but **never
set** — reserved placeholders this section activates.

**Proposed policy (per ticket — confirm exact fee with Lead):**

| Cancelled when | Consumer refund | Provider | Platform |
|---|---|---|---|
| Pre-acceptance (`requested`) | **100%** | — | — |
| Post-acceptance (`accepted`/`en_route`) | **Partial** (service_amount − cancellation fee) | compensation share of fee (TBD) | remainder of fee |
| After `in_progress` | No auto-refund (manual) | — | — |

**Design deliverables:**
- A **refund mechanism** (first use of `REFUND_PROCESSING`/`REFUND_FAILED`): call
  Razorpay Refund API, drive `payment_status` through
  `PAID → REFUND_PROCESSING → REFUNDED` (or `REFUND_FAILED`) via `refund.processed` /
  `refund.failed` webhooks. Fully idempotent, like the payout webhook.
- Cancellation fee split feeds the provider wallet (§4) and platform commission ledger.
- `cancellation_fee` config (percent or flat) in `app/config.py`.

---

## 8. 🔴 Reconciliation job (NEW — design)

No reconciliation exists today. This is the compliance backstop that makes the money
model trustworthy.

**Design deliverables:**
- **Sources of truth (declare precedence):**
  1. Razorpay settlement report (money actually in our bank) — *authoritative for cash-in*
  2. RazorpayX payout report (money actually sent) — *authoritative for cash-out*
  3. Our `payments` + `provider_ledger_entries` + `bookings` — *our internal view*
- **Nightly Celery Beat job** (sits alongside `expire_stale_bookings`): pull
  Razorpay/RazorpayX reports for the day, match on `razorpay_payment_id` /
  `settlement_payout_id`, and flag **drift**:
  - captured at Razorpay but no `PAID` booking (or vice-versa),
  - payout `processed` at RazorpayX but booking still `PROCESSING`,
  - amount mismatches, orphan disputes, wallet balance ≠ Σ ledger entries.
- Output: a `reconciliation_runs` / `reconciliation_exceptions` table + Slack/Sentry
  alert on non-zero drift. **Never auto-corrects money** — it flags for an admin.
- **Cash commission receivable** (§3) is reconciled here: platform commission owed on
  cash jobs is netted against outbound online payouts.

---

## 9. 🟡 Abandoned-payment expiry (PARTIAL — wrong mechanism today)

**Code today:** `expire_stale_bookings` (`app/tasks/cleanup_tasks.py`) is a **wallclock
Celery Beat timer** — it expires `REQUESTED` bookings older than
`BOOKING_REQUEST_TIMEOUT_SECONDS`. The ticket **explicitly rejects a wallclock timer for
payment abandonment.** `create_order` sets **no `expire_by`** and nothing consumes an
order-expiry signal.

> The existing wallclock task governs **provider-match timeout** (a fulfilment concern)
> and is fine to keep for that. Payment abandonment is a **separate** concern and must
> use Razorpay's signal.

**Design deliverables:**
- Set **`expire_by`** on the Razorpay order at creation (order TTL = the checkout
  window, e.g. 15 min).
- Consume Razorpay's expiry: the `order.paid` webhook confirms capture; for
  abandonment, act on the **order expiry** signal (order never transitions to paid /
  Razorpay marks it expired) rather than a local clock.
- On expiry: activate the reserved `PaymentStatus.EXPIRED`, and if pay-to-book,
  release the held booking (`status → EXPIRED`) so it stops consuming capacity.
- Idempotent, and reconciled by §8 (an order Razorpay shows expired must not be `PAID`
  locally).

---

## 10. Summary — what changes vs. what's documented

| § | Item | Verdict |
|---|---|---|
| 1 | Pay-at-booking order/verify | 🟢 as-built (decide trigger timing) |
| 2 | RazorpayX payouts | 🟢 as-built (+🔴 cache contact/fund-account) |
| 3 | Cash flow | 🟢 as-built (+ receivable → §8) |
| 4 | PA/escrow + provider wallet | 🔴 new |
| 5 | Dispute state machine | 🟡 stub → full machine + clawback |
| 6 | Tips / extras | 🔴 new |
| 7 | Cancellation fee + refunds | 🟡 policy + refund mechanism |
| 8 | Reconciliation job | 🔴 new |
| 9 | Abandoned-payment expiry | 🟡 replace wallclock with order-expiry signal |

## 11. Open questions for Lead (blocking implementation tickets)

1. **Trigger timing** (§1): pay-to-book vs. pay-on-completion for online?
2. **Wallet table** (§4): build the ledger now, or keep deriving "owed" from bookings?
3. **Cancellation fee** (§7): exact fee (flat vs %) and the provider's compensation share.
4. **Tips collection** (§6): second-order capture vs. defer to payout — which path?
5. **Cash receivable netting** (§8): net cash-commission against payouts, or invoice
   providers separately?

---

_Once approved, each 🔴/🟡 section becomes its own PAY-* implementation subtask. 🟢
sections need no work beyond fixing the stale `SettlementStatus` comments noted in §0._
