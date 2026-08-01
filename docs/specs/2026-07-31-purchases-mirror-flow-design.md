# Milestone 2: Purchases Mirror-Flow, Production-Grade

**Date:** 2026-07-31
**Status:** Approved (continuation of Milestone 1's approved methodology, per user's "yes continue with purchases mirror-flow next")
**Project:** VastraBook (backend: Django/DRF, frontend: React/Vite; demo tenant: CSM SILKS)

## Context

Milestone 1 (`2026-07-30-core-billing-loop-production-grade-design.md`) hardened the sales-side core loop: Item → Party → Sales Invoice → Payment In → Dashboard/Reports. This milestone mirrors that same loop on the purchase side, as scoped in Milestone 1's follow-up list.

Given this milestone is a direct continuation of an already-approved process and scope, this doc is intentionally concise — it follows the same discovery method, fix workflow, hardening bar, and testing strategy as Milestone 1, just applied to the purchase-side flow.

## Goal

Make this flow work correctly, robustly, and safely, in one continuous session:

**Create Item (supplier-side reuse of Milestone 1's Item) → create Party as a supplier → create Purchase Invoice referencing both, with correct GST math → record a Payment Out against it → item stock increases correctly → invoice status transitions (unpaid → partial/paid) → Dashboard/Reports reflect it.**

## In scope

- `apps.purchases` — Purchase Invoice creation, viewing, GST/tax calculation (not Purchase Orders, Purchase Returns, Debit Notes — those are separate follow-up items).
- `apps.payments` — Payment Out creation against a Purchase Invoice, invoice status transition (not Payment In, already covered by Milestone 1).
- `apps.items` — stock increase via purchase (mirrors Milestone 1's stock-decrease-via-sale path already exercised).
- Frontend: `Purchases.tsx` (or wherever Purchase Invoice creation lives), the Payment Out UI, plus the shared API client/workspace bootstrap as needed.

## Explicitly out of scope

Purchase Orders, Purchase Returns, Debit Notes, PO-to-invoice conversion — these get their own future milestones. Anything already covered/fixed under Milestone 1 (Item/Party creation hardening, demo-session bug, CGST/SGST rounding pattern) is assumed fixed and just reused here; if the purchase-side code has its own independent copy of any of those bugs (e.g. its own tax-rounding logic, its own hidden-error modal), that's in scope to find and fix here since it's the purchase-side instance of the same defect class.

## Discovery method

Same as Milestone 1: drive the flow live via Playwright scripts against the running dev stack (already up), walking stages in dependency order (Party-as-supplier already covered by Milestone 1's Party fixes → Purchase Invoice → Payment Out → stock → Dashboard/Reports), logging bugs with evidence as found.

## Fix workflow, hardening bar, testing strategy

Identical to Milestone 1: root-cause via systematic-debugging, minimal fix in the right place, server-side validation as source of truth, regression test per fix (Django APITestCase + Playwright), atomic commits (`fix(<app>): ...`), re-verify live after each fix.

## Acceptance criteria

- [x] Fresh demo tenant: create Item → create supplier Party → create Purchase Invoice referencing both with correct GST math → record Payment Out (full or partial) → invoice status transitions correctly → item stock increases by the purchased quantity → Dashboard/Reports reflect the new purchase and payment. Verified live: Item+Party 201, Invoice 201 with reconciled GST, Payment Out 201 via FIFO settlement across multiple invoices (math verified by hand), stock increase already covered by an existing backend test, Dashboard "Latest Transactions" and Reports "Party Ledger" both reflect the new data with zero console errors.
- [x] Zero unhandled browser console errors through the flow. Confirmed by `e2e/purchases-mirror-flow.spec.ts`.
- [x] A Playwright spec exists that exercises this exact flow and passes. `e2e/purchases-mirror-flow.spec.ts` (frontend commit `8c2f4a1`).
- [x] Every bug found is listed in the appendix with its fix commit.
- [x] All existing e2e specs still pass (except the pre-existing, still-deferred Shared Ledger failure). Confirmed: 11/12 green, same single pre-existing failure as Milestone 1, no new regressions.
- [x] `python manage.py check --deploy` shows no new warnings. Same 5 intentional local-dev warnings as Milestone 1.

## Appendix: Bug log

| # | Stage | Bug | Evidence | Root cause | Fix commit |
|---|-------|-----|----------|------------|------------|
| 1 | Purchase Invoice | CGST + SGST could sum to ₹0.01 more than the invoice's own total_amount | Real invoice created with amount=101 at 5% GST gave cgst=2.53 + sgst=2.53 = 5.06 against a total only accounting for 5.05 | Copy-pasted from `api/sales.ts` before that file's Milestone 1 fix - both cgst/sgst rounded independently via two separate `roundMoney(taxTotal / 2)` calls | frontend `10099af` |
| 2 | Purchase Invoice | `PurchaseInvoiceSerializer` had no validation at all - no supplier required, zero/negative total accepted, paid_amount could exceed total, line items could have zero quantity or negative rate | Code review + confirmed via new regression tests: `POST` with a zero-quantity or negative-rate line item, or no party, or a non-positive total all returned `201` before the fix | `SalesInvoiceSerializer` (its sales-side mirror) already had this validation; it was simply never added to the purchase side | backend `52d549f` |
| 3 | Purchase Invoice / Payment Out | Failed saves showed no error at all inside the create modal - same class of bug as Milestone 1's Items/Parties fix | Confirmed: `syncNotice` only rendered in the list page's action strip, invisible behind the modal backdrop | Notice never passed into the shared create-modal JSX (same component serves Purchase Invoice, Payment Out, Purchase Return, Debit Note, Purchase Orders) | frontend `10099af` |
| 4 | Purchases + Sales registers | Any open "Create X" modal (Purchase Invoice, Payment Out, Quotation, Payment In, Sales Return, Credit Note, Delivery Challan, Proforma) silently closed and wiped in-progress form input roughly every 15 seconds | Reproduced via Playwright trace: the Amount Paid input in Create Payment Out was caught in a continuous mount/unmount loop for the full length of a slow interaction ("element was detached from the DOM, retrying") | A reset `useEffect` keyed on `[view, parties, items]` unconditionally called `setShowCreate(false)` and reset the draft; `parties`/`items` get a new array reference on every workspace refresh (incl. the ~15s realtime poll) even when the data is unchanged | frontend `475be0f` (fixed identically in both `Purchases.tsx` and `SalesRegisters.tsx`) |
| 5 | Reports (whole tenant, not purchases-specific) | The entire Reports screen 500'd for any tenant with even one item lacking an HSN code - not just the HSN Summary report, all of them, since they're built in one payload call | `GET /api/v1/accounting/reports/` → 500, backend traceback: `TypeError: '<' not supported between instances of 'NoneType' and 'str'` at `sorted(set(hsn_sales.keys()) \| set(hsn_purchases.keys()))` | The sales-side HSN key derivation already falls back to `"NA"` when `hsn_code` is `None`; the purchase-side equivalent line was missing that same `or "NA"` fallback, so a purchased item with no HSN code left a raw `None` key mixed into a `sorted()` call | backend `236ebc8` |
