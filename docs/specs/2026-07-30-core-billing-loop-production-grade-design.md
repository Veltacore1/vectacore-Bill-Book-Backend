# Milestone 1: Core Billing Loop, Production-Grade

**Date:** 2026-07-30
**Status:** Approved, pending implementation
**Project:** VastraBook (backend: Django/DRF, frontend: React/Vite; demo tenant: CSM SILKS)

## Context

VastraBook is a multi-tenant textile/saree billing & inventory SaaS (a MyBillBook clone), covering 10 backend Django apps and 28 frontend screens. The owner reports it has "many bugs and is not functioning properly" and wants the app brought to production-grade quality with the full application lifecycle working end to end.

That request spans too many independent subsystems to spec and fix in one pass. This document scopes **Milestone 1 only**: the core billing loop — the money-critical path every other feature in the app ultimately depends on. Later milestones (see "Out of scope" below) will each get their own design doc following this same process.

## Goal

Make the following flow work correctly, robustly, and safely for a real tenant, in one continuous session:

**Login/tenant session → Business workspace bootstrap → create Item → create Party → create Sales Invoice (referencing both) → record Payment In against it → Dashboard reflects it → it appears correctly in Reports.**

"Payment" here means the manual Payment In flow (cash/bank/UPI receipt recorded by the shop owner against an invoice) — **not** the Razorpay online payment gateway checkout, which belongs to the separate Online Orders feature and is out of scope for this milestone.

## In scope

- `apps.accounts` — login/OTP demo session, workspace bootstrap, tenant/business context, RBAC as it applies to the endpoints below.
- `apps.items` — Item creation/editing (not godowns/transfers/barcode labels/offers).
- `apps.parties` — Party creation/editing.
- `apps.sales` — Sales Invoice creation, viewing, GST/tax calculation (not quotations/challans/proforma/returns/credit-notes/e-invoicing external calls).
- `apps.payments` — Payment In creation against a Sales Invoice, invoice status transition (not Payment Out, not the Razorpay gateway).
- `apps.accounting` — just enough of the Reports engine to confirm the invoice/payment appear in the Sales Register / day-wise summary.
- Frontend equivalents: `Dashboard.tsx`, `Items.tsx`, `Parties.tsx`, `SalesInvoices.tsx`, the Payment-In UI (within `SalesRegisters.tsx` or `SalesInvoices.tsx`), `Reports.tsx`, plus the shared API client (`src/api/*`) and workspace bootstrap (`src/api/workspace.ts`) as needed to support these screens.

## Explicitly out of scope (future milestones, not touched here unless a core-loop bug forces it)

Purchases mirror-flow, Quotations/Delivery Challans/Proforma/Sales Returns/Credit Notes, Staff/Attendance/Payroll, E-invoicing (remains on `local_stub` provider), Razorpay real gateway integration, SMS/WhatsApp marketing, Shiprocket shipping, Online Orders, POS Billing, Godown/stock transfer, Settings/branding customization, and the known CSS "3 colliding theme systems" issue (`frontend/src/index.css` / `redesign.css` — see project memory `frontend-css-theme-collision`). Each becomes its own future design-doc cycle.

Also out of scope for *this* milestone's hardening bar: full OWASP-style audit, load/performance testing, and correctness of the stubbed e-invoicing/e-way-bill/Razorpay integrations (they're intentionally stubbed in dev — `local_stub`/`disabled` provider mode per `apps/accounts/checks.py` provider-boundary pattern).

## Discovery method

1. Bring up the full dev stack via the existing root `docker-compose.dev.yml` (postgres:16-alpine + Django backend on 8001 + Vite frontend on 5174, demo session + demo data seeding enabled). Nothing is currently running locally, so this is a cold start.
2. Drive the flow with the session's built-in browser tool against the seeded CSM SILKS demo tenant (OTP demo session, mobile `8608633066`), capturing:
   - Browser console errors
   - Failed/erroring network requests (status codes, response bodies)
   - Screenshots at each step
3. Walk the stages **in dependency order** — Login → Items → Parties → Sales Invoice → Payment In → Dashboard → Reports — clearing all bugs in a stage before moving to the next, since later stages depend on earlier ones producing valid data.
4. Log every bug found in the appendix below with: repro steps, expected vs. actual behavior, evidence, and root cause once identified.

## Fix workflow (per bug, before advancing to the next stage)

1. Root-cause via the `systematic-debugging` skill — no symptom-patching.
2. Fix in the minimal correct location (backend serializer/view/model, or frontend component/API client). No drive-by refactors beyond what the fix requires.
3. Add a regression test that would have caught the bug:
   - Backend: a Django `APITestCase` in the relevant app's `tests.py`, hitting the real DRF endpoint under a tenant-scoped test user.
   - Frontend: extend `e2e/navigation.spec.ts` or add a focused Playwright spec if the bug is UI-only and not exercised by an API test.
4. Re-verify the fix live in the browser.
5. Commit atomically (see "Git workflow" below).

## Hardening bar

Applies only to the endpoints/screens in scope above:

- **Error handling:** every API call from the frontend that can fail must surface a visible error state to the user — no silent failures. (Known risk areas to check: `App.tsx`'s workspace bootstrap fetch, `SalesInvoices.tsx` save/submit, `Items.tsx`/`Parties.tsx` create/edit modals.)
- **Validation:** enforced server-side as the source of truth (DRF serializer validators), client-side only as fast-feedback UX:
  - Sales Invoice line items: quantity > 0, unit price ≥ 0, referenced item/party must belong to the same tenant.
  - Item: required fields present, GST rate within a valid range (0–28 typical Indian GST slabs).
  - Party: mobile number format, GSTIN format if provided (15-char pattern), at least one contact field.
- **Tenant isolation / RBAC:** confirm Items/Parties/SalesInvoice/PaymentIn viewsets correctly filter querysets by `request.business`, and that the 5-role RBAC matrix (`admin`/`partner`/`salesman`/`accountant`/`stock_manager`) correctly gates view/create/manage access specifically on these 4 endpoint groups. This is a targeted spot-check, not a full cross-app RBAC audit.
- No regression in `python manage.py check --deploy`.
- No secrets or PII written to logs as a side effect of any fix.

## Git workflow

The project currently has no `.git` directory. Before any fix work begins:
1. `git init` at the repo root.
2. A baseline commit capturing the current state as-is (so every subsequent fix is a reviewable, revertable diff).
3. This design doc is committed as its own commit.
4. Each bug fix from then on is its own atomic commit: `fix(<app>): <short description>`, with a commit body noting the root cause.

## Testing strategy

- **Backend:** Django `APITestCase` per fixed bug, plus a happy-path test covering the full Item → Party → Invoice → Payment-In → status-transition sequence at the API level.
- **Frontend:** a new Playwright spec (`e2e/core-billing-loop.spec.ts` or similar) that codifies the full UI flow end to end — login → create item → create party → create invoice → record payment → assert dashboard/report reflect it — with the same zero-unhandled-console-errors bar `e2e/navigation.spec.ts` already applies to screen navigation.
- Existing `e2e/navigation.spec.ts`, `e2e/auth.spec.ts`, `e2e/forms.spec.ts` must continue passing (no regressions introduced).

## Acceptance criteria

- [ ] Fresh demo tenant, one continuous session: login → create Item → create Party → create Sales Invoice referencing both, with correct GST/tax math → record a Payment In (full or partial) → invoice status transitions correctly (unpaid → partial/paid) → Dashboard totals reflect the new invoice and payment → the invoice appears correctly in at least the Sales Register report.
- [ ] Zero unhandled browser console errors through that entire flow.
- [ ] A Playwright spec exists that exercises this exact flow and passes.
- [ ] Every bug found during discovery is listed in the appendix below with its fix commit reference.
- [ ] All existing e2e specs still pass.
- [ ] `python manage.py check --deploy` shows no new warnings introduced by these changes.

## Appendix: Bug log

_To be filled in during discovery — not fabricated in advance. Each entry: ID, stage, description, evidence, root cause, fix commit._

| # | Stage | Bug | Evidence | Root cause | Fix commit |
|---|-------|-----|----------|------------|------------|
| _(none yet — discovery not started)_ | | | | | |

## Follow-up milestones (not specced yet)

Roughly in likely priority order, each to go through its own brainstorming → spec → plan cycle when picked up:
1. Purchases mirror-flow (Purchase Invoice → Payment Out → stock updates)
2. Sales-side registers (Quotation, Delivery Challan, Proforma, Sales Return, Credit Note)
3. Inventory depth (Godown transfers, barcode labels, item offers/party-pricing)
4. Staff/Attendance/Payroll
5. E-invoicing real provider integration
6. Razorpay real gateway integration
7. SMS/WhatsApp marketing, Shiprocket shipping, Online Orders
8. POS Billing
9. Settings/branding, CSS theme-system cleanup
