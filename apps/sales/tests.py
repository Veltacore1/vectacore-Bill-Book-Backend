from decimal import Decimal

from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import ActivityLog, Business, DocumentSequence, User
from apps.business_settings.models import BusinessPreference, InvoiceSettings
from apps.items.models import Godown, Item, ItemGodownStock
from apps.parties.models import Party
from apps.payments.models import PaymentIn, PaymentInSettlement
from apps.sales.models import CreditNote, DeliveryChallan, EInvoiceLog, ProformaInvoice, Quotation, SalesInvoice


class EInvoiceProviderBoundaryTests(APITestCase):
    def setUp(self):
        self.business = Business.objects.create(
            name="Provider Textile",
            phone="9100000001",
            gstin="33AAAAA0000A1Z5",
            invoice_prefix="PTX",
        )
        self.user = User.objects.create_user(
            mobile="9100000002",
            business=self.business,
            first_name="Admin",
            role="admin",
            is_active=True,
        )
        self.party = Party.objects.create(
            business=self.business,
            name="Retail Customer",
            party_type="customer",
            gstin="33BBBBB0000B1Z5",
        )
        self.invoice = SalesInvoice.objects.create(
            business=self.business,
            invoice_number="PTX/26-27/0001",
            party=self.party,
            subtotal=1000,
            taxable_amount=1000,
            total_amount=1050,
            paid_amount=0,
            status="unpaid",
            created_by=self.user,
        )
        token = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    @override_settings(E_INVOICE_PROVIDER="disabled", E_INVOICE_API_URL="", E_INVOICE_API_TOKEN="")
    def test_disabled_provider_fails_without_fake_irn(self):
        response = self.client.post(f"/api/v1/sales/invoices/{self.invoice.id}/trigger_einvoice/")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.einvoice_status, "failed")
        self.assertEqual(self.invoice.einvoice_provider, "disabled")
        self.assertFalse(self.invoice.irn)
        self.assertIn("not configured", self.invoice.einvoice_last_error.lower())
        self.assertEqual(EInvoiceLog.objects.filter(invoice=self.invoice, status="failed").count(), 1)
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="einvoice_failed",
                entity_id=self.invoice.id,
            ).exists()
        )

    @override_settings(E_INVOICE_PROVIDER="local_stub", E_INVOICE_API_URL="", E_INVOICE_API_TOKEN="")
    def test_local_stub_generates_qr_svg_and_cancel_lifecycle(self):
        generate_response = self.client.post(f"/api/v1/sales/invoices/{self.invoice.id}/trigger_einvoice/")

        self.assertEqual(generate_response.status_code, status.HTTP_200_OK)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.einvoice_status, "generated")
        self.assertTrue(self.invoice.irn)
        self.assertTrue(self.invoice.qr_code_data)
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="einvoice_generated",
                entity_id=self.invoice.id,
            ).exists()
        )

        qr_response = self.client.get(f"/api/v1/sales/invoices/{self.invoice.id}/einvoice_qr/")
        self.assertEqual(qr_response.status_code, status.HTTP_200_OK)
        self.assertIn("image/svg+xml", qr_response["Content-Type"])
        self.assertIn(b"<svg", qr_response.content)

        cancel_response = self.client.post(
            f"/api/v1/sales/invoices/{self.invoice.id}/cancel_einvoice/",
            {"reason": "Wrong GSTIN"},
            format="json",
        )
        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.einvoice_status, "cancelled")
        self.assertEqual(self.invoice.einvoice_cancel_reason, "Wrong GSTIN")
        self.assertEqual(EInvoiceLog.objects.filter(invoice=self.invoice, event="cancel", status="cancelled").count(), 1)
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="einvoice_cancelled",
                entity_id=self.invoice.id,
            ).exists()
        )

        retry_response = self.client.post(f"/api/v1/sales/invoices/{self.invoice.id}/retry_einvoice/")
        self.assertEqual(retry_response.status_code, status.HTTP_400_BAD_REQUEST)


class SalesInvoiceSettingsTests(APITestCase):
    def setUp(self):
        self.business = Business.objects.create(
            name="Settings Textile",
            phone="9100000101",
            gstin="33AAAAA0000A1Z5",
            invoice_prefix="OLD",
        )
        self.user = User.objects.create_user(
            mobile="9100000102",
            business=self.business,
            first_name="Admin",
            role="admin",
            is_active=True,
        )
        self.party = Party.objects.create(
            business=self.business,
            name="Walk In Customer",
            party_type="customer",
        )
        self.item = Item.objects.create(
            business=self.business,
            name="Soft Silk Saree",
            item_code="SILK-001",
            hsn_code="50072010",
            selling_price=1000,
            purchase_price=700,
            mrp=1200,
            gst_rate=5,
            current_stock=10,
        )
        self.godown = Godown.objects.create(
            business=self.business,
            name="Main Godown",
            is_default=True,
        )
        self.item.godown = self.godown
        self.item.save(update_fields=["godown"])
        ItemGodownStock.objects.create(
            business=self.business,
            item=self.item,
            godown=self.godown,
            opening_stock=10,
            current_stock=10,
        )
        InvoiceSettings.objects.create(
            business=self.business,
            invoice_prefix="POS",
            reset_each_year=True,
            show_hsn=False,
            show_mrp=False,
            show_discount=True,
            thermal_paper_size="3inch",
            thermal_theme="advanced",
            theme_color="#c80f0f",
        )
        BusinessPreference.objects.create(
            business=self.business,
            print_original_duplicate=False,
        )
        token = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_invoice_number_and_print_use_tenant_settings(self):
        response = self.client.post("/api/v1/sales/invoices/", {
            "party": str(self.party.id),
            "subtotal": "1000.00",
            "discount_amount": "50.00",
            "discount_pct": "5.00",
            "taxable_amount": "950.00",
            "cgst_amount": "23.75",
            "sgst_amount": "23.75",
            "igst_amount": "0.00",
            "cess_amount": "0.00",
            "additional_charges": "10.00",
            "additional_charges_label": "Packing",
            "total_amount": "1007.50",
            "paid_amount": "1007.50",
            "payment_mode": "cash",
            "place_of_supply": "Tamil Nadu",
            "is_pos": True,
            "line_items": [{
                "item": str(self.item.id),
                "item_name": self.item.name,
                "item_code": self.item.item_code,
                "hsn_code": self.item.hsn_code,
                "unit": "PCS",
                "quantity": "1.000",
                "free_quantity": "0.000",
                "mrp": "1200.00",
                "rate": "1000.00",
                "discount_pct": "5.00",
                "discount_amount": "50.00",
                "gst_rate": "5.00",
                "taxable_amount": "950.00",
                "tax_amount": "47.50",
                "amount": "997.50",
                "sort_order": 0,
            }],
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["invoice_number"].startswith("POS/"))
        self.assertNotIn("/OLD/", response.data["invoice_number"])

        invoice = SalesInvoice.objects.get(id=response.data["id"])
        print_response = self.client.get(f"/api/v1/sales/invoices/{invoice.id}/print_pdf/?template=thermal")
        html = print_response.content.decode("utf-8")

        self.assertEqual(print_response.status_code, status.HTTP_200_OK)
        self.assertIn("80mm", html)
        self.assertIn("Discount", html)
        self.assertIn("Packing", html)
        self.assertNotIn("<th>HSN</th>", html)

    def test_pos_invoice_without_party_uses_tenant_cash_sale_party(self):
        response = self.client.post("/api/v1/sales/invoices/", {
            "subtotal": "1000.00",
            "discount_amount": "0.00",
            "discount_pct": "0.00",
            "taxable_amount": "1000.00",
            "cgst_amount": "25.00",
            "sgst_amount": "25.00",
            "igst_amount": "0.00",
            "cess_amount": "0.00",
            "additional_charges": "0.00",
            "total_amount": "1050.00",
            "paid_amount": "1050.00",
            "payment_mode": "cash",
            "place_of_supply": "Tamil Nadu",
            "is_pos": True,
            "line_items": [{
                "item": str(self.item.id),
                "item_name": self.item.name,
                "item_code": self.item.item_code,
                "hsn_code": self.item.hsn_code,
                "unit": "PCS",
                "quantity": "1.000",
                "free_quantity": "0.000",
                "mrp": "1200.00",
                "rate": "1000.00",
                "discount_pct": "0.00",
                "discount_amount": "0.00",
                "gst_rate": "5.00",
                "taxable_amount": "1000.00",
                "tax_amount": "50.00",
                "amount": "1050.00",
                "sort_order": 0,
            }],
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["party_name"], "Cash Sale")

        invoice = SalesInvoice.objects.get(id=response.data["id"])
        self.assertEqual(invoice.party.name, "Cash Sale")
        self.assertEqual(invoice.party.business, self.business)
        self.assertEqual(PaymentIn.objects.get(reference_number=invoice.invoice_number).party, invoice.party)

    def _base_line_item(self, **overrides):
        line = {
            "item": str(self.item.id),
            "item_name": self.item.name,
            "item_code": self.item.item_code,
            "hsn_code": self.item.hsn_code,
            "unit": "PCS",
            "quantity": "1.000",
            "free_quantity": "0.000",
            "mrp": "1200.00",
            "rate": "1000.00",
            "discount_pct": "0.00",
            "discount_amount": "0.00",
            "gst_rate": "5.00",
            "taxable_amount": "1000.00",
            "tax_amount": "50.00",
            "amount": "1050.00",
            "sort_order": 0,
        }
        line.update(overrides)
        return line

    def _base_invoice_payload(self, line_item):
        return {
            "party": str(self.party.id),
            "subtotal": "1000.00",
            "discount_amount": "0.00",
            "discount_pct": "0.00",
            "taxable_amount": "1000.00",
            "cgst_amount": "25.00",
            "sgst_amount": "25.00",
            "igst_amount": "0.00",
            "cess_amount": "0.00",
            "additional_charges": "0.00",
            "total_amount": "1050.00",
            "paid_amount": "0.00",
            "payment_mode": "cash",
            "place_of_supply": "Tamil Nadu",
            "line_items": [line_item],
        }

    def test_invoice_rejects_zero_quantity_line_item(self):
        response = self.client.post(
            "/api/v1/sales/invoices/",
            self._base_invoice_payload(self._base_line_item(quantity="0.000")),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(SalesInvoice.objects.filter(subtotal=1000, taxable_amount=1000).exists())

    def test_invoice_rejects_negative_rate_line_item(self):
        response = self.client.post(
            "/api/v1/sales/invoices/",
            self._base_invoice_payload(self._base_line_item(rate="-100.00")),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SalesInvoiceLifecycleTests(APITestCase):
    def setUp(self):
        self.business = Business.objects.create(
            name="Lifecycle Textile",
            phone="9100000201",
            gstin="33AAAAA0000A1Z5",
            invoice_prefix="OLD",
        )
        self.user = User.objects.create_user(
            mobile="9100000202",
            business=self.business,
            first_name="Admin",
            role="admin",
            is_active=True,
        )
        self.party = Party.objects.create(
            business=self.business,
            name="Retail Customer",
            party_type="customer",
        )
        self.godown = Godown.objects.create(
            business=self.business,
            name="Main Godown",
            is_default=True,
        )
        self.item = Item.objects.create(
            business=self.business,
            name="Kanchipuram Silk",
            item_code="SILK-LIFE-001",
            hsn_code="50072010",
            selling_price=1000,
            purchase_price=700,
            mrp=1200,
            gst_rate=5,
            current_stock=10,
            godown=self.godown,
        )
        ItemGodownStock.objects.create(
            business=self.business,
            item=self.item,
            godown=self.godown,
            opening_stock=10,
            current_stock=10,
        )
        InvoiceSettings.objects.create(
            business=self.business,
            invoice_prefix="SAFE",
            reset_each_year=True,
        )
        token = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _invoice_payload(self, **overrides):
        payload = {
            "invoice_number": "CLIENT-FAKE-9999",
            "party": str(self.party.id),
            "subtotal": "2000.00",
            "discount_amount": "0.00",
            "discount_pct": "0.00",
            "taxable_amount": "2000.00",
            "cgst_amount": "50.00",
            "sgst_amount": "50.00",
            "igst_amount": "0.00",
            "cess_amount": "0.00",
            "additional_charges": "0.00",
            "total_amount": "2100.00",
            "paid_amount": "2100.00",
            "payment_mode": "upi",
            "place_of_supply": "Tamil Nadu",
            "is_pos": False,
            "line_items": [{
                "item": str(self.item.id),
                "item_name": self.item.name,
                "item_code": self.item.item_code,
                "hsn_code": self.item.hsn_code,
                "unit": "PCS",
                "quantity": "2.000",
                "free_quantity": "0.000",
                "mrp": "1200.00",
                "rate": "1000.00",
                "discount_pct": "0.00",
                "discount_amount": "0.00",
                "gst_rate": "5.00",
                "taxable_amount": "2000.00",
                "tax_amount": "100.00",
                "amount": "2100.00",
                "sort_order": 0,
            }],
        }
        payload.update(overrides)
        return payload

    def _current_fy(self):
        today = timezone.localdate()
        start_year = today.year if today.month >= self.business.fy_start_month else today.year - 1
        return f"{start_year % 100:02d}-{(start_year + 1) % 100:02d}"

    def test_create_uses_server_numbering_and_real_payment_stock(self):
        response = self.client.post("/api/v1/sales/invoices/", self._invoice_payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["invoice_number"].startswith("SAFE/"))
        self.assertNotEqual(response.data["invoice_number"], "CLIENT-FAKE-9999")
        self.assertEqual(response.data["status"], "paid")

        self.item.refresh_from_db()
        stock = ItemGodownStock.objects.get(business=self.business, item=self.item, godown=self.godown)
        self.assertEqual(str(self.item.current_stock), "8.000")
        self.assertEqual(str(stock.current_stock), "8.000")
        payment = PaymentIn.objects.get(business=self.business, reference_number=response.data["invoice_number"])
        self.assertEqual(payment.status, "active")
        self.assertEqual(PaymentInSettlement.objects.filter(payment_in=payment, invoice_id=response.data["id"]).count(), 1)

    def test_cross_tenant_party_or_item_is_rejected_without_stock_change(self):
        other_business = Business.objects.create(name="Other Textile", phone="9100000301", invoice_prefix="OTH")
        other_party = Party.objects.create(business=other_business, name="Other Customer", party_type="customer")
        other_godown = Godown.objects.create(business=other_business, name="Other Godown", is_default=True)
        other_item = Item.objects.create(
            business=other_business,
            name="Other Silk",
            selling_price=1000,
            purchase_price=700,
            gst_rate=5,
            current_stock=5,
            godown=other_godown,
        )
        ItemGodownStock.objects.create(
            business=other_business,
            item=other_item,
            godown=other_godown,
            opening_stock=5,
            current_stock=5,
        )

        item_payload = self._invoice_payload()
        item_payload["line_items"][0]["item"] = str(other_item.id)
        item_response = self.client.post("/api/v1/sales/invoices/", item_payload, format="json")
        self.assertEqual(item_response.status_code, status.HTTP_400_BAD_REQUEST)

        party_response = self.client.post(
            "/api/v1/sales/invoices/",
            self._invoice_payload(party=str(other_party.id)),
            format="json",
        )
        self.assertEqual(party_response.status_code, status.HTTP_400_BAD_REQUEST)

        self.item.refresh_from_db()
        other_item.refresh_from_db()
        self.assertEqual(str(self.item.current_stock), "10.000")
        self.assertEqual(str(other_item.current_stock), "5.000")
        self.assertEqual(SalesInvoice.objects.filter(business=self.business).count(), 0)

    def test_invoice_and_initial_payment_numbers_continue_from_existing_documents(self):
        fy = self._current_fy()
        SalesInvoice.objects.create(
            business=self.business,
            invoice_number=f"SAFE/{fy}/0042",
            party=self.party,
            subtotal=100,
            taxable_amount=100,
            total_amount=100,
            paid_amount=0,
            status="unpaid",
        )
        PaymentIn.objects.create(
            business=self.business,
            payment_number="PMTIN-0042",
            party=self.party,
            amount_received=100,
            payment_mode="cash",
        )

        response = self.client.post("/api/v1/sales/invoices/", self._invoice_payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["invoice_number"], f"SAFE/{fy}/0043")
        payment = PaymentIn.objects.get(business=self.business, reference_number=response.data["invoice_number"])
        self.assertEqual(payment.payment_number, "PMTIN-0043")
        self.assertEqual(
            DocumentSequence.objects.get(
                business=self.business,
                sequence_key=f"sales_invoice:SAFE:{fy}",
            ).last_number,
            43,
        )
        self.assertEqual(
            DocumentSequence.objects.get(
                business=self.business,
                sequence_key="pmtin:PMTIN",
            ).last_number,
            43,
        )

    def test_cancel_restores_stock_and_voids_initial_payment_once(self):
        create_response = self.client.post("/api/v1/sales/invoices/", self._invoice_payload(), format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        invoice_id = create_response.data["id"]

        cancel_response = self.client.post(
            f"/api/v1/sales/invoices/{invoice_id}/cancel/",
            {"reason": "Customer cancelled order"},
            format="json",
        )
        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)
        self.assertEqual(cancel_response.data["invoice"]["status"], "cancelled")
        self.assertEqual(str(cancel_response.data["invoice"]["paid_amount"]), "0.00")

        invoice = SalesInvoice.objects.get(id=invoice_id, business=self.business)
        payment = PaymentIn.objects.get(business=self.business, reference_number=invoice.invoice_number)
        self.item.refresh_from_db()
        stock = ItemGodownStock.objects.get(business=self.business, item=self.item, godown=self.godown)

        self.assertEqual(invoice.status, "cancelled")
        self.assertEqual(str(invoice.paid_amount), "0.00")
        self.assertEqual(payment.status, "void")
        self.assertIn("Customer cancelled order", payment.cancellation_reason)
        self.assertEqual(str(self.item.current_stock), "10.000")
        self.assertEqual(str(stock.current_stock), "10.000")
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="sales_invoice_cancelled",
                entity_id=invoice.id,
            ).exists()
        )

        second_cancel = self.client.post(
            f"/api/v1/sales/invoices/{invoice_id}/cancel/",
            {"reason": "Second click"},
            format="json",
        )
        self.assertEqual(second_cancel.status_code, status.HTTP_400_BAD_REQUEST)
        self.item.refresh_from_db()
        self.assertEqual(str(self.item.current_stock), "10.000")

    def test_paid_amount_cannot_exceed_total(self):
        response = self.client.post(
            "/api/v1/sales/invoices/",
            self._invoice_payload(paid_amount="2500.00"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(SalesInvoice.objects.filter(business=self.business).count(), 0)

    def test_sales_return_applies_stock_and_rejects_cross_tenant_references(self):
        invoice = SalesInvoice.objects.create(
            business=self.business,
            invoice_number=f"SAFE/{self._current_fy()}/0100",
            party=self.party,
            subtotal=Decimal("1000.00"),
            taxable_amount=Decimal("1000.00"),
            total_amount=Decimal("1000.00"),
            paid_amount=Decimal("1000.00"),
            status="paid",
        )

        response = self.client.post("/api/v1/sales/sales-returns/", {
            "party": str(self.party.id),
            "original_invoice": str(invoice.id),
            "total_amount": "1000.00",
            "reason": "Customer returned one saree",
            "line_items": [{
                "item": str(self.item.id),
                "item_name": self.item.name,
                "quantity": "1.000",
                "rate": "1000.00",
                "gst_rate": "0.00",
                "amount": "1000.00",
            }],
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.item.refresh_from_db()
        stock = ItemGodownStock.objects.get(business=self.business, item=self.item, godown=self.godown)
        self.assertEqual(str(self.item.current_stock), "11.000")
        self.assertEqual(str(stock.current_stock), "11.000")

        other_business = Business.objects.create(name="Other Sales Return Tenant", phone="9100000601", invoice_prefix="OSR")
        other_party = Party.objects.create(business=other_business, name="Other Customer", party_type="customer")
        other_item = Item.objects.create(
            business=other_business,
            name="Other Silk",
            selling_price=1000,
            purchase_price=700,
            gst_rate=0,
            current_stock=5,
        )
        other_invoice = SalesInvoice.objects.create(
            business=other_business,
            invoice_number="OSR/26-27/0001",
            party=other_party,
            subtotal=Decimal("1000.00"),
            taxable_amount=Decimal("1000.00"),
            total_amount=Decimal("1000.00"),
            paid_amount=Decimal("0.00"),
            status="unpaid",
        )

        invalid_item_response = self.client.post("/api/v1/sales/sales-returns/", {
            "party": str(self.party.id),
            "total_amount": "1000.00",
            "line_items": [{
                "item": str(other_item.id),
                "item_name": other_item.name,
                "quantity": "1.000",
                "rate": "1000.00",
                "gst_rate": "0.00",
                "amount": "1000.00",
            }],
        }, format="json")
        invalid_party_response = self.client.post("/api/v1/sales/sales-returns/", {
            "party": str(other_party.id),
            "total_amount": "1000.00",
            "line_items": [{
                "item": str(self.item.id),
                "item_name": self.item.name,
                "quantity": "1.000",
                "rate": "1000.00",
                "gst_rate": "0.00",
                "amount": "1000.00",
            }],
        }, format="json")
        invalid_invoice_response = self.client.post("/api/v1/sales/sales-returns/", {
            "party": str(self.party.id),
            "original_invoice": str(other_invoice.id),
            "total_amount": "1000.00",
            "line_items": [{
                "item": str(self.item.id),
                "item_name": self.item.name,
                "quantity": "1.000",
                "rate": "1000.00",
                "gst_rate": "0.00",
                "amount": "1000.00",
            }],
        }, format="json")

        self.assertEqual(invalid_item_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(invalid_party_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(invalid_invoice_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.item.refresh_from_db()
        self.assertEqual(str(self.item.current_stock), "11.000")

    def test_credit_note_rejects_cross_tenant_references_and_cannot_move_business(self):
        other_business = Business.objects.create(name="Other Credit Tenant", phone="9100000602", invoice_prefix="OCN")
        other_party = Party.objects.create(business=other_business, name="Other Customer", party_type="customer")
        other_invoice = SalesInvoice.objects.create(
            business=other_business,
            invoice_number="OCN/26-27/0001",
            party=other_party,
            subtotal=Decimal("1000.00"),
            taxable_amount=Decimal("1000.00"),
            total_amount=Decimal("1000.00"),
            paid_amount=Decimal("0.00"),
            status="unpaid",
        )

        create_response = self.client.post("/api/v1/sales/credit-notes/", {
            "party": str(self.party.id),
            "total_amount": "150.00",
            "reason": "Rate difference",
        }, format="json")
        invalid_party_response = self.client.post("/api/v1/sales/credit-notes/", {
            "party": str(other_party.id),
            "total_amount": "150.00",
            "reason": "Wrong tenant party",
        }, format="json")
        invalid_invoice_response = self.client.post("/api/v1/sales/credit-notes/", {
            "party": str(self.party.id),
            "original_invoice": str(other_invoice.id),
            "total_amount": "150.00",
            "reason": "Wrong tenant invoice",
        }, format="json")

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(invalid_party_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(invalid_invoice_response.status_code, status.HTTP_400_BAD_REQUEST)

        patch_response = self.client.patch(f"/api/v1/sales/credit-notes/{create_response.data['id']}/", {
            "business": str(other_business.id),
            "total_amount": "175.00",
        }, format="json")
        self.assertEqual(patch_response.status_code, status.HTTP_200_OK)
        credit_note = CreditNote.objects.get(id=create_response.data["id"])
        self.assertEqual(credit_note.business_id, self.business.id)
        self.assertEqual(credit_note.total_amount, Decimal("175.00"))


class SalesRegisterConversionTests(APITestCase):
    def setUp(self):
        self.business = Business.objects.create(
            name="Conversion Textile",
            phone="9100000401",
            gstin="33AAAAA0000A1Z5",
            invoice_prefix="OLD",
        )
        self.user = User.objects.create_user(
            mobile="9100000402",
            business=self.business,
            first_name="Admin",
            role="admin",
            is_active=True,
        )
        self.party = Party.objects.create(
            business=self.business,
            name="Retail Customer",
            party_type="customer",
        )
        self.godown = Godown.objects.create(
            business=self.business,
            name="Main Godown",
            is_default=True,
        )
        self.item = Item.objects.create(
            business=self.business,
            name="Mysore Silk Saree",
            item_code="CNV-SILK-001",
            hsn_code="50072010",
            selling_price=1000,
            purchase_price=700,
            mrp=1200,
            gst_rate=0,
            current_stock=10,
            godown=self.godown,
        )
        ItemGodownStock.objects.create(
            business=self.business,
            item=self.item,
            godown=self.godown,
            opening_stock=10,
            current_stock=10,
        )
        InvoiceSettings.objects.create(
            business=self.business,
            invoice_prefix="CNV",
            reset_each_year=True,
        )
        token = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _line_item(self, quantity="1.000"):
        return {
            "item": str(self.item.id),
            "item_name": self.item.name,
            "quantity": quantity,
            "rate": "1000.00",
            "gst_rate": "0.00",
            "amount": "1000.00",
            "sort_order": 0,
        }

    def _create_register_voucher(self, view):
        endpoints = {
            "quotation": "/api/v1/sales/quotations/",
            "delivery-challan": "/api/v1/sales/challans/",
            "proforma-invoice": "/api/v1/sales/proforma-invoices/",
        }
        payload = {
            "party": str(self.party.id),
            "total_amount": "1000.00",
            "line_items": [self._line_item()],
        }
        if view == "quotation":
            payload["subtotal"] = "1000.00"
            payload["notes"] = "Customer asked for quote"
        elif view == "delivery-challan":
            payload["notes"] = "Deliver before invoice"
        elif view == "proforma-invoice":
            payload["valid_till"] = "2026-06-05"

        response = self.client.post(endpoints[view], payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response.data

    def test_register_vouchers_convert_to_real_sales_invoices_and_reduce_stock(self):
        cases = [
            ("quotation", Quotation, "/api/v1/sales/quotations/{}/convert_to_invoice/"),
            ("delivery-challan", DeliveryChallan, "/api/v1/sales/challans/{}/convert_to_invoice/"),
            ("proforma-invoice", ProformaInvoice, "/api/v1/sales/proforma-invoices/{}/convert_to_invoice/"),
        ]

        for view, model, convert_url in cases:
            with self.subTest(view=view):
                voucher = self._create_register_voucher(view)
                response = self.client.post(convert_url.format(voucher["id"]), {}, format="json")

                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertTrue(response.data["invoice"]["invoice_number"].startswith("CNV/"))
                self.assertEqual(str(response.data["invoice"]["party"]), str(self.party.id))

                source = model.objects.get(id=voucher["id"], business=self.business)
                self.assertEqual(source.status, "converted")
                self.assertEqual(str(source.converted_invoice_id), response.data["invoice"]["id"])
                self.assertTrue(
                    ActivityLog.objects.filter(
                        business=self.business,
                        entity_id=source.id,
                        action__endswith="_converted_to_sales_invoice",
                    ).exists()
                )

        self.item.refresh_from_db()
        stock = ItemGodownStock.objects.get(business=self.business, item=self.item, godown=self.godown)
        self.assertEqual(str(self.item.current_stock), "7.000")
        self.assertEqual(str(stock.current_stock), "7.000")
        self.assertEqual(SalesInvoice.objects.filter(business=self.business).count(), 3)

    def test_register_voucher_conversion_computes_gst_from_line_items(self):
        # self.item is deliberately 0% GST elsewhere in this class, which
        # means it can never catch a broken tax calculation (0 x anything is
        # still 0). Use a real GST rate here so a regression is actually
        # detectable.
        taxed_item = Item.objects.create(
            business=self.business,
            name="Taxed Silk Saree",
            item_code="CNV-SILK-002",
            hsn_code="50072010",
            selling_price=1000,
            purchase_price=700,
            gst_rate=5,
            current_stock=10,
            godown=self.godown,
        )
        ItemGodownStock.objects.create(
            business=self.business,
            item=taxed_item,
            godown=self.godown,
            opening_stock=10,
            current_stock=10,
        )
        response = self.client.post("/api/v1/sales/quotations/", {
            "party": str(self.party.id),
            "subtotal": "1000.00",
            "total_amount": "1050.00",
            "line_items": [{
                "item": str(taxed_item.id),
                "item_name": taxed_item.name,
                "quantity": "1.000",
                "rate": "1000.00",
                "gst_rate": "5.00",
                "amount": "1050.00",
                "sort_order": 0,
            }],
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # The quotation itself has no cgst/sgst fields, so its own total is
        # only asserted at the API-payload level (frontend responsibility);
        # the invariant that actually matters is what conversion produces.

        convert_response = self.client.post(
            f"/api/v1/sales/quotations/{response.data['id']}/convert_to_invoice/", {}, format="json"
        )
        self.assertEqual(convert_response.status_code, status.HTTP_201_CREATED)
        invoice = convert_response.data["invoice"]
        self.assertEqual(invoice["taxable_amount"], "1000.00")
        cgst = Decimal(invoice["cgst_amount"])
        sgst = Decimal(invoice["sgst_amount"])
        self.assertEqual(cgst + sgst, Decimal("50.00"))
        self.assertEqual(invoice["total_amount"], "1050.00")

    def test_duplicate_conversion_is_rejected_without_second_invoice(self):
        voucher = self._create_register_voucher("quotation")
        first = self.client.post(f"/api/v1/sales/quotations/{voucher['id']}/convert_to_invoice/", {}, format="json")
        second = self.client.post(f"/api/v1/sales/quotations/{voucher['id']}/convert_to_invoice/", {}, format="json")

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("already converted", second.data["message"])
        self.assertEqual(SalesInvoice.objects.filter(business=self.business).count(), 1)
        self.item.refresh_from_db()
        self.assertEqual(str(self.item.current_stock), "9.000")

    def test_cross_tenant_register_party_or_item_is_rejected(self):
        other_business = Business.objects.create(name="Other Conversion Textile", phone="9100000501", invoice_prefix="OTH")
        other_party = Party.objects.create(business=other_business, name="Other Customer", party_type="customer")
        other_item = Item.objects.create(
            business=other_business,
            name="Other Saree",
            selling_price=1000,
            purchase_price=700,
            gst_rate=0,
            current_stock=5,
        )

        party_response = self.client.post("/api/v1/sales/quotations/", {
            "party": str(other_party.id),
            "subtotal": "1000.00",
            "total_amount": "1000.00",
            "line_items": [self._line_item()],
        }, format="json")
        item_line = self._line_item()
        item_line["item"] = str(other_item.id)
        item_response = self.client.post("/api/v1/sales/quotations/", {
            "party": str(self.party.id),
            "subtotal": "1000.00",
            "total_amount": "1000.00",
            "line_items": [item_line],
        }, format="json")

        self.assertEqual(party_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(item_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Quotation.objects.filter(business=self.business).count(), 0)
        self.item.refresh_from_db()
        self.assertEqual(str(self.item.current_stock), "10.000")
