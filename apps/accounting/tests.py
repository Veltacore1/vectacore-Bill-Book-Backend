from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import Business, User
from apps.accounting.models import Expense, ReportShare
from apps.items.models import Godown, Item, StockMovement
from apps.parties.models import Party
from apps.payments.models import PaymentIn, PaymentOut
from apps.purchases.models import PurchaseInvoice, PurchaseInvoiceItem
from apps.sales.models import SalesInvoice, SalesInvoiceItem


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

    def test_ca_report_share_is_persisted_for_current_tenant(self):
        self.auth_as(self.user)

        response = self.client.post(
            "/api/v1/accounting/reports/share/?report=sales-register&date_range=This%20Month",
            {"report": "sales-register", "recipient": "ca@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["share"]["reportId"], "sales-register")
        self.assertEqual(response.data["share"]["recipient"], "ca@example.com")
        share = ReportShare.objects.get(id=response.data["share"]["id"])
        self.assertEqual(share.business, self.business)
        self.assertEqual(share.created_by, self.user)
        self.assertNotEqual(share.business, self.other_business)
