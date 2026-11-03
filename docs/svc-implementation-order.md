# SVC Implementation Order

> Build order for the three epics filed on 2026-11-02 (SVC-406, SVC-407, SVC-435) and their 35 stories. Dependencies drive the order — follow it top-to-bottom and nothing waits on something that isn't done.

**All 35 stories are assigned to Tushar.**

---

## Epic order

1. **[SVC-407](https://kalpai.atlassian.net/browse/SVC-407)** — Admin console. Unblocks everything else.
2. **[SVC-406](https://kalpai.atlassian.net/browse/SVC-406)** — Marketplace pricing. Needs admin's pricing config (SVC-428) live first.
3. **[SVC-435](https://kalpai.atlassian.net/browse/SVC-435)** — Service addition. Needs admin shell + marketplace pricing before it has value.

---

## SVC-407 — Admin console

> Rip-and-replace of the make-shift admin. Also unlocks pricing config + review queue surfaces the other two epics assume.

1. **[SVC-421](https://kalpai.atlassian.net/browse/SVC-421)** — Scaffold. Everything else lives inside this shell.
2. **[SVC-422](https://kalpai.atlassian.net/browse/SVC-422)** — Auth + RBAC (email/pw + TOTP, 3 roles). Gates every write.
3. **[SVC-423](https://kalpai.atlassian.net/browse/SVC-423)** — Audit log. Required middleware for every write surface after this.
4. **[SVC-424](https://kalpai.atlassian.net/browse/SVC-424)** — Migrate KYC queue. First real surface — unblocks SP verification in the new app.
5. **[SVC-425](https://kalpai.atlassian.net/browse/SVC-425)** — Migrate bookings lookup.
6. **[SVC-426](https://kalpai.atlassian.net/browse/SVC-426)** — Migrate payouts dashboard.
7. **[SVC-427](https://kalpai.atlassian.net/browse/SVC-427)** — SP + Pro directory (adds Pro as first-class per SVC-318).
8. **[SVC-428](https://kalpai.atlassian.net/browse/SVC-428)** — Pricing config surface. **Hard gate for SVC-406.**
9. **[SVC-429](https://kalpai.atlassian.net/browse/SVC-429)** — Coupon management (replaces the SQL-insert flow we used for EXPO25/FIRST100).
10. **[SVC-430](https://kalpai.atlassian.net/browse/SVC-430)** — Service catalog management.
11. **[SVC-431](https://kalpai.atlassian.net/browse/SVC-431)** — Refund + dispute workflow.
12. **[SVC-432](https://kalpai.atlassian.net/browse/SVC-432)** — Analytics shell + GMV/supply dashboards.
13. **[SVC-433](https://kalpai.atlassian.net/browse/SVC-433)** — Support console (lookup-first).
14. **[SVC-434](https://kalpai.atlassian.net/browse/SVC-434)** — Decommission inline admin. Last — only after ops has used the new app for 7 days.

---

## SVC-406 — Marketplace pricing

> SP-set prices, customer-choice marketplace. The big product bet. Can start on SVC-408 the moment SVC-428 (admin pricing config) is live.

1. **[SVC-408](https://kalpai.atlassian.net/browse/SVC-408)** — Data model + migrations. Every other story imports from this.
2. **[SVC-409](https://kalpai.atlassian.net/browse/SVC-409)** — Ranking endpoint (composite score + PostGIS geo filter). Core backend logic.
3. **[SVC-414](https://kalpai.atlassian.net/browse/SVC-414)** — Boost activation hook on KYC pass. Small; land anywhere after SVC-408.
4. **[SVC-410](https://kalpai.atlassian.net/browse/SVC-410)** — Dynamic commission + Razorpay Route rework. Payments-critical.
5. **[SVC-411](https://kalpai.atlassian.net/browse/SVC-411)** — Price lock + sheet TTL. Blocks cart integration.
6. **[SVC-412](https://kalpai.atlassian.net/browse/SVC-412)** — SP pricing surface. SPs set their own prices.
7. **[SVC-413](https://kalpai.atlassian.net/browse/SVC-413)** — SP competitive view (aggregated). Parallel with SVC-412.
8. **[SVC-415](https://kalpai.atlassian.net/browse/SVC-415)** — Service tile "from ₹X" on customer home.
9. **[SVC-416](https://kalpai.atlassian.net/browse/SVC-416)** — Pro-selection bottom sheet. Central customer UX of the marketplace.
10. **[SVC-417](https://kalpai.atlassian.net/browse/SVC-417)** — Cart + checkout honors locked price.
11. **[SVC-418](https://kalpai.atlassian.net/browse/SVC-418)** — Data migration backfill. Runs just before launch, not during dev.
12. **[SVC-419](https://kalpai.atlassian.net/browse/SVC-419)** — Analytics instrumentation. Feeds dashboard + ranking-formula tuning.
13. **[SVC-420](https://kalpai.atlassian.net/browse/SVC-420)** — E2E test. Gates launch — don't ship if this fails.

---

## SVC-435 — Service addition

> Open catalog, admin-queued approval. The "any service, come on board" expo pitch, operationalized. Starts after SVC-421-423 (admin shell + auth + audit).

1. **[SVC-436](https://kalpai.atlassian.net/browse/SVC-436)** — Data model (service_requests, SP quota, spam-ban table).
2. **[SVC-437](https://kalpai.atlassian.net/browse/SVC-437)** — Duplicate detection (pg_trgm now, swappable for AI later).
3. **[SVC-438](https://kalpai.atlassian.net/browse/SVC-438)** — SP "Request a service" flow with inline suggestions.
4. **[SVC-440](https://kalpai.atlassian.net/browse/SVC-440)** — Admin review queue (list + detail + 3-action panel).
5. **[SVC-441](https://kalpai.atlassian.net/browse/SVC-441)** — Admin approve action (creates service + auto-adds to proposing SP).
6. **[SVC-442](https://kalpai.atlassian.net/browse/SVC-442)** — Admin reject-with-reason + reject-with-recommendation.
7. **[SVC-439](https://kalpai.atlassian.net/browse/SVC-439)** — SP status tracker + notifications. Depends on SVC-440 to SVC-442 producing the resolutions it reads.
8. **[SVC-443](https://kalpai.atlassian.net/browse/SVC-443)** — Analytics for service-request metrics.

---

## Notes

- **Tushar**: pick tickets top-down. Within an epic, treat the order as serial unless the "parallel with X" hint lets you overlap. Every ticket already has baked defaults for ergonomic decisions — build to spec, override via PR comment only if a default is actually wrong.
- **Vipul**: the one Vipul-flavored decision baked into tickets is [SVC-428](https://kalpai.atlassian.net/browse/SVC-428) — the initial sensitive-vs-commodity classification per category + bounds %. Default proposal is in the ticket; you can override at approval time via the admin UI, or redline the ticket now.
- **Spike reference**: [SVC-405](https://kalpai.atlassian.net/browse/SVC-405) — all SVC-406 design decisions trace back here.
