from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import ActivityLog, Business, User
from apps.items.models import Godown, Item, ItemGodownStock
from apps.parties.models import Party
from apps.payments.models import PaymentOut
from apps.purchases.models import DebitNote, PurchaseInvoice, PurchaseOrder, PurchaseReturn


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

    def _current_fy(self):
        today = timezone.localdate()
        start_year = today.year if today.month >= self.business.fy_start_month else today.year - 1
        return f"{start_year % 100:02d}-{(start_year + 1) % 100:02d}"

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

    def test_purchase_invoice_print_pdf_shows_every_line_item_and_gst_breakdown(self):
        # buildPurchaseVoucherHtml (the old client-side print path) only ever
        # rendered row.itemName - the first line item - and had no CGST/SGST
        # breakdown at all. This exercises the new server-rendered
        # print_pdf endpoint against a two-line, GST-rated invoice to prove
        # every line item and the full tax breakdown actually appear.
        second_item = Item.objects.create(
            business=self.business,
            name="Zari Border Saree",
            item_code="PUR-SILK-002",
            selling_price=2500,
            purchase_price=1500,
            gst_rate=5,
            current_stock=Decimal("5.000"),
            godown=self.godown,
        )
        line_items = [
            {
                "item": str(self.item.id),
                "item_name": self.item.name,
                "quantity": "2.000",
                "rate": "900.00",
                "gst_rate": "5.00",
                "taxable_amount": "1800.00",
                "amount": "1890.00",
                "sort_order": 0,
            },
            {
                "item": str(second_item.id),
                "item_name": second_item.name,
                "quantity": "1.000",
                "rate": "1500.00",
                "gst_rate": "5.00",
                "taxable_amount": "1500.00",
                "amount": "1575.00",
                "sort_order": 1,
            },
        ]
        create_response = self.client.post("/api/v1/purchases/invoices/", {
            "party": str(self.supplier.id),
            "subtotal": "3300.00",
            "taxable_amount": "3300.00",
            "cgst_amount": "82.50",
            "sgst_amount": "82.50",
            "total_amount": "3465.00",
            "paid_amount": "0.00",
            "line_items": line_items,
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        invoice_id = create_response.data["id"]

        print_response = self.client.get(f"/api/v1/purchases/invoices/{invoice_id}/print_pdf/?template=a4")

        self.assertEqual(print_response.status_code, status.HTTP_200_OK)
        html = print_response.content.decode()
        self.assertIn(self.item.name, html)
        self.assertIn(second_item.name, html)
        self.assertIn("Rs. 82.50", html)  # CGST
        self.assertIn("Rs. 3,465.00", html)  # total

    def test_purchase_invoice_rejects_zero_quantity_and_negative_rate_line_items(self):
        zero_qty_response = self.client.post("/api/v1/purchases/invoices/", {
            "party": str(self.supplier.id),
            "subtotal": "1800.00",
            "taxable_amount": "1800.00",
            "total_amount": "1800.00",
            "paid_amount": "0.00",
            "line_items": [self._line_item(quantity="0.000")],
        }, format="json")
        self.assertEqual(zero_qty_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(PurchaseInvoice.objects.filter(business=self.business, subtotal="1800.00").exists())

        negative_rate_line = self._line_item()
        negative_rate_line["rate"] = "-900.00"
        negative_rate_response = self.client.post("/api/v1/purchases/invoices/", {
            "party": str(self.supplier.id),
            "subtotal": "1800.00",
            "taxable_amount": "1800.00",
            "total_amount": "1800.00",
            "paid_amount": "0.00",
            "line_items": [negative_rate_line],
        }, format="json")
        self.assertEqual(negative_rate_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_purchase_invoice_rejects_missing_party_and_non_positive_total(self):
        no_party_response = self.client.post("/api/v1/purchases/invoices/", {
            "subtotal": "1800.00",
            "taxable_amount": "1800.00",
            "total_amount": "1800.00",
            "paid_amount": "0.00",
            "line_items": [self._line_item()],
        }, format="json")
        self.assertEqual(no_party_response.status_code, status.HTTP_400_BAD_REQUEST)

        zero_total_response = self.client.post("/api/v1/purchases/invoices/", {
            "party": str(self.supplier.id),
            "subtotal": "0.00",
            "taxable_amount": "0.00",
            "total_amount": "0.00",
            "paid_amount": "0.00",
            "line_items": [self._line_item()],
        }, format="json")
        self.assertEqual(zero_total_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_purchase_invoice_and_payment_numbers_continue_from_existing_documents(self):
        fy = self._current_fy()
        PurchaseInvoice.objects.create(
            business=self.business,
            invoice_number=f"PUR/{fy}/0040",
            supplier_invoice_number="SUP-OLD",
            party=self.supplier,
            subtotal=Decimal("100.00"),
            taxable_amount=Decimal("100.00"),
            total_amount=Decimal("100.00"),
            paid_amount=Decimal("0.00"),
            status="unpaid",
        )
        PaymentOut.objects.create(
            business=self.business,
            payment_number="PMTOUT-0040",
            party=self.supplier,
            amount_paid=Decimal("100.00"),
            payment_mode="cash",
        )

        response = self.client.post("/api/v1/purchases/invoices/", {
            "party": str(self.supplier.id),
            "supplier_invoice_number": "SUP-101",
            "subtotal": "1800.00",
            "taxable_amount": "1800.00",
            "total_amount": "1800.00",
            "paid_amount": "1800.00",
            "payment_mode": "bank",
            "line_items": [self._line_item()],
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["invoice_number"], f"PUR/{fy}/0041")
        payment = PaymentOut.objects.get(business=self.business, reference_number=response.data["invoice_number"])
        self.assertEqual(payment.payment_number, "PMTOUT-0041")

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

    def test_purchase_order_convert_to_invoice_computes_gst_from_line_items(self):
        # self.item is 0% GST, which can never catch a broken tax
        # calculation (0 x anything is still 0). Use a real GST rate here
        # so a regression is actually detectable.
        taxed_item = Item.objects.create(
            business=self.business,
            name="Taxed Silk Saree",
            item_code="PO-CNV-SILK-002",
            hsn_code="50072010",
            selling_price=1000,
            purchase_price=1000,
            gst_rate=5,
            current_stock=0,
            godown=self.godown,
        )
        order_response = self.client.post("/api/v1/purchases/orders/", {
            "party": str(self.supplier.id),
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
        self.assertEqual(order_response.status_code, status.HTTP_201_CREATED)

        convert_response = self.client.post(
            f"/api/v1/purchases/orders/{order_response.data['id']}/convert_to_invoice/",
            {},
            format="json",
        )
        self.assertEqual(convert_response.status_code, status.HTTP_200_OK)
        invoice = convert_response.data["invoice"]
        self.assertEqual(invoice["taxable_amount"], "1000.00")
        cgst = Decimal(invoice["cgst_amount"])
        sgst = Decimal(invoice["sgst_amount"])
        self.assertEqual(cgst + sgst, Decimal("50.00"))
        self.assertEqual(invoice["total_amount"], "1050.00")

    def test_purchase_order_rejects_missing_party_zero_total_and_bad_line_items(self):
        no_party_response = self.client.post("/api/v1/purchases/orders/", {
            "total_amount": "1800.00",
            "line_items": [self._line_item()],
        }, format="json")
        self.assertEqual(no_party_response.status_code, status.HTTP_400_BAD_REQUEST)

        zero_total_response = self.client.post("/api/v1/purchases/orders/", {
            "party": str(self.supplier.id),
            "total_amount": "0.00",
            "line_items": [self._line_item()],
        }, format="json")
        self.assertEqual(zero_total_response.status_code, status.HTTP_400_BAD_REQUEST)

        zero_qty_line = self._line_item(quantity="0.000")
        zero_qty_response = self.client.post("/api/v1/purchases/orders/", {
            "party": str(self.supplier.id),
            "total_amount": "1800.00",
            "line_items": [zero_qty_line],
        }, format="json")
        self.assertEqual(zero_qty_response.status_code, status.HTTP_400_BAD_REQUEST)

        negative_rate_line = self._line_item()
        negative_rate_line["rate"] = "-900.00"
        negative_rate_response = self.client.post("/api/v1/purchases/orders/", {
            "party": str(self.supplier.id),
            "total_amount": "1800.00",
            "line_items": [negative_rate_line],
        }, format="json")
        self.assertEqual(negative_rate_response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertFalse(PurchaseOrder.objects.filter(business=self.business).exists())

    def test_debit_note_rejects_zero_total_and_non_supplier_party(self):
        zero_total_response = self.client.post("/api/v1/purchases/debit-notes/", {
            "party": str(self.supplier.id),
            "total_amount": "0.00",
            "reason": "Rate difference",
        }, format="json")
        self.assertEqual(zero_total_response.status_code, status.HTTP_400_BAD_REQUEST)

        negative_total_response = self.client.post("/api/v1/purchases/debit-notes/", {
            "party": str(self.supplier.id),
            "total_amount": "-100.00",
            "reason": "Rate difference",
        }, format="json")
        self.assertEqual(negative_total_response.status_code, status.HTTP_400_BAD_REQUEST)

        customer = Party.objects.create(
            business=self.business,
            name="Retail Customer",
            party_type="customer",
        )
        wrong_party_type_response = self.client.post("/api/v1/purchases/debit-notes/", {
            "party": str(customer.id),
            "total_amount": "250.00",
            "reason": "Rate difference",
        }, format="json")
        self.assertEqual(wrong_party_type_response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertFalse(DebitNote.objects.filter(business=self.business).exists())

    def test_purchase_return_is_real_voucher_with_stock_cancel_and_workspace_row(self):
        create_response = self.client.post("/api/v1/purchases/returns/", {
            "party": str(self.supplier.id),
            "reference_number": "SUP-100",
            "total_amount": "900.00",
            "reason": "Damaged saree returned to supplier",
            "line_items": [{
                "item": str(self.item.id),
                "item_name": self.item.name,
                "quantity": "1.000",
                "rate": "900.00",
                "gst_rate": "0.00",
                "taxable_amount": "900.00",
                "amount": "900.00",
                "sort_order": 0,
            }],
        }, format="json")

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(create_response.data["return_number"].startswith("PR/"))

        purchase_return = PurchaseReturn.objects.get(id=create_response.data["id"], business=self.business)
        self.item.refresh_from_db()
        stock = ItemGodownStock.objects.get(business=self.business, item=self.item, godown=self.godown)

        self.assertEqual(purchase_return.status, "adjusted")
        self.assertEqual(str(self.item.current_stock), "9.000")
        self.assertEqual(str(stock.current_stock), "9.000")
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="purchase_return_created",
                entity_id=purchase_return.id,
            ).exists()
        )

        workspace_response = self.client.get("/api/v1/auth/workspace")
        self.assertEqual(workspace_response.status_code, status.HTTP_200_OK)
        return_rows = workspace_response.data["purchaseRows"]["purchase-return"]
        self.assertEqual(return_rows[0]["number"], purchase_return.return_number)
        self.assertEqual(return_rows[0]["linkedVoucher"], "SUP-100")
        self.assertEqual(return_rows[0]["status"], "Adjusted")

        cancel_response = self.client.post(
            f"/api/v1/purchases/returns/{purchase_return.id}/cancel/",
            {"reason": "Supplier rejected return"},
            format="json",
        )

        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)
        purchase_return.refresh_from_db()
        self.item.refresh_from_db()
        stock.refresh_from_db()
        self.assertEqual(purchase_return.status, "cancelled")
        self.assertEqual(str(self.item.current_stock), "10.000")
        self.assertEqual(str(stock.current_stock), "10.000")
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="purchase_return_cancelled",
                entity_id=purchase_return.id,
            ).exists()
        )

    def test_purchase_return_print_pdf_shows_every_line_item_and_tax_total(self):
        # Same bug class as purchase invoices: the old client-side print path
        # only ever showed the first line item and no tax breakdown at all.
        second_item = Item.objects.create(
            business=self.business,
            name="Zari Border Saree",
            item_code="PR-SILK-002",
            selling_price=2500,
            purchase_price=1500,
            gst_rate=5,
            current_stock=Decimal("5.000"),
            godown=self.godown,
        )
        ItemGodownStock.objects.create(
            business=self.business,
            item=second_item,
            godown=self.godown,
            opening_stock=Decimal("5.000"),
            current_stock=Decimal("5.000"),
        )
        line_items = [
            {
                "item": str(self.item.id),
                "item_name": self.item.name,
                "quantity": "1.000",
                "rate": "900.00",
                "gst_rate": "5.00",
                "taxable_amount": "900.00",
                "amount": "945.00",
                "sort_order": 0,
            },
            {
                "item": str(second_item.id),
                "item_name": second_item.name,
                "quantity": "1.000",
                "rate": "1500.00",
                "gst_rate": "5.00",
                "taxable_amount": "1500.00",
                "amount": "1575.00",
                "sort_order": 1,
            },
        ]
        create_response = self.client.post("/api/v1/purchases/returns/", {
            "party": str(self.supplier.id),
            "total_amount": "2520.00",
            "reason": "Colour mismatch",
            "line_items": line_items,
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        return_id = create_response.data["id"]

        print_response = self.client.get(f"/api/v1/purchases/returns/{return_id}/print_pdf/?template=a4")

        self.assertEqual(print_response.status_code, status.HTTP_200_OK)
        html = print_response.content.decode()
        self.assertIn(self.item.name, html)
        self.assertIn(second_item.name, html)
        self.assertIn("Rs. 2,400.00", html)  # taxable value (900 + 1500)
        self.assertIn("Rs. 2,520.00", html)  # total

    def test_purchase_return_rejects_cross_tenant_item_without_stock_change(self):
        other_business = Business.objects.create(name="Other Purchase Return Tenant", phone="9100000801", invoice_prefix="OPR")
        other_item = Item.objects.create(
            business=other_business,
            name="Other Supplier Saree",
            selling_price=1500,
            purchase_price=900,
            gst_rate=0,
            current_stock=Decimal("5.000"),
        )

        response = self.client.post("/api/v1/purchases/returns/", {
            "party": str(self.supplier.id),
            "total_amount": "900.00",
            "line_items": [{
                "item": str(other_item.id),
                "item_name": other_item.name,
                "quantity": "1.000",
                "rate": "900.00",
                "gst_rate": "0.00",
                "taxable_amount": "900.00",
                "amount": "900.00",
            }],
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.item.refresh_from_db()
        other_item.refresh_from_db()
        self.assertEqual(str(self.item.current_stock), "10.000")
        self.assertEqual(str(other_item.current_stock), "5.000")
        self.assertEqual(PurchaseReturn.objects.filter(business=self.business).count(), 0)
