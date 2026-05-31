import hashlib
import hmac
import json
from decimal import Decimal
from unittest import mock

from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import ActivityLog, Business, User
from apps.parties.models import Party
from apps.payments.models import PaymentGatewayEvent, PaymentGatewayOrder, PaymentIn
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


class RazorpayGatewayLifecycleTests(APITestCase):
    def auth_as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def setUp(self):
        self.business = Business.objects.create(name="CSM SILKS", phone="8608633066", invoice_prefix="CSM")
        self.user = User.objects.create_user(
            mobile="8608633066",
            business=self.business,
            role="admin",
            first_name="CSM",
            is_active=True,
        )
        self.customer = Party.objects.create(business=self.business, name="AARTHI", party_type="customer")
        self.invoice = SalesInvoice.objects.create(
            business=self.business,
            invoice_number="CSM/26-27/2001",
            party=self.customer,
            subtotal=Decimal("1250.00"),
            taxable_amount=Decimal("1250.00"),
            total_amount=Decimal("1250.00"),
            paid_amount=Decimal("0.00"),
            status="unpaid",
        )

    def gateway_settings(self):
        return override_settings(
            PAYMENT_GATEWAY_PROVIDER="razorpay",
            RAZORPAY_API_URL="https://api.razorpay.test/v1",
            RAZORPAY_KEY_ID="rzp_test_unit",
            RAZORPAY_KEY_SECRET="razorpay-secret",
            RAZORPAY_WEBHOOK_SECRET="webhook-secret",
        )

    def provider_response(self, payload):
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = json.dumps(payload).encode("utf-8")
        response.__enter__.return_value = response
        return response

    @staticmethod
    def checkout_signature(order_id, payment_id, secret="razorpay-secret"):
        return hmac.new(secret.encode("utf-8"), f"{order_id}|{payment_id}".encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def webhook_signature(raw_body, secret="webhook-secret"):
        return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    def test_gateway_order_creation_calls_razorpay_without_leaking_secret(self):
        self.auth_as(self.user)
        provider_payload = {
            "id": "order_unit_001",
            "amount": 125000,
            "currency": "INR",
            "receipt": "CSM-RZP-001",
            "status": "created",
        }

        with self.gateway_settings(), mock.patch("apps.payments.gateway.urlopen", return_value=self.provider_response(provider_payload)) as provider_call:
            response = self.client.post("/api/v1/payments/gateway/orders/", {
                "party": str(self.customer.id),
                "invoice": str(self.invoice.id),
                "amount": "1250.00",
                "notes": {"channel": "invoice-share"},
            }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = PaymentGatewayOrder.objects.get(provider_order_id="order_unit_001")
        self.assertEqual(order.amount_subunits, 125000)
        self.assertEqual(order.status, "created")
        request = provider_call.call_args.args[0]
        self.assertIn("Basic ", request.headers["Authorization"])
        self.assertNotIn("razorpay-secret", str(response.data))
        self.assertEqual(response.data["order"]["provider_order_id"], "order_unit_001")
        self.assertEqual(response.data["checkout"]["keyId"], "rzp_test_unit")
        self.assertNotIn("webhook-secret", str(response.data))

    def test_checkout_verify_marks_order_paid_once_and_creates_payment_in(self):
        self.auth_as(self.user)
        order = PaymentGatewayOrder.objects.create(
            business=self.business,
            party=self.customer,
            invoice=self.invoice,
            provider_order_id="order_unit_verify",
            receipt="CSM-RZP-VERIFY",
            amount=Decimal("1250.00"),
            amount_subunits=125000,
            created_by=self.user,
        )
        payload = {
            "razorpay_order_id": "order_unit_verify",
            "razorpay_payment_id": "pay_unit_verify",
            "razorpay_signature": self.checkout_signature("order_unit_verify", "pay_unit_verify"),
        }

        with self.gateway_settings():
            response = self.client.post(f"/api/v1/payments/gateway/orders/{order.id}/verify/", payload, format="json")
            duplicate_response = self.client.post(f"/api/v1/payments/gateway/orders/{order.id}/verify/", payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["createdPayment"])
        self.assertEqual(duplicate_response.status_code, status.HTTP_200_OK)
        self.assertFalse(duplicate_response.data["createdPayment"])
        self.assertEqual(PaymentIn.objects.filter(reference_number="pay_unit_verify").count(), 1)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_amount, Decimal("1250.00"))
        self.assertEqual(self.invoice.status, "paid")
        order.refresh_from_db()
        self.assertEqual(order.status, "paid")
        self.assertTrue(order.signature_verified)

    def test_webhook_signature_is_idempotent_and_creates_payment(self):
        order = PaymentGatewayOrder.objects.create(
            business=self.business,
            party=self.customer,
            invoice=self.invoice,
            provider_order_id="order_unit_webhook",
            receipt="CSM-RZP-WEBHOOK",
            amount=Decimal("1250.00"),
            amount_subunits=125000,
            created_by=self.user,
        )
        body = json.dumps({
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_unit_webhook",
                        "order_id": "order_unit_webhook",
                    }
                }
            }
        }).encode("utf-8")
        signature = self.webhook_signature(body)

        with self.gateway_settings():
            response = self.client.post(
                "/api/v1/payments/webhooks/razorpay/",
                body,
                content_type="application/json",
                HTTP_X_RAZORPAY_SIGNATURE=signature,
                HTTP_X_RAZORPAY_EVENT_ID="evt_unit_001",
            )
            duplicate_response = self.client.post(
                "/api/v1/payments/webhooks/razorpay/",
                body,
                content_type="application/json",
                HTTP_X_RAZORPAY_SIGNATURE=signature,
                HTTP_X_RAZORPAY_EVENT_ID="evt_unit_001",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["processed"])
        self.assertEqual(duplicate_response.status_code, status.HTTP_200_OK)
        self.assertFalse(duplicate_response.data["processed"])
        self.assertEqual(PaymentIn.objects.filter(reference_number="pay_unit_webhook").count(), 1)
        self.assertEqual(PaymentGatewayEvent.objects.filter(event_id="evt_unit_001").count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, "paid")

    def test_webhook_rejects_invalid_signature_without_creating_payment(self):
        PaymentGatewayOrder.objects.create(
            business=self.business,
            party=self.customer,
            invoice=self.invoice,
            provider_order_id="order_unit_bad_webhook",
            receipt="CSM-RZP-BADWEB",
            amount=Decimal("1250.00"),
            amount_subunits=125000,
            created_by=self.user,
        )
        body = json.dumps({
            "event": "payment.captured",
            "payload": {"payment": {"entity": {"id": "pay_bad", "order_id": "order_unit_bad_webhook"}}},
        }).encode("utf-8")

        with self.gateway_settings():
            response = self.client.post(
                "/api/v1/payments/webhooks/razorpay/",
                body,
                content_type="application/json",
                HTTP_X_RAZORPAY_SIGNATURE="bad-signature",
                HTTP_X_RAZORPAY_EVENT_ID="evt_bad_001",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(PaymentIn.objects.filter(reference_number="pay_bad").count(), 0)
        self.assertFalse(PaymentGatewayEvent.objects.filter(event_id="evt_bad_001").exists())
