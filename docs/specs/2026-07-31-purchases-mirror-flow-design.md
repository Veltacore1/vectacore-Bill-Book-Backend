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

- [ ] Fresh demo tenant: create Item → create supplier Party → create Purchase Invoice referencing both with correct GST math → record Payment Out (full or partial) → invoice status transitions correctly → item stock increases by the purchased quantity → Dashboard/Reports reflect the new purchase and payment.
- [ ] Zero unhandled browser console errors through the flow.
- [ ] A Playwright spec exists that exercises this exact flow and passes.
- [ ] Every bug found is listed in the appendix with its fix commit.
- [ ] All existing e2e specs still pass (except the pre-existing, still-deferred Shared Ledger failure).
- [ ] `python manage.py check --deploy` shows no new warnings.

## Appendix: Bug log

_To be filled in during discovery._

| # | Stage | Bug | Evidence | Root cause | Fix commit |
|---|-------|-----|----------|------------|------------|
| _(none yet — discovery not started)_ | | | | | |
