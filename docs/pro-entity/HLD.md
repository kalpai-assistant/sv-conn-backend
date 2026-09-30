# Service Provider + Professional — High-Level Design

**Author**: Claude (drafted 2026-09-23; simplified 2026-09-25 misinterpreting Vipul's intent; corrected 2026-10-01 after Vipul re-explained his mental model)
**Status**: Design locked. Phase A revised ticket pending.

---

## Design history — read this first

- **2026-09-23** — original proposal: two entities (Provider + Professional) with M:N link + hidden-Provider-for-solo-Pros. Complex.
- **2026-09-25** — Vipul said *"in essence provider... only provider gets paid... no worker without provider... just a provider with no workers."* I mis-interpreted as *"collapse to one entity type."* Redesigned to single-entity with `parent_provider_id` self-link. Docs and tickets updated to match.
- **2026-10-01** — Vipul re-explained his actual mental model: two entities, 1:N (not M:N), two-level dispatch (broadcast to SP → SP picks Pro). My collapse was wrong. **This is the correct design.**

The single-entity `parent_provider_id` model is superseded. The sections below reflect the corrected model.

## Important — ship state as of 2026-10-01

The single-entity design **shipped to production on 2026-09-30 as SVC-319 / PR #185** (before the design correction happened on 2026-10-01). That means beta RDS already has:

- `provider_profiles.parent_provider_id` — nullable self-FK. Every row is currently `NULL`.
- `ProviderProfile.payout_target_id` — Python property, currently returns `user_id` for every row.

Neither is actively used by any downstream code path (Phase B hadn't shipped when SVC-319 landed).

**We are NOT reverting SVC-319.** SVC-339 lands the corrected two-entity design **additively** on top of the shipped artifacts. The `parent_provider_id` column and `payout_target_id` property remain as harmless legacy — a future cleanup ticket drops them once we're confident nothing references them. Rationale: avoids revert theater in git + alembic history, keeps beta running throughout, and dormant NULL columns cost nothing.

**Actionable ticket:** SVC-339 (Phase A corrective — two-entity migration). All Phase B/C/D tickets under SVC-318 will be rewritten to reference the two-entity design (in flight, being updated in parallel with this doc).

---

## What this is

Split the current single overloaded "provider" into two entities:

- **Service Provider (SP)** — the business/team. Receives dispatch, gets paid, holds the brand.
- **Professional (Pro)** — the person who shows up at the door. Belongs to exactly one SP.

Today's provider signup becomes SP signup with an auto-created Pro-for-self. Solo case is transparent (SP and Pro are the same person). Team case unlocks when SP invites more Pros.

## Stakeholders — unchanged

- **Consumer** — books services
- **Service Provider (SP)** — receives dispatch, accepts on behalf of the team, assigns a Pro
- **Professional (Pro)** — the person at the door; owns doorstep OTP, en_route → in_progress → completed
- **Admin** — KYC review, cancel bookings, payouts

Four stakeholders, same as before. SP vs Pro is a data-model split, not a new user role.

## Entity model

```
Consumer ─books─▶ Booking ◀─accepted by─ Service Provider ◀─belongs to─ Professional
                     │                             ▲                         ▲
                     └──── assigned to (Pro chosen by SP) ────────────────────┘
                                                    │
                                                    └──── payout target = SP
```

- **SP ← 1:N → Pro.** A Pro belongs to exactly **one** SP. No cross-SP gig workers (that shape would need a junction table; not V1).
- **Solo SP**: on signup, an SP row + a Pro-for-self row are auto-created together. Same `user_id` links them.
- **Team SP**: SP row + N Pro rows. Owner may or may not have a Pro-for-self row.
- **Payout target** is always the SP. Pro's earnings settle to `professional.service_provider`. Team-owner settles with team members off-platform.

## Three shapes of SP (all use the same data model)

1. **Solo SP** — SP row + 1 Pro-for-self row. Accepts + does the work + gets paid. All today's providers convert to this shape.
2. **Team SP, owner-also-working** — SP row + 1 Pro-for-self row + N Pro rows (team). Owner participates in dispatch pool.
3. **Team SP, owner-off-tools** — SP row + N Pro rows (team only). Owner accepts on behalf of team but never assigns himself.

Every SP on beta today = shape #1. Shapes #2 and #3 unlock in Phase B.

## Dispatch flow (two-level)

1. **Consumer books.**
2. **Dispatch broadcasts to eligible SPs** (matching service + geo + verified). Same shape as today — SPs are the recipients of dispatch, like Uber-to-drivers.
3. **SP taps "accept."**
   - **Solo SP**: instant. Booking becomes `assigned` with SP's own Pro as the assignee. No modal, no pick step.
   - **Team SP**: opens a **pick-a-Pro modal** showing team members + availability. SP picks one. That's the atomic write that locks the booking.
4. **Race resolution.** "Accept" is UI-only (no DB write for team SPs). `/assign` is the atomic operation — first-write-wins. If SP1 accepts, hesitates, and SP2 accepts + assigns first, SP1's later attempt returns **409 already assigned.** No timeouts; the race resolves itself.
5. **Consumer sees "assigned" only after Pro is picked.** Copy: *"Rohan from Sparkle Cleaners will complete your order."* Solo case (SP brand ≡ Pro name) collapses to *"Sunita will complete your order."*
6. Standard fulfillment: `assigned → en_route → in_progress → completed`.
7. **Doorstep OTP** entered by the assigned **Pro** (person at the door), not the SP.

## Pro decline / no-show

- Assigned Pro declines (via Pro app) or fails to show → SP notified → **SP re-picks** from remaining team members.
- If whole team has declined / SP is out of options → booking transitions `assigned` → `requested` → other SPs get a fresh broadcast.
- **No timeouts.** If an SP stops responding entirely, consumer's escape hatch is manual cancel + refund. SP incentive is to complete or release quickly.

## Consumer visibility rules

- **During dispatch, before assign**: "finding a professional near you..."
- **After first SP assigns a Pro**: **"{Pro name} from {SP business name} will complete your order."** Solo case collapses to just Pro name.
- **En_route / in_progress**: Pro name + SP brand persist on all screens.
- **Push notifications**: same copy shape.
- **Rating (after completion)**: consumer rates the **Pro** (person who was there). SP aggregate is derived as a weighted average.

## KYC

- **Pro** — Aadhaar identity KYC. Same AI KYC verifier (SVC-307) applies. `kyc_status` enum unchanged.
- **SP** — bank/UPI payout details + business identity (`business_name` from SVC-336).
- **Solo case**: same person completes both, but they're two distinct verifications on two distinct rows.
- **Team member Pro**: only Aadhaar. Their SP handles payout KYC.
- **Enforcement**: SP with `kyc_status != VERIFIED` cannot invite Pros (their team's payouts would fail).

## Rollout phases

- **Phase A** — schema + auto-migration. `professionals` table + `bookings.professional_id` column + data migration that auto-creates Pro-for-self for every existing SP. Zero user-visible change. **~2–3 days** (bigger than the earlier collapsed one-column ticket — two tables + booking column + data migration).
- **Phase B** — team management + two-level dispatch. SP invite API, Pro-side app UI, `POST /bookings/{id}/assign` endpoint, pick-a-Pro modal for team SPs. Pro accept/decline flow. **~1.5–2 weeks.**
- **Phase C** — consumer-facing branding. Booking payloads include SP + Pro nested. Consumer app renders "Rohan from Sparkle." FCM push copy updated. **~3–5 days.**
- **Phase D** — polish. Owner-off-tools toggle, availability advancements, etc. **~2 days.**

Phase A ships pre-emptively — invisible to users, reserves the schema. Phases B/C/D remain deferred until a real agency-trigger arrives.

## Open questions — all resolved 2026-10-01

1. **Compensation** → SP is always the payout target. Pro's earnings route via `professional.service_provider_id`.
2. **Job assignment** → Two-level. Broadcast to SPs; SP picks Pro (self for solo, from team for agencies).
3. **Rating** → Pro rated; SP aggregate derived.
4. **KYC** → Pro does Aadhaar; SP does bank/UPI + business identity. Solo case: same person, both KYCs.
5. **Migration** → Auto-create Pro-for-self for every existing SP; backfill bookings.

## What's genuinely simpler about this vs the two prior drafts

- **Vs original M:N draft**: no junction table, no cross-SP link complexity, no "which SP gets attributed when multi-linked."
- **Vs single-entity collapse**: no verbose "Solo/Team-owner/Team-member Provider" verbiage. Two distinct nouns (SP + Pro) mean what they say.
- **Vs status quo**: SPs still receive dispatch as today — the change is INTERNAL routing (SP now picks a specific Pro) plus the Pro entity existing as a first-class identity for the person at the door.

---

## Historical drafts (superseded, retained for context)

<details>
<summary><strong>Original two-entity M:N draft (2026-09-23) — rejected</strong></summary>

Same shape as V1 but with M:N linking via a `provider_professionals` junction table (a Pro could work under multiple SPs, e.g. "Rohan works for Sparkle Mon–Fri, HomeShine on weekends"). Rejected because that flexibility isn't a real signup pattern; when it shows up, we can add the junction table.

</details>

<details>
<summary><strong>Single-entity collapse (2026-09-25 to 2026-10-01) — misinterpretation</strong></summary>

One `provider_profiles` table with a nullable `parent_provider_id` self-FK. Solo/team-owner/team-member were three states of a single entity, distinguished by whether they had a parent and/or children.

Rejected on 2026-10-01: didn't match Vipul's actual mental model (which was two entities all along). Also forced verbose "Solo Provider / Team-owner Provider / Team-member Provider" nomenclature, and the "SP does dispatch, then picks a Pro from team" two-level flow couldn't be modelled naturally without materializing a distinct Pro entity anyway.

</details>
