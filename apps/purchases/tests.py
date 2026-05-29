from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import ActivityLog, Business, User
from apps.items.models import Godown, Item, ItemGodownStock
from apps.parties.models import Party
from apps.payments.models import PaymentOut
from apps.purchases.models import PurchaseInvoice, PurchaseOrder


class PurchaseLifecycleAuditTests(APITestCase):
    def setUp(self):
        self.business = Business.objects.create(
            name="Purchase Audit Textile",
            phone="9100000701",
            gstin="33AAAAA0000A1Z5",
            invoice_prefix="PAT",
        )
        self.user = User.objects.create_user(
            mobile="9100000702",
            business=self.business,
            first_name="Admin",
            role="admin",
            is_active=True,
        )
        self.supplier = Party.objects.create(
            business=self.business,
            name="Kanchipuram Supplier",
            party_type="supplier",
        )
        self.godown = Godown.objects.create(
            business=self.business,
            name="Main Godown",
            is_default=True,
        )
        self.item = Item.objects.create(
            business=self.business,
            name="Soft Silk Saree",
            item_code="PUR-SILK-001",
            selling_price=1500,
            purchase_price=900,
            gst_rate=5,
            current_stock=Decimal("10.000"),
            godown=self.godown,
        )
        ItemGodownStock.objects.create(
            business=self.business,
            item=self.item,
            godown=self.godown,
            opening_stock=Decimal("10.000"),
            current_stock=Decimal("10.000"),
        )
        token = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _line_item(self, quantity="2.000"):
        return {
            "item": str(self.item.id),
            "item_name": self.item.name,
            "quantity": quantity,
            "rate": "900.00",
            "gst_rate": "0.00",
            "taxable_amount": "1800.00",
            "amount": "1800.00",
            "sort_order": 0,
        }

    def test_purchase_invoice_cancel_reverses_stock_payments_and_audits(self):
        create_response = self.client.post("/api/v1/purchases/invoices/", {
            "party": str(self.supplier.id),
            "supplier_invoice_number": "SUP-100",
            "subtotal": "1800.00",
            "taxable_amount": "1800.00",
            "total_amount": "1800.00",
            "paid_amount": "1800.00",
            "payment_mode": "bank",
            "line_items": [self._line_item()],
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)

        self.item.refresh_from_db()
        self.assertEqual(str(self.item.current_stock), "12.000")
        payment = PaymentOut.objects.get(business=self.business, reference_number=create_response.data["invoice_number"])
        self.assertEqual(payment.status, "active")

        cancel_response = self.client.post(
            f"/api/v1/purchases/invoices/{create_response.data['id']}/cancel/",
            {},
            format="json",
        )
        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)

        invoice = PurchaseInvoice.objects.get(id=create_response.data["id"], business=self.business)
        payment.refresh_from_db()
        self.item.refresh_from_db()
        stock = ItemGodownStock.objects.get(business=self.business, item=self.item, godown=self.godown)

        self.assertEqual(invoice.status, "cancelled")
        self.assertEqual(invoice.paid_amount, Decimal("0.00"))
        self.assertEqual(payment.status, "void")
        self.assertEqual(str(self.item.current_stock), "10.000")
        self.assertEqual(str(stock.current_stock), "10.000")
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="purchase_invoice_cancelled",
                entity_id=invoice.id,
            ).exists()
        )

    def test_purchase_order_convert_cancel_and_debit_note_cancel_are_audited(self):
        order_response = self.client.post("/api/v1/purchases/orders/", {
            "party": str(self.supplier.id),
            "total_amount": "1800.00",
            "line_items": [self._line_item()],
        }, format="json")
        self.assertEqual(order_response.status_code, status.HTTP_201_CREATED)

        convert_response = self.client.post(
            f"/api/v1/purchases/orders/{order_response.data['id']}/convert_to_invoice/",
            {},
            format="json",
        )
        self.assertEqual(convert_response.status_code, status.HTTP_200_OK)
        order = PurchaseOrder.objects.get(id=order_response.data["id"], business=self.business)
        self.assertEqual(order.status, "converted")
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="purchase_order_converted_to_invoice",
                entity_id=order.id,
            ).exists()
        )

        cancel_order_response = self.client.post("/api/v1/purchases/orders/", {
            "party": str(self.supplier.id),
            "total_amount": "1800.00",
            "line_items": [self._line_item()],
        }, format="json")
        self.assertEqual(cancel_order_response.status_code, status.HTTP_201_CREATED)
        cancel_response = self.client.post(
            f"/api/v1/purchases/orders/{cancel_order_response.data['id']}/cancel/",
            {},
            format="json",
        )
        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="purchase_order_cancelled",
                entity_id=cancel_order_response.data["id"],
            ).exists()
        )

        debit_response = self.client.post("/api/v1/purchases/debit-notes/", {
            "party": str(self.supplier.id),
            "total_amount": "250.00",
            "reason": "Rate difference",
        }, format="json")
        self.assertEqual(debit_response.status_code, status.HTTP_201_CREATED)
        debit_cancel = self.client.post(
            f"/api/v1/purchases/debit-notes/{debit_response.data['id']}/cancel/",
            {},
            format="json",
        )
        self.assertEqual(debit_cancel.status_code, status.HTTP_200_OK)
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="debit_note_cancelled",
                entity_id=debit_response.data["id"],
            ).exists()
        )
