from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import ActivityLog, Business, User
from apps.parties.models import Party
from apps.purchases.models import PurchaseInvoice
from apps.sales.models import SalesInvoice


class PaymentReceiptLifecycleTests(APITestCase):
    def auth_as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def setUp(self):
        self.business = Business.objects.create(name="CSM SILKS", phone="8608633066", gstin="33ABCDE1234F1Z5")
        self.other_business = Business.objects.create(name="OTHER TEXTILE", phone="9000000000")
        self.user = User.objects.create_user(
            mobile="8608633066",
            business=self.business,
            role="admin",
            first_name="CSM",
            is_active=True,
        )
        self.customer = Party.objects.create(business=self.business, name="PRAVEEN", party_type="customer")
        self.supplier = Party.objects.create(business=self.business, name="MOORTHY", party_type="supplier")
        self.other_customer = Party.objects.create(business=self.other_business, name="OTHER CUSTOMER", party_type="customer")
        self.sales_invoice = SalesInvoice.objects.create(
            business=self.business,
            invoice_number="INV/26-27/0001",
            party=self.customer,
            subtotal=Decimal("1000.00"),
            taxable_amount=Decimal("1000.00"),
            total_amount=Decimal("1000.00"),
            paid_amount=Decimal("0.00"),
            status="unpaid",
        )
        self.purchase_invoice = PurchaseInvoice.objects.create(
            business=self.business,
            invoice_number="PUR/26-27/0001",
            supplier_invoice_number="SUP-1",
            party=self.supplier,
            subtotal=Decimal("700.00"),
            taxable_amount=Decimal("700.00"),
            total_amount=Decimal("700.00"),
            paid_amount=Decimal("0.00"),
            status="unpaid",
        )

    def test_payment_in_can_settle_selected_invoice_and_rejects_cross_tenant_party(self):
        self.auth_as(self.user)
        second_invoice = SalesInvoice.objects.create(
            business=self.business,
            invoice_number="INV/26-27/0002",
            party=self.customer,
            subtotal=Decimal("500.00"),
            taxable_amount=Decimal("500.00"),
            total_amount=Decimal("500.00"),
            paid_amount=Decimal("0.00"),
            status="unpaid",
        )

        response = self.client.post("/api/v1/payments/payment-in/", {
            "party": str(self.customer.id),
            "amount_received": "250.00",
            "payment_mode": "upi",
            "settlement_allocations": [
                {"invoice": str(second_invoice.id), "settled_amount": "250.00"}
            ],
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.sales_invoice.refresh_from_db()
        second_invoice.refresh_from_db()
        self.assertEqual(self.sales_invoice.paid_amount, Decimal("0.00"))
        self.assertEqual(second_invoice.paid_amount, Decimal("250.00"))
        self.assertEqual(str(response.data["settlements"][0]["invoice"]), str(second_invoice.id))

        invalid_response = self.client.post("/api/v1/payments/payment-in/", {
            "party": str(self.other_customer.id),
            "amount_received": "100.00",
            "payment_mode": "cash",
        }, format="json")
        self.assertEqual(invalid_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_payment_out_can_settle_selected_purchase_invoice(self):
        self.auth_as(self.user)
        second_purchase = PurchaseInvoice.objects.create(
            business=self.business,
            invoice_number="PUR/26-27/0002",
            supplier_invoice_number="SUP-2",
            party=self.supplier,
            subtotal=Decimal("300.00"),
            taxable_amount=Decimal("300.00"),
            total_amount=Decimal("300.00"),
            paid_amount=Decimal("0.00"),
            status="unpaid",
        )

        response = self.client.post("/api/v1/payments/payment-out/", {
            "party": str(self.supplier.id),
            "amount_paid": "125.00",
            "payment_mode": "bank",
            "settlement_allocations": [
                {"invoice": str(second_purchase.id), "settled_amount": "125.00"}
            ],
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.purchase_invoice.refresh_from_db()
        second_purchase.refresh_from_db()
        self.assertEqual(self.purchase_invoice.paid_amount, Decimal("0.00"))
        self.assertEqual(second_purchase.paid_amount, Decimal("125.00"))
        self.assertEqual(str(response.data["settlements"][0]["invoice"]), str(second_purchase.id))

    def test_payment_in_receipt_html_text_and_void_are_real_tenant_data(self):
        self.auth_as(self.user)
        create_response = self.client.post("/api/v1/payments/payment-in/", {
            "party": str(self.customer.id),
            "amount_received": "400.00",
            "payment_mode": "cash",
            "reference_number": "CASH-1",
            "notes": "Counter receipt",
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        payment_id = create_response.data["id"]

        self.sales_invoice.refresh_from_db()
        self.assertEqual(self.sales_invoice.paid_amount, Decimal("400.00"))
        self.assertEqual(self.sales_invoice.status, "partial")

        html_response = self.client.get(f"/api/v1/payments/payment-in/{payment_id}/receipt/?export_format=html")
        self.assertEqual(html_response.status_code, status.HTTP_200_OK)
        html = html_response.content.decode("utf-8")
        self.assertIn("Payment Receipt", html)
        self.assertIn("CSM SILKS", html)
        self.assertIn("INV/26-27/0001", html)

        text_response = self.client.get(f"/api/v1/payments/payment-in/{payment_id}/receipt/?export_format=text")
        self.assertEqual(text_response.status_code, status.HTTP_200_OK)
        self.assertIn("PRAVEEN", text_response.content.decode("utf-8"))

        void_response = self.client.post(f"/api/v1/payments/payment-in/{payment_id}/void/", {
            "reason": "Duplicate receipt",
        }, format="json")
        self.assertEqual(void_response.status_code, status.HTTP_200_OK)
        self.sales_invoice.refresh_from_db()
        self.assertEqual(self.sales_invoice.paid_amount, Decimal("0.00"))
        self.assertEqual(self.sales_invoice.status, "unpaid")
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="payment_in_voided",
                entity_id=payment_id,
            ).exists()
        )

        void_text = self.client.get(f"/api/v1/payments/payment-in/{payment_id}/receipt/?export_format=text")
        self.assertIn("Status: Void", void_text.content.decode("utf-8"))
        self.assertIn("Duplicate receipt", void_text.content.decode("utf-8"))

    def test_payment_out_receipt_html_text_and_void_are_real_tenant_data(self):
        self.auth_as(self.user)
        create_response = self.client.post("/api/v1/payments/payment-out/", {
            "party": str(self.supplier.id),
            "amount_paid": "300.00",
            "payment_mode": "upi",
            "reference_number": "UPI-1",
            "notes": "Supplier part payment",
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        payment_id = create_response.data["id"]

        self.purchase_invoice.refresh_from_db()
        self.assertEqual(self.purchase_invoice.paid_amount, Decimal("300.00"))
        self.assertEqual(self.purchase_invoice.status, "partial")

        html_response = self.client.get(f"/api/v1/payments/payment-out/{payment_id}/receipt/?export_format=html")
        self.assertEqual(html_response.status_code, status.HTTP_200_OK)
        html = html_response.content.decode("utf-8")
        self.assertIn("Payment Voucher", html)
        self.assertIn("MOORTHY", html)
        self.assertIn("PUR/26-27/0001", html)

        text_response = self.client.get(f"/api/v1/payments/payment-out/{payment_id}/receipt/?export_format=text")
        self.assertEqual(text_response.status_code, status.HTTP_200_OK)
        self.assertIn("Amount Paid", text_response.content.decode("utf-8"))

        void_response = self.client.post(f"/api/v1/payments/payment-out/{payment_id}/void/", {
            "reason": "Wrong supplier payment",
        }, format="json")
        self.assertEqual(void_response.status_code, status.HTTP_200_OK)
        self.purchase_invoice.refresh_from_db()
        self.assertEqual(self.purchase_invoice.paid_amount, Decimal("0.00"))
        self.assertEqual(self.purchase_invoice.status, "unpaid")
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="payment_out_voided",
                entity_id=payment_id,
            ).exists()
        )

        void_text = self.client.get(f"/api/v1/payments/payment-out/{payment_id}/receipt/?export_format=text")
        self.assertIn("Status: Void", void_text.content.decode("utf-8"))
        self.assertIn("Wrong supplier payment", void_text.content.decode("utf-8"))
