"""End-to-end API integration: register → login → CRUD → Postgres persistence."""

import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Business
from apps.business_tools.models import SMSCreditLedger
from apps.items.models import Item
from apps.parties.models import Party
from apps.payments.models import PaymentIn
from apps.sales.models import SalesInvoice

User = get_user_model()


class CoreBillingLoopIntegrationTests(APITestCase):
    """Verifies tenant onboarding and core billing data persist correctly."""

    def _register_tenant(self, *, mobile: str, password: str = "secret12"):
        response = self.client.post(
            "/api/v1/auth/register",
            {
                "business_name": "Integration Textile House",
                "owner_name": "Integration Owner",
                "mobile": mobile,
                "password": password,
                "invoice_prefix": "INT",
                "state": "Tamil Nadu",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("access", response.data["tokens"])
        self.assertIn("vastrabook_refresh", response.cookies)
        return response

    def _auth(self, access_token: str):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

    def test_register_login_party_item_invoice_and_payment_persist(self):
        mobile = f"9{uuid.uuid4().int % 10_000_000_000:010d}"[-10:]
        password = "secret12"

        register = self._register_tenant(mobile=mobile, password=password)
        business = Business.objects.get(phone=mobile)
        user = User.objects.get(mobile=mobile, business=business)
        self.assertTrue(user.check_password(password))
        self.assertGreaterEqual(
            SMSCreditLedger.objects.filter(business=business, entry_type="credit").count(),
            1,
        )

        self.client.credentials()
        login = self.client.post(
            "/api/v1/auth/login",
            {"mobile": mobile, "password": password},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK, login.data)
        self._auth(login.data["tokens"]["access"])

        party_response = self.client.post(
            "/api/v1/parties/parties/",
            {
                "name": "Integration Customer",
                "party_type": "customer",
                "mobile": "9876501234",
                "city": "Chennai",
            },
            format="json",
        )
        self.assertEqual(party_response.status_code, status.HTTP_201_CREATED, party_response.data)
        party_id = party_response.data["id"]

        godown_response = self.client.post(
            "/api/v1/items/godowns/",
            {"name": "Main Store", "is_default": True},
            format="json",
        )
        self.assertEqual(godown_response.status_code, status.HTTP_201_CREATED, godown_response.data)

        category_response = self.client.post(
            "/api/v1/items/categories/",
            {"name": "PURE SILK"},
            format="json",
        )
        self.assertEqual(category_response.status_code, status.HTTP_201_CREATED, category_response.data)

        item_response = self.client.post(
            "/api/v1/items/items/",
            {
                "name": "Integration Saree",
                "category": category_response.data["id"],
                "godown": godown_response.data["id"],
                "selling_price": "1500.00",
                "purchase_price": "900.00",
                "gst_rate": 5,
                "opening_stock": "10.000",
            },
            format="json",
        )
        self.assertEqual(item_response.status_code, status.HTTP_201_CREATED, item_response.data)
        item_id = item_response.data["id"]

        line_item = {
            "item": item_id,
            "item_name": "Integration Saree",
            "item_code": item_response.data.get("item_code") or "INT-001",
            "hsn_code": "50072010",
            "unit": "PCS",
            "quantity": "1.000",
            "free_quantity": "0.000",
            "mrp": "1800.00",
            "rate": "1500.00",
            "discount_pct": "0.00",
            "discount_amount": "0.00",
            "gst_rate": "5.00",
            "taxable_amount": "1500.00",
            "tax_amount": "75.00",
            "amount": "1575.00",
            "sort_order": 0,
        }
        invoice_response = self.client.post(
            "/api/v1/sales/invoices/",
            {
                "party": party_id,
                "subtotal": "1500.00",
                "discount_amount": "0.00",
                "discount_pct": "0.00",
                "taxable_amount": "1500.00",
                "cgst_amount": "37.50",
                "sgst_amount": "37.50",
                "igst_amount": "0.00",
                "cess_amount": "0.00",
                "additional_charges": "0.00",
                "total_amount": "1575.00",
                "paid_amount": "0.00",
                "payment_mode": "cash",
                "place_of_supply": "Tamil Nadu",
                "line_items": [line_item],
            },
            format="json",
        )
        self.assertEqual(invoice_response.status_code, status.HTTP_201_CREATED, invoice_response.data)
        invoice_id = invoice_response.data["id"]
        invoice_number = invoice_response.data["invoice_number"]

        payment_response = self.client.post(
            "/api/v1/payments/payment-in/",
            {
                "party": party_id,
                "amount_received": "500.00",
                "payment_mode": "cash",
                "settlement_allocations": [
                    {"invoice": invoice_id, "settled_amount": "500.00"}
                ],
            },
            format="json",
        )
        self.assertEqual(payment_response.status_code, status.HTTP_201_CREATED, payment_response.data)

        workspace = self.client.get("/api/v1/auth/workspace")
        self.assertEqual(workspace.status_code, status.HTTP_200_OK)
        party_names = {row["name"] for row in workspace.data["parties"]}
        item_names = {row["name"] for row in workspace.data["items"]}
        invoice_numbers = {row["invoiceNumber"] for row in workspace.data["invoices"]}
        self.assertIn("Integration Customer", party_names)
        self.assertIn("Integration Saree", item_names)
        self.assertIn(invoice_number, invoice_numbers)
        self.assertEqual(workspace.data["counts"]["salesInvoices"], 1)
        self.assertEqual(workspace.data["counts"]["paymentsIn"], 1)

        invoice = SalesInvoice.objects.get(id=invoice_id, business=business)
        party = Party.objects.get(id=party_id, business=business)
        item = Item.objects.get(id=item_id, business=business)
        self.assertEqual(invoice.party_id, party.id)
        self.assertEqual(invoice.total_amount, Decimal("1575.00"))
        self.assertEqual(invoice.paid_amount, Decimal("500.00"))
        self.assertEqual(item.current_stock, Decimal("9.000"))
        self.assertTrue(
            PaymentIn.objects.filter(business=business, party=party, amount_received=Decimal("500.00")).exists()
        )

        list_parties = self.client.get("/api/v1/parties/parties/")
        self.assertEqual(list_parties.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_parties.data), 1)
