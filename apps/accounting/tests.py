import json
from unittest import mock
from decimal import Decimal

from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import Business, User
from apps.accounting.models import AutomatedBill, BankAccount, BankTransaction, Expense, ReportShare
from apps.items.models import Godown, Item, StockMovement
from apps.parties.models import Party
from apps.payments.models import PaymentIn, PaymentOut
from apps.purchases.models import PurchaseInvoice, PurchaseInvoiceItem
from apps.sales.models import SalesInvoice, SalesInvoiceItem


class BankTransactionLifecycleTests(APITestCase):
    def auth_as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def setUp(self):
        self.business = Business.objects.create(name="Bank Tenant", phone="9000000901")
        self.other_business = Business.objects.create(name="Other Bank Tenant", phone="9000000902")
        self.user = User.objects.create_user(
            mobile="9000000903",
            business=self.business,
            role="admin",
            first_name="Bank Admin",
            is_active=True,
        )
        self.account = BankAccount.objects.create(
            business=self.business,
            account_name="Main Bank",
            account_number="BANK-1",
            ifsc_code="BANK0001",
            bank_name="Primary Bank",
            opening_balance=Decimal("1000.00"),
            current_balance=Decimal("1000.00"),
        )
        self.other_account = BankAccount.objects.create(
            business=self.other_business,
            account_name="Other Bank",
            account_number="BANK-2",
            ifsc_code="BANK0002",
            bank_name="Other Bank",
            opening_balance=Decimal("500.00"),
            current_balance=Decimal("500.00"),
        )
        self.auth_as(self.user)

    def test_bank_transaction_create_update_delete_keeps_account_balance_real(self):
        create_response = self.client.post("/api/v1/accounting/transactions/", {
            "bank_account": str(self.account.id),
            "transaction_type": "deposit",
            "amount": "250.00",
            "reference_number": "DEP-1",
            "description": "Counter deposit",
        }, format="json")

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal("1250.00"))

        patch_response = self.client.patch(f"/api/v1/accounting/transactions/{create_response.data['id']}/", {
            "transaction_type": "withdrawal",
            "amount": "100.00",
            "description": "Corrected as withdrawal",
        }, format="json")

        self.assertEqual(patch_response.status_code, status.HTTP_200_OK)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal("900.00"))

        delete_response = self.client.delete(f"/api/v1/accounting/transactions/{create_response.data['id']}/")

        self.assertEqual(delete_response.status_code, status.HTTP_200_OK)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal("1000.00"))
        self.assertFalse(BankTransaction.objects.filter(id=create_response.data["id"]).exists())

    def test_bank_transaction_rejects_cross_tenant_account_without_balance_change(self):
        response = self.client.post("/api/v1/accounting/transactions/", {
            "bank_account": str(self.other_account.id),
            "transaction_type": "deposit",
            "amount": "250.00",
            "reference_number": "BAD-1",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.account.refresh_from_db()
        self.other_account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal("1000.00"))
        self.assertEqual(self.other_account.current_balance, Decimal("500.00"))
        self.assertEqual(BankTransaction.objects.filter(business=self.business).count(), 0)

    def test_bank_transfer_moves_balance_between_accounts_and_records_both_legs(self):
        second_account = BankAccount.objects.create(
            business=self.business,
            account_name="Secondary Bank",
            account_number="BANK-3",
            ifsc_code="BANK0003",
            bank_name="Secondary Bank",
            opening_balance=Decimal("0.00"),
            current_balance=Decimal("0.00"),
        )

        response = self.client.post("/api/v1/accounting/accounts/transfer/", {
            "from_account": str(self.account.id),
            "to_account": str(second_account.id),
            "amount": "300.00",
            "description": "Move to secondary",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.account.refresh_from_db()
        second_account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal("700.00"))
        self.assertEqual(second_account.current_balance, Decimal("300.00"))
        self.assertEqual(
            BankTransaction.objects.filter(bank_account=self.account, transaction_type="withdrawal").count(), 1
        )
        self.assertEqual(
            BankTransaction.objects.filter(bank_account=second_account, transaction_type="deposit").count(), 1
        )

    def test_bank_transfer_rejects_same_account_non_positive_amount_and_cross_tenant(self):
        same_account_response = self.client.post("/api/v1/accounting/accounts/transfer/", {
            "from_account": str(self.account.id),
            "to_account": str(self.account.id),
            "amount": "100.00",
        }, format="json")
        self.assertEqual(same_account_response.status_code, status.HTTP_400_BAD_REQUEST)

        second_account = BankAccount.objects.create(
            business=self.business,
            account_name="Secondary Bank",
            account_number="BANK-4",
            ifsc_code="BANK0004",
            bank_name="Secondary Bank",
        )
        zero_amount_response = self.client.post("/api/v1/accounting/accounts/transfer/", {
            "from_account": str(self.account.id),
            "to_account": str(second_account.id),
            "amount": "0.00",
        }, format="json")
        self.assertEqual(zero_amount_response.status_code, status.HTTP_400_BAD_REQUEST)

        cross_tenant_response = self.client.post("/api/v1/accounting/accounts/transfer/", {
            "from_account": str(self.account.id),
            "to_account": str(self.other_account.id),
            "amount": "100.00",
        }, format="json")
        self.assertEqual(cross_tenant_response.status_code, status.HTTP_404_NOT_FOUND)

        self.account.refresh_from_db()
        self.other_account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal("1000.00"))
        self.assertEqual(self.other_account.current_balance, Decimal("500.00"))


class AutomatedBillTests(APITestCase):
    def setUp(self):
        self.business = Business.objects.create(name="Bill Tenant", phone="9000000904")
        self.user = User.objects.create_user(
            mobile="9000000905",
            business=self.business,
            role="admin",
            first_name="Bill Admin",
            is_active=True,
        )
        token = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_automated_bill_rejects_zero_and_negative_amount(self):
        zero_response = self.client.post("/api/v1/accounting/recurring-bills/", {
            "bill_name": "Zero Rent",
            "amount": "0.00",
            "frequency": "monthly",
            "next_due_date": "2026-09-01",
        }, format="json")
        self.assertEqual(zero_response.status_code, status.HTTP_400_BAD_REQUEST)

        negative_response = self.client.post("/api/v1/accounting/recurring-bills/", {
            "bill_name": "Negative Rent",
            "amount": "-500.00",
            "frequency": "monthly",
            "next_due_date": "2026-09-01",
        }, format="json")
        self.assertEqual(negative_response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertEqual(AutomatedBill.objects.filter(business=self.business).count(), 0)

        valid_response = self.client.post("/api/v1/accounting/recurring-bills/", {
            "bill_name": "Showroom Rent",
            "amount": "15000.00",
            "frequency": "monthly",
            "next_due_date": "2026-09-01",
        }, format="json")
        self.assertEqual(valid_response.status_code, status.HTTP_201_CREATED)


class ReportsExportTests(APITestCase):
    def auth_as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def setUp(self):
        self.business = Business.objects.create(name="CSM SILKS", phone="8608633066", gstin="33ABCDE1234F1Z5")
        self.other_business = Business.objects.create(name="Other Textile", phone="9000000000")
        self.user = User.objects.create_user(
            mobile="8608633066",
            business=self.business,
            role="admin",
            first_name="CSM",
            is_active=True,
        )
        self.customer = Party.objects.create(
            business=self.business,
            name="PRAVEEN",
            party_type="customer",
            opening_balance=Decimal("100.00"),
            opening_balance_type="debit",
        )
        self.supplier = Party.objects.create(
            business=self.business,
            name="MOORTHY",
            party_type="supplier",
            opening_balance=Decimal("75.00"),
            opening_balance_type="credit",
        )
        self.godown = Godown.objects.create(business=self.business, name="Main Godown", is_default=True)
        self.item = Item.objects.create(
            business=self.business,
            godown=self.godown,
            name="BASIC-GFC6-9ATM00",
            item_code="210191761827",
            hsn_code="50072010",
            unit="PCS",
            selling_price=Decimal("1000.00"),
            purchase_price=Decimal("700.00"),
            current_stock=Decimal("4.000"),
        )
        self.invoice = SalesInvoice.objects.create(
            business=self.business,
            invoice_number="INV/26-27/0001",
            party=self.customer,
            subtotal=Decimal("1000.00"),
            discount_amount=Decimal("50.00"),
            taxable_amount=Decimal("950.00"),
            cgst_amount=Decimal("23.75"),
            sgst_amount=Decimal("23.75"),
            additional_charges=Decimal("20.00"),
            additional_charges_label="Packing",
            total_amount=Decimal("997.50"),
            paid_amount=Decimal("300.00"),
            status="partial",
            created_by=self.user,
        )
        SalesInvoiceItem.objects.create(
            invoice=self.invoice,
            item=self.item,
            item_name=self.item.name,
            item_code=self.item.item_code,
            hsn_code=self.item.hsn_code,
            quantity=Decimal("1.000"),
            rate=Decimal("1000.00"),
            discount_pct=Decimal("5.00"),
            discount_amount=Decimal("50.00"),
            gst_rate=Decimal("5.00"),
            taxable_amount=Decimal("950.00"),
            tax_amount=Decimal("47.50"),
            amount=Decimal("997.50"),
        )
        purchase = PurchaseInvoice.objects.create(
            business=self.business,
            invoice_number="PUR/26-27/0001",
            supplier_invoice_number="SUP-1",
            party=self.supplier,
            subtotal=Decimal("700.00"),
            taxable_amount=Decimal("700.00"),
            cgst_amount=Decimal("17.50"),
            sgst_amount=Decimal("17.50"),
            total_amount=Decimal("735.00"),
            paid_amount=Decimal("100.00"),
            status="partial",
        )
        PurchaseInvoiceItem.objects.create(
            invoice=purchase,
            item=self.item,
            item_name=self.item.name,
            quantity=Decimal("1.000"),
            rate=Decimal("700.00"),
            gst_rate=Decimal("5.00"),
            taxable_amount=Decimal("700.00"),
            amount=Decimal("735.00"),
        )
        PaymentIn.objects.create(
            business=self.business,
            payment_number="PMTIN-0001",
            party=self.customer,
            amount_received=Decimal("300.00"),
            reference_number=self.invoice.invoice_number,
            created_by=self.user,
        )
        PaymentOut.objects.create(
            business=self.business,
            payment_number="PMTOUT-0001",
            party=self.supplier,
            amount_paid=Decimal("100.00"),
            reference_number=purchase.invoice_number,
            created_by=self.user,
        )
        Expense.objects.create(
            business=self.business,
            expense_number="EXP-0001",
            expense_category="Packing",
            total_amount=Decimal("25.00"),
            paid_amount=Decimal("25.00"),
            created_by=self.user,
        )
        StockMovement.objects.create(
            business=self.business,
            item=self.item,
            godown=self.godown,
            movement_type="sale",
            reference_type="sales_invoice",
            reference_id=self.invoice.id,
            quantity=Decimal("-1.000"),
            rate=Decimal("1000.00"),
            balance_after=Decimal("3.000"),
            created_by=self.user,
        )
        self.other_party = Party.objects.create(business=self.other_business, name="OTHER CUSTOMER", party_type="customer")
        SalesInvoice.objects.create(
            business=self.other_business,
            invoice_number="OTHER/0001",
            party=self.other_party,
            total_amount=Decimal("9999.00"),
        )

    def test_reports_include_mybillbook_style_ledgers_from_tenant_data(self):
        self.auth_as(self.user)
        response = self.client.get("/api/v1/accounting/reports/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        report_ids = {report["id"] for report in response.data["reports"]}
        self.assertTrue({
            "sales-register",
            "sales-tax-register",
            "purchase-register",
            "purchase-tax-register",
            "payment-in-report",
            "payment-out-report",
            "stock-ledger",
            "stock-valuation",
            "item-profitability",
            "item-purchase",
            "party-statement",
            "party-wise-profit",
            "receivable-ageing",
            "expense-register",
            "bank-book",
            "cash-flow",
            "hsn-summary",
            "e-invoice-status",
            "tax-summary",
        }.issubset(report_ids))
        sales_register = next(report for report in response.data["reports"] if report["id"] == "sales-register")
        self.assertEqual(sales_register["metricValue"], "1")
        self.assertIn("INV/26-27/0001", sales_register["rows"][0])
        hsn_summary = next(report for report in response.data["reports"] if report["id"] == "hsn-summary")
        self.assertEqual(hsn_summary["rows"][0][0], "50072010")
        item_profit = next(report for report in response.data["reports"] if report["id"] == "item-profitability")
        self.assertEqual(item_profit["rows"][0][0], "BASIC-GFC6-9ATM00")
        self.assertIn("\u20b9 250.00", item_profit["rows"][0])

    def test_reports_do_not_crash_when_a_purchased_item_has_no_hsn_code(self):
        no_hsn_item = Item.objects.create(
            business=self.business,
            godown=self.godown,
            name="LOOSE THREAD SPOOL",
            item_code="NOHSN-001",
            hsn_code=None,
            unit="PCS",
            selling_price=Decimal("50.00"),
            purchase_price=Decimal("30.00"),
            current_stock=Decimal("10.000"),
        )
        no_hsn_purchase = PurchaseInvoice.objects.create(
            business=self.business,
            invoice_number="PUR/26-27/0002",
            party=self.supplier,
            subtotal=Decimal("30.00"),
            taxable_amount=Decimal("30.00"),
            total_amount=Decimal("30.00"),
            status="unpaid",
        )
        PurchaseInvoiceItem.objects.create(
            invoice=no_hsn_purchase,
            item=no_hsn_item,
            item_name=no_hsn_item.name,
            quantity=Decimal("1.000"),
            rate=Decimal("30.00"),
            gst_rate=Decimal("0.00"),
            taxable_amount=Decimal("30.00"),
            amount=Decimal("30.00"),
        )

        self.auth_as(self.user)
        response = self.client.get("/api/v1/accounting/reports/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        hsn_summary = next(report for report in response.data["reports"] if report["id"] == "hsn-summary")
        hsn_labels = {row[0] for row in hsn_summary["rows"]}
        self.assertIn("NA", hsn_labels)
        self.assertIn("50072010", hsn_labels)

    def test_report_exports_are_generated_server_side_and_tenant_scoped(self):
        self.auth_as(self.user)

        csv_response = self.client.get("/api/v1/accounting/reports/export/?report=sales-register&export_format=csv")
        self.assertEqual(csv_response.status_code, status.HTTP_200_OK)
        self.assertIn("text/csv", csv_response["Content-Type"])
        csv_body = csv_response.content.decode("utf-8")
        self.assertIn("INV/26-27/0001", csv_body)
        self.assertIn("PRAVEEN", csv_body)
        self.assertNotIn("OTHER/0001", csv_body)

        html_response = self.client.get("/api/v1/accounting/reports/export/?report=party-statement&export_format=html")
        self.assertEqual(html_response.status_code, status.HTTP_200_OK)
        html_body = html_response.content.decode("utf-8")
        self.assertIn("CSM SILKS", html_body)
        self.assertIn("Party Statement", html_body)

        excel_response = self.client.get("/api/v1/accounting/reports/export/?report=stock-ledger&export_format=excel")
        self.assertEqual(excel_response.status_code, status.HTTP_200_OK)
        self.assertIn("application/vnd.ms-excel", excel_response["Content-Type"])
        self.assertIn("stock-ledger.xls", excel_response["Content-Disposition"])

    def test_report_filters_are_tenant_scoped_and_apply_to_party_reports(self):
        self.auth_as(self.user)

        response = self.client.get(f"/api/v1/accounting/reports/?party={self.customer.id}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        party_statement = next(report for report in response.data["reports"] if report["id"] == "party-statement")
        party_names = [row[0] for row in party_statement["rows"]]
        self.assertEqual(party_names, ["PRAVEEN"])
        self.assertEqual(party_statement["filters"]["Party"], "PRAVEEN")

        invalid_response = self.client.get(f"/api/v1/accounting/reports/?party={self.other_party.id}")
        self.assertEqual(invalid_response.status_code, status.HTTP_404_NOT_FOUND)

    @override_settings(
        DEBUG=False,
        EMAIL_PROVIDER="resend",
        RESEND_API_URL="https://api.resend.test/emails",
        RESEND_API_KEY="test-resend-token",
        RESEND_FROM_EMAIL="reports@example.com",
    )
    def test_ca_report_share_is_persisted_sent_and_public_link_is_tenant_scoped(self):
        self.auth_as(self.user)
        provider_response = mock.MagicMock()
        provider_response.status = 202
        provider_response.read.return_value = b'{"id":"email_test_report"}'
        provider_response.__enter__.return_value = provider_response

        with mock.patch("apps.accounts.email_delivery.urlopen", return_value=provider_response) as provider_call:
            response = self.client.post(
                "/api/v1/accounting/reports/share/?report=sales-register&date_range=This%20Month",
                {"report": "sales-register", "recipient": "ca@example.com"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["share"]["reportId"], "sales-register")
        self.assertEqual(response.data["share"]["recipient"], "ca@example.com")
        self.assertEqual(response.data["share"]["status"], "sent")
        self.assertTrue(response.data["share"]["delivery"]["delivered"])
        self.assertTrue(provider_call.called)
        email_payload = json.loads(provider_call.call_args.args[0].data.decode("utf-8"))
        self.assertIn("/api/v1/accounting/reports/shared/", email_payload["html"])
        share = ReportShare.objects.get(id=response.data["share"]["id"])
        self.assertEqual(share.business, self.business)
        self.assertEqual(share.created_by, self.user)
        self.assertEqual(share.status, "sent")
        self.assertTrue(share.filters["emailDelivery"]["delivered"])
        self.assertNotEqual(share.business, self.other_business)

        shared_response = self.client.get(f"/api/v1/accounting/reports/shared/{share.share_token}/")
        self.assertEqual(shared_response.status_code, status.HTTP_200_OK)
        self.assertIn("text/html", shared_response["Content-Type"])
        shared_html = shared_response.content.decode("utf-8")
        self.assertIn("Sales Register", shared_html)
        self.assertIn("INV/26-27/0001", shared_html)
        self.assertNotIn("OTHER/0001", shared_html)
