from decimal import Decimal
from html import escape
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from .models import PurchaseInvoice, PurchaseOrder, PurchaseReturn, DebitNote
from .serializers import (
    PurchaseInvoiceSerializer, PurchaseOrderSerializer, PurchaseReturnSerializer,
    DebitNoteSerializer
)
from apps.accounts.activity import write_activity
from apps.items.models import Item, apply_stock_movement
from apps.payments.models import PaymentOutSettlement
from apps.sales.views import _money, _date, _business_address, _bank_lines, _invoice_print_settings


def _write_purchase_activity(request, action, entity_type, entity_id, details):
    write_activity(
        business=request.business,
        user=request.user,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
    )


def _voucher_details(voucher, number_attr, **extra):
    details = {
        "number": getattr(voucher, number_attr),
        "party": voucher.party.name if getattr(voucher, "party", None) else "",
        "status": getattr(voucher, "status", ""),
        "totalAmount": getattr(voucher, "total_amount", None),
    }
    details.update(extra)
    return details


def _render_purchase_invoice_print_html(invoice, template="a4"):
    business = invoice.business
    party = invoice.party
    _, business_preferences = _invoice_print_settings(business)
    lines = list(invoice.line_items.all().order_by("sort_order"))
    balance = invoice.total_amount - invoice.paid_amount
    template = "thermal" if template == "thermal" else "a4"
    bank_lines = _bank_lines(business.bank_account_details)
    terms = business.terms_conditions or "Thank you for your business."
    bank_html = "".join(f"<p><strong>{escape(label)}:</strong> {escape(value)}</p>" for label, value in bank_lines)

    rows = []
    for index, line in enumerate(lines, start=1):
        rows.append(f"""
          <tr>
            <td class="right">{index}</td>
            <td class="left"><strong>{escape(line.item_name)}</strong></td>
            <td class="right">{line.quantity:g}</td>
            <td class="right">{_money(line.rate)}</td>
            <td class="right">{line.discount_pct:g}%</td>
            <td class="right">{_money(line.taxable_amount)}</td>
            <td class="right">{line.gst_rate:g}%</td>
            <td class="right">{_money(line.amount)}</td>
          </tr>
        """)

    thermal_rows = []
    for line in lines:
        thermal_rows.append(f"""
          <div class="thermal-line">
            <span>{escape(line.item_name)}</span>
            <b>{line.quantity:g} x {_money(line.rate)}</b>
            <strong>{_money(line.amount)}</strong>
          </div>
        """)

    if template == "thermal":
        body = f"""
          <main class="thermal-receipt">
            <header>
              <h1>{escape(business.name or "Business")}</h1>
              <p>{escape(_business_address(business) or business.phone or "")}</p>
              <strong>PURCHASE VOUCHER</strong>
            </header>
            <section class="thermal-meta">
              <span>Invoice</span><b>{escape(invoice.invoice_number)}</b>
              <span>Date</span><b>{_date(invoice.invoice_date)}</b>
              <span>Supplier</span><b>{escape(party.name)}</b>
            </section>
            <section class="thermal-items">
              {''.join(thermal_rows)}
            </section>
            <section class="thermal-totals">
              <span>Subtotal</span><b>{_money(invoice.subtotal)}</b>
              {'<span>Discount</span><b>- ' + _money(invoice.discount_amount) + '</b>' if invoice.discount_amount else ''}
              <span>CGST</span><b>{_money(invoice.cgst_amount)}</b>
              <span>SGST</span><b>{_money(invoice.sgst_amount)}</b>
              {'<span>IGST</span><b>' + _money(invoice.igst_amount) + '</b>' if invoice.igst_amount else ''}
              <span>Total</span><strong>{_money(invoice.total_amount)}</strong>
              <span>Paid</span><b>{_money(invoice.paid_amount)}</b>
              <span>Balance</span><strong>{_money(balance)}</strong>
            </section>
            <footer>{escape(terms)}</footer>
          </main>
        """
    else:
        body = f"""
          <main class="a4-sheet">
            <header class="invoice-head">
              <div>
                <strong>PURCHASE VOUCHER</strong>
                <span>{escape(invoice.get_status_display())}</span>
              </div>
              <b>{escape(business.name or "Business")}</b>
            </header>
            <section class="invoice-grid">
              <div class="business-block">
                <h1>{escape(business.name or "Business")}</h1>
                <p>{escape(_business_address(business) or "-")}</p>
                <p><strong>GSTIN:</strong> {escape(business.gstin or "-")}</p>
                <p><strong>Mobile:</strong> {escape(business.phone or "-")}</p>
                {bank_html}
              </div>
              <div class="meta-block">
                <p><strong>Purchase Invoice No.</strong><span>{escape(invoice.invoice_number)}</span></p>
                <p><strong>Invoice Date</strong><span>{_date(invoice.invoice_date)}</span></p>
                {'<p><strong>Supplier Invoice No.</strong><span>' + escape(invoice.supplier_invoice_number) + '</span></p>' if invoice.supplier_invoice_number else ''}
              </div>
              <div class="party-block">
                <strong>SUPPLIER</strong>
                <b>{escape(party.name)}</b>
                {'<span>GSTIN: ' + escape(party.gstin) + '</span>' if getattr(party, "gstin", None) else ''}
              </div>
            </section>
            <table class="line-table">
              <thead>
                <tr>
                  <th>S.NO.</th><th>ITEMS</th><th>QTY</th><th>RATE</th><th>DISC.</th><th>TAXABLE</th><th>GST</th><th>AMOUNT</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows)}
              </tbody>
            </table>
            <section class="totals-grid">
              <div>
                <strong>Notes</strong>
                <span>{escape(invoice.notes or "-")}</span>
              </div>
              <table>
                <tbody>
                  <tr><td>Subtotal</td><td>{_money(invoice.subtotal)}</td></tr>
                  <tr><td>Discount</td><td>- {_money(invoice.discount_amount)}</td></tr>
                  <tr><td>CGST</td><td>{_money(invoice.cgst_amount)}</td></tr>
                  <tr><td>SGST</td><td>{_money(invoice.sgst_amount)}</td></tr>
                  <tr><td>IGST</td><td>{_money(invoice.igst_amount)}</td></tr>
                  <tr class="grand"><td>Total</td><td>{_money(invoice.total_amount)}</td></tr>
                  <tr><td>Paid</td><td>{_money(invoice.paid_amount)}</td></tr>
                  <tr class="balance"><td>Balance</td><td>{_money(balance)}</td></tr>
                </tbody>
              </table>
            </section>
          </main>
        """

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{escape(invoice.invoice_number)} Purchase Voucher</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #eef1f5; color: #111827; font-family: Arial, Helvetica, sans-serif; }}
    .toolbar {{ position: sticky; top: 0; z-index: 2; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 18px; border-bottom: 1px solid #d6dce7; background: #fff; }}
    .toolbar div {{ display: flex; align-items: center; gap: 8px; }}
    .toolbar strong {{ font-size: 15px; }}
    .toolbar button {{ height: 36px; border: 1px solid #cbd5e1; border-radius: 6px; background: #fff; color: #1f2937; padding: 0 12px; font-weight: 700; cursor: pointer; }}
    .toolbar button.primary {{ border-color: #5b44d8; background: #5b44d8; color: #fff; }}
    .a4-sheet {{ width: 210mm; min-height: 297mm; margin: 18px auto; padding: 12mm; background: #fff; box-shadow: 0 18px 50px rgba(15, 23, 42, 0.18); font-size: 12px; }}
    .invoice-head {{ display: flex; justify-content: space-between; align-items: center; padding-bottom: 8px; }}
    .invoice-head div {{ display: flex; align-items: center; gap: 8px; }}
    .invoice-head strong {{ font-size: 18px; }}
    .invoice-head span {{ border: 1px solid #8c95aa; color: #566174; padding: 4px 7px; font-weight: 800; }}
    .invoice-grid {{ display: grid; grid-template-columns: 1fr 1fr; border: 1px solid #111; }}
    .invoice-grid > div {{ min-height: 34mm; border-right: 1px solid #111; border-bottom: 1px solid #111; padding: 8px; }}
    .invoice-grid > div:nth-child(2n) {{ border-right: 0; }}
    h1 {{ margin: 0 0 6px; color: #5B48F5; font-size: 20px; }}
    p {{ margin: 3px 0; line-height: 1.4; }}
    .meta-block {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; text-align: center; align-content: center; }}
    .meta-block p {{ display: grid; gap: 6px; }}
    .party-block {{ display: grid; align-content: start; gap: 6px; }}
    .line-table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
    .line-table th, .line-table td {{ border: 1px solid #111; padding: 6px; text-align: right; vertical-align: top; }}
    .line-table th {{ background: color-mix(in srgb, #5B48F5 14%, white); font-weight: 800; }}
    .line-table th:nth-child(2), .line-table td:nth-child(2) {{ text-align: left; }}
    .line-table td.left {{ text-align: left; }}
    .line-table td.right {{ text-align: right; }}
    .totals-grid {{ display: grid; grid-template-columns: 1fr 72mm; gap: 12px; margin-top: 10px; }}
    .totals-grid > div {{ border: 1px solid #111; padding: 8px; display: grid; gap: 7px; align-content: start; }}
    .totals-grid table {{ width: 100%; border-collapse: collapse; }}
    .totals-grid td {{ border: 1px solid #111; padding: 7px; }}
    .totals-grid td:last-child {{ text-align: right; font-weight: 800; }}
    .grand td {{ background: color-mix(in srgb, #5B48F5 14%, white); font-size: 14px; }}
    .balance td {{ color: #d91f2a; }}
    .thermal-receipt {{ width: 58mm; min-height: 120mm; margin: 18px auto; padding: 4mm; background: #fff; box-shadow: 0 18px 50px rgba(15, 23, 42, 0.18); font-size: 10px; }}
    .thermal-receipt header {{ text-align: center; border-bottom: 1px dashed #111; padding-bottom: 6px; }}
    .thermal-receipt h1 {{ color: #111; font-size: 14px; }}
    .thermal-meta, .thermal-totals {{ display: grid; grid-template-columns: 1fr auto; gap: 4px 8px; padding: 7px 0; border-bottom: 1px dashed #111; }}
    .thermal-line {{ display: grid; gap: 3px; padding: 6px 0; border-bottom: 1px dotted #bbb; }}
    .thermal-line strong, .thermal-totals strong {{ font-size: 12px; text-align: right; }}
    .thermal-receipt footer {{ text-align: center; padding-top: 8px; }}
    @page {{ size: {"58mm auto" if template == "thermal" else "A4"}; margin: {"2mm" if template == "thermal" else "8mm"}; }}
    @media print {{
      body {{ background: #fff; }}
      .toolbar {{ display: none; }}
      .a4-sheet, .thermal-receipt {{ margin: 0; box-shadow: none; }}
    }}
  </style>
</head>
<body>
  <div class="toolbar">
    <strong>{escape(invoice.invoice_number)} - {"Thermal" if template == "thermal" else "A4"} preview</strong>
    <div>
      <button onclick="window.close()">Close</button>
      <button class="primary" onclick="window.print()">Print</button>
    </div>
  </div>
  {body}
</body>
</html>"""


class LifecycleDeleteBlockedMixin:
    def destroy(self, request, *args, **kwargs):
        return Response(
            {"success": False, "message": "Use the cancel/void action so stock and ledger entries are reversed safely."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )


class PurchaseInvoiceViewSet(LifecycleDeleteBlockedMixin, viewsets.ModelViewSet):
    serializer_class = PurchaseInvoiceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return PurchaseInvoice.objects.none()
        
        queryset = PurchaseInvoice.objects.filter(business=self.request.business)
        
        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)
            
        party = self.request.query_params.get("party")
        if party:
            queryset = queryset.filter(party_id=party)
            
        return queryset.order_by("-created_at")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Cancels purchase invoice, rolls back stock, and voids invoice-created payments."""
        invoice_ref = self.get_object()
            
        with transaction.atomic():
            invoice = (
                PurchaseInvoice.objects.select_for_update()
                .prefetch_related("line_items")
                .get(id=invoice_ref.id, business=request.business)
            )
            if invoice.status == "cancelled":
                return Response(
                    {"success": False, "message": "Invoice is already cancelled"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            invoice.status = "cancelled"
            invoice.paid_amount = 0
            invoice.save(update_fields=["status", "paid_amount", "updated_at"])
            
            # Deduct the added stock back out
            for line_item in invoice.line_items.all():
                if line_item.item:
                    actual_item = Item.objects.select_for_update().get(id=line_item.item.id, business=request.business)
                    deducted_qty = line_item.quantity
                    
                    apply_stock_movement(
                        business=request.business,
                        item=actual_item,
                        godown=actual_item.godown,
                        movement_type="purchase_return",
                        reference_type="purchase_invoice_cancel",
                        reference_id=invoice.id,
                        quantity=-deducted_qty,
                        rate=line_item.rate,
                        created_by=request.user,
                        notes=f"Deducted via Purchase Cancellation {invoice.invoice_number}",
                        allow_negative=False,
                    )

            for settlement in PaymentOutSettlement.objects.select_related("payment_out").filter(
                invoice=invoice,
                payment_out__business=request.business,
                payment_out__status="active",
            ):
                payment = settlement.payment_out
                has_other_allocations = payment.settlements.exclude(invoice=invoice).exists()
                if has_other_allocations:
                    settlement.delete()
                    continue

                payment.status = "void"
                payment.cancellation_reason = f"Purchase invoice {invoice.invoice_number} cancelled"
                payment.cancelled_at = timezone.now()
                payment.cancelled_by = request.user
                payment.save(update_fields=[
                    "status",
                    "cancellation_reason",
                    "cancelled_at",
                    "cancelled_by",
                ])

            _write_purchase_activity(
                request,
                "purchase_invoice_cancelled",
                "purchase_invoice",
                invoice.id,
                _voucher_details(invoice, "invoice_number", stockReversed=True, linkedPaymentsReversed=True),
            )
                    
        return Response({
            "success": True,
            "message": "Purchase invoice cancelled and inventory stock adjusted successfully"
        })

    @action(detail=True, methods=["get"])
    def print_pdf(self, request, pk=None):
        """Returns a print-ready purchase voucher HTML template for browser PDF or thermal print."""
        invoice = self.get_object()
        template = request.query_params.get("template") or request.query_params.get("format") or "a4"
        return HttpResponse(_render_purchase_invoice_print_html(invoice, template), content_type="text/html")

class PurchaseOrderViewSet(LifecycleDeleteBlockedMixin, viewsets.ModelViewSet):
    serializer_class = PurchaseOrderSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return PurchaseOrder.objects.none()
        return PurchaseOrder.objects.filter(business=self.request.business).order_by("-created_at")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        order = self.get_object()
        if order.status == "cancelled":
            return Response({"success": False, "message": "Purchase order is already cancelled"}, status=status.HTTP_400_BAD_REQUEST)
        if order.status == "converted":
            return Response({"success": False, "message": "Converted purchase orders cannot be cancelled"}, status=status.HTTP_400_BAD_REQUEST)
        order.status = "cancelled"
        order.save(update_fields=["status"])
        _write_purchase_activity(
            request,
            "purchase_order_cancelled",
            "purchase_order",
            order.id,
            _voucher_details(order, "order_number"),
        )
        return Response({"success": True, "message": "Purchase order cancelled successfully"})

    @action(detail=True, methods=["post"])
    def convert_to_invoice(self, request, pk=None):
        """Converts an open purchase order into a Purchase Invoice."""
        order = self.get_object()
        if order.status == "converted":
            return Response({"success": False, "message": "Order already converted to an invoice"})
        if order.status == "cancelled":
            return Response({"success": False, "message": "Cancelled purchase orders cannot be converted"}, status=status.HTTP_400_BAD_REQUEST)
            
        with transaction.atomic():
            line_payloads = []
            total_taxable = Decimal("0")
            total_tax = Decimal("0")
            for line_item in order.line_items.all():
                quantity = Decimal(str(line_item.quantity))
                rate = Decimal(str(line_item.rate))
                gst_rate = Decimal(str(line_item.gst_rate or 0))
                taxable = (quantity * rate).quantize(Decimal("0.01"))
                tax = (taxable * gst_rate / Decimal("100")).quantize(Decimal("0.01"))
                total_taxable += taxable
                total_tax += tax
                line_payloads.append({
                    "item": line_item.item.id if line_item.item else None,
                    "item_name": line_item.item_name,
                    "quantity": float(quantity),
                    "rate": float(rate),
                    "gst_rate": float(gst_rate),
                    "taxable_amount": float(taxable),
                    "amount": float(taxable + tax)
                })

            # Round CGST then derive SGST by subtraction so the two always
            # sum to exactly total_tax (see the identical fix in api/sales.ts
            # and apps/sales/views.py's register-to-invoice conversion).
            cgst_amount = (total_tax / 2).quantize(Decimal("0.01"))
            sgst_amount = total_tax - cgst_amount

            invoice_data = {
                "party": order.party.id,
                "subtotal": float(total_taxable),
                "taxable_amount": float(total_taxable),
                "cgst_amount": float(cgst_amount),
                "sgst_amount": float(sgst_amount),
                "total_amount": float(total_taxable + total_tax),
                "notes": f"Converted from Purchase Order No: {order.order_number}",
                "line_items": line_payloads
            }

            serializer = PurchaseInvoiceSerializer(data=invoice_data, context={"request": request})
            serializer.is_valid(raise_exception=True)
            invoice = serializer.save()
            
            order.status = "converted"
            order.converted_invoice = invoice
            order.save()
            _write_purchase_activity(
                request,
                "purchase_order_converted_to_invoice",
                "purchase_order",
                order.id,
                _voucher_details(
                    order,
                    "order_number",
                    convertedInvoiceId=invoice.id,
                    convertedInvoiceNumber=invoice.invoice_number,
                ),
            )
            
        return Response({
            "success": True,
            "message": "Purchase order successfully converted to purchase invoice",
            "invoice": PurchaseInvoiceSerializer(invoice).data
        })


def _render_purchase_return_print_html(purchase_return, template="a4"):
    business = purchase_return.business
    party = purchase_return.party
    lines = list(purchase_return.line_items.all().order_by("sort_order"))
    subtotal = sum((line.taxable_amount for line in lines), Decimal("0.00"))
    tax_total = purchase_return.total_amount - subtotal
    template = "thermal" if template == "thermal" else "a4"
    bank_lines = _bank_lines(business.bank_account_details)
    bank_html = "".join(f"<p><strong>{escape(label)}:</strong> {escape(value)}</p>" for label, value in bank_lines)
    status_label = dict(purchase_return.STATUS_CHOICES).get(purchase_return.status, purchase_return.status)

    rows = []
    for index, line in enumerate(lines, start=1):
        rows.append(f"""
          <tr>
            <td class="right">{index}</td>
            <td class="left"><strong>{escape(line.item_name)}</strong></td>
            <td class="right">{line.quantity:g}</td>
            <td class="right">{_money(line.rate)}</td>
            <td class="right">{_money(line.taxable_amount)}</td>
            <td class="right">{line.gst_rate:g}%</td>
            <td class="right">{_money(line.amount)}</td>
          </tr>
        """)

    thermal_rows = []
    for line in lines:
        thermal_rows.append(f"""
          <div class="thermal-line">
            <span>{escape(line.item_name)}</span>
            <b>{line.quantity:g} x {_money(line.rate)}</b>
            <strong>{_money(line.amount)}</strong>
          </div>
        """)

    if template == "thermal":
        body = f"""
          <main class="thermal-receipt">
            <header>
              <h1>{escape(business.name or "Business")}</h1>
              <p>{escape(_business_address(business) or business.phone or "")}</p>
              <strong>PURCHASE RETURN</strong>
            </header>
            <section class="thermal-meta">
              <span>Return No.</span><b>{escape(purchase_return.return_number)}</b>
              <span>Date</span><b>{_date(purchase_return.return_date)}</b>
              <span>Supplier</span><b>{escape(party.name)}</b>
            </section>
            <section class="thermal-items">
              {''.join(thermal_rows)}
            </section>
            <section class="thermal-totals">
              <span>Taxable Value</span><b>{_money(subtotal)}</b>
              <span>Tax Amount</span><b>{_money(tax_total)}</b>
              <span>Total</span><strong>{_money(purchase_return.total_amount)}</strong>
            </section>
            <footer>{escape(purchase_return.reason or "")}</footer>
          </main>
        """
    else:
        body = f"""
          <main class="a4-sheet">
            <header class="invoice-head">
              <div>
                <strong>PURCHASE RETURN</strong>
                <span>{escape(status_label)}</span>
              </div>
              <b>{escape(business.name or "Business")}</b>
            </header>
            <section class="invoice-grid">
              <div class="business-block">
                <h1>{escape(business.name or "Business")}</h1>
                <p>{escape(_business_address(business) or "-")}</p>
                <p><strong>GSTIN:</strong> {escape(business.gstin or "-")}</p>
                <p><strong>Mobile:</strong> {escape(business.phone or "-")}</p>
                {bank_html}
              </div>
              <div class="meta-block">
                <p><strong>Return No.</strong><span>{escape(purchase_return.return_number)}</span></p>
                <p><strong>Return Date</strong><span>{_date(purchase_return.return_date)}</span></p>
                {'<p><strong>Reference No.</strong><span>' + escape(purchase_return.reference_number) + '</span></p>' if purchase_return.reference_number else ''}
                {'<p><strong>Against Invoice</strong><span>' + escape(purchase_return.original_invoice.invoice_number) + '</span></p>' if purchase_return.original_invoice else ''}
              </div>
              <div class="party-block">
                <strong>SUPPLIER</strong>
                <b>{escape(party.name)}</b>
                {'<span>GSTIN: ' + escape(party.gstin) + '</span>' if getattr(party, "gstin", None) else ''}
              </div>
            </section>
            <table class="line-table">
              <thead>
                <tr>
                  <th>S.NO.</th><th>ITEMS</th><th>QTY</th><th>RATE</th><th>TAXABLE</th><th>GST</th><th>AMOUNT</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows)}
              </tbody>
            </table>
            <section class="totals-grid">
              <div>
                <strong>Reason</strong>
                <span>{escape(purchase_return.reason or "-")}</span>
              </div>
              <table>
                <tbody>
                  <tr><td>Taxable Value</td><td>{_money(subtotal)}</td></tr>
                  <tr><td>Tax Amount</td><td>{_money(tax_total)}</td></tr>
                  <tr class="grand"><td>Total</td><td>{_money(purchase_return.total_amount)}</td></tr>
                </tbody>
              </table>
            </section>
          </main>
        """

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{escape(purchase_return.return_number)} Purchase Return</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #eef1f5; color: #111827; font-family: Arial, Helvetica, sans-serif; }}
    .toolbar {{ position: sticky; top: 0; z-index: 2; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 18px; border-bottom: 1px solid #d6dce7; background: #fff; }}
    .toolbar div {{ display: flex; align-items: center; gap: 8px; }}
    .toolbar strong {{ font-size: 15px; }}
    .toolbar button {{ height: 36px; border: 1px solid #cbd5e1; border-radius: 6px; background: #fff; color: #1f2937; padding: 0 12px; font-weight: 700; cursor: pointer; }}
    .toolbar button.primary {{ border-color: #5b44d8; background: #5b44d8; color: #fff; }}
    .a4-sheet {{ width: 210mm; min-height: 297mm; margin: 18px auto; padding: 12mm; background: #fff; box-shadow: 0 18px 50px rgba(15, 23, 42, 0.18); font-size: 12px; }}
    .invoice-head {{ display: flex; justify-content: space-between; align-items: center; padding-bottom: 8px; }}
    .invoice-head div {{ display: flex; align-items: center; gap: 8px; }}
    .invoice-head strong {{ font-size: 18px; }}
    .invoice-head span {{ border: 1px solid #8c95aa; color: #566174; padding: 4px 7px; font-weight: 800; }}
    .invoice-grid {{ display: grid; grid-template-columns: 1fr 1fr; border: 1px solid #111; }}
    .invoice-grid > div {{ min-height: 34mm; border-right: 1px solid #111; border-bottom: 1px solid #111; padding: 8px; }}
    .invoice-grid > div:nth-child(2n) {{ border-right: 0; }}
    h1 {{ margin: 0 0 6px; color: #5B48F5; font-size: 20px; }}
    p {{ margin: 3px 0; line-height: 1.4; }}
    .meta-block {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; text-align: center; align-content: center; }}
    .meta-block p {{ display: grid; gap: 6px; }}
    .party-block {{ display: grid; align-content: start; gap: 6px; }}
    .line-table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
    .line-table th, .line-table td {{ border: 1px solid #111; padding: 6px; text-align: right; vertical-align: top; }}
    .line-table th {{ background: color-mix(in srgb, #5B48F5 14%, white); font-weight: 800; }}
    .line-table th:nth-child(2), .line-table td:nth-child(2) {{ text-align: left; }}
    .line-table td.left {{ text-align: left; }}
    .line-table td.right {{ text-align: right; }}
    .totals-grid {{ display: grid; grid-template-columns: 1fr 72mm; gap: 12px; margin-top: 10px; }}
    .totals-grid > div {{ border: 1px solid #111; padding: 8px; display: grid; gap: 7px; align-content: start; }}
    .totals-grid table {{ width: 100%; border-collapse: collapse; }}
    .totals-grid td {{ border: 1px solid #111; padding: 7px; }}
    .totals-grid td:last-child {{ text-align: right; font-weight: 800; }}
    .grand td {{ background: color-mix(in srgb, #5B48F5 14%, white); font-size: 14px; }}
    .thermal-receipt {{ width: 58mm; min-height: 120mm; margin: 18px auto; padding: 4mm; background: #fff; box-shadow: 0 18px 50px rgba(15, 23, 42, 0.18); font-size: 10px; }}
    .thermal-receipt header {{ text-align: center; border-bottom: 1px dashed #111; padding-bottom: 6px; }}
    .thermal-receipt h1 {{ color: #111; font-size: 14px; }}
    .thermal-meta, .thermal-totals {{ display: grid; grid-template-columns: 1fr auto; gap: 4px 8px; padding: 7px 0; border-bottom: 1px dashed #111; }}
    .thermal-line {{ display: grid; gap: 3px; padding: 6px 0; border-bottom: 1px dotted #bbb; }}
    .thermal-line strong, .thermal-totals strong {{ font-size: 12px; text-align: right; }}
    .thermal-receipt footer {{ text-align: center; padding-top: 8px; }}
    @page {{ size: {"58mm auto" if template == "thermal" else "A4"}; margin: {"2mm" if template == "thermal" else "8mm"}; }}
    @media print {{
      body {{ background: #fff; }}
      .toolbar {{ display: none; }}
      .a4-sheet, .thermal-receipt {{ margin: 0; box-shadow: none; }}
    }}
  </style>
</head>
<body>
  <div class="toolbar">
    <strong>{escape(purchase_return.return_number)} - {"Thermal" if template == "thermal" else "A4"} preview</strong>
    <div>
      <button onclick="window.close()">Close</button>
      <button class="primary" onclick="window.print()">Print</button>
    </div>
  </div>
  {body}
</body>
</html>"""


class PurchaseReturnViewSet(LifecycleDeleteBlockedMixin, viewsets.ModelViewSet):
    serializer_class = PurchaseReturnSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return PurchaseReturn.objects.none()

        queryset = PurchaseReturn.objects.filter(business=self.request.business)
        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)
        party = self.request.query_params.get("party")
        if party:
            queryset = queryset.filter(party_id=party)
        return queryset.select_related("party", "original_invoice").prefetch_related("line_items").order_by("-return_date", "-created_at")

    def perform_create(self, serializer):
        purchase_return = serializer.save()
        _write_purchase_activity(
            self.request,
            "purchase_return_created",
            "purchase_return",
            purchase_return.id,
            _voucher_details(purchase_return, "return_number", stockAdjusted=True),
        )

    def perform_update(self, serializer):
        purchase_return = serializer.save()
        _write_purchase_activity(
            self.request,
            "purchase_return_updated",
            "purchase_return",
            purchase_return.id,
            _voucher_details(purchase_return, "return_number", stockReapplied=True),
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        purchase_return_ref = self.get_object()

        with transaction.atomic():
            purchase_return = (
                PurchaseReturn.objects.select_for_update()
                .prefetch_related("line_items")
                .get(id=purchase_return_ref.id, business=request.business)
            )
            if purchase_return.status == "cancelled":
                return Response(
                    {"success": False, "message": "Purchase return is already cancelled"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            for line_item in purchase_return.line_items.all():
                if not line_item.item:
                    continue
                actual_item = Item.objects.select_for_update().get(id=line_item.item.id, business=request.business)
                apply_stock_movement(
                    business=request.business,
                    item=actual_item,
                    godown=actual_item.godown,
                    movement_type="purchase",
                    reference_type="purchase_return_cancel",
                    reference_id=purchase_return.id,
                    quantity=line_item.quantity,
                    rate=line_item.rate,
                    created_by=request.user,
                    notes=f"Restored via Purchase Return Cancellation {purchase_return.return_number}",
                )

            purchase_return.status = "cancelled"
            purchase_return.reason = request.data.get("reason") or purchase_return.reason
            purchase_return.save(update_fields=["status", "reason", "updated_at"])
            _write_purchase_activity(
                request,
                "purchase_return_cancelled",
                "purchase_return",
                purchase_return.id,
                _voucher_details(purchase_return, "return_number", stockRestored=True),
            )

        return Response({
            "success": True,
            "message": "Purchase return cancelled and stock restored successfully",
            "purchase_return": PurchaseReturnSerializer(purchase_return, context={"request": request}).data,
        })

    @action(detail=True, methods=["get"])
    def print_pdf(self, request, pk=None):
        """Returns a print-ready purchase return HTML template for browser PDF or thermal print."""
        purchase_return = self.get_object()
        template = request.query_params.get("template") or request.query_params.get("format") or "a4"
        return HttpResponse(_render_purchase_return_print_html(purchase_return, template), content_type="text/html")


class DebitNoteViewSet(LifecycleDeleteBlockedMixin, viewsets.ModelViewSet):
    serializer_class = DebitNoteSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return DebitNote.objects.none()
        return DebitNote.objects.filter(business=self.request.business).order_by("-created_at")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        debit_note = self.get_object()
        if debit_note.status == "cancelled":
            return Response({"success": False, "message": "Debit note is already cancelled"}, status=status.HTTP_400_BAD_REQUEST)
        debit_note.status = "cancelled"
        debit_note.save(update_fields=["status"])
        _write_purchase_activity(
            request,
            "debit_note_cancelled",
            "debit_note",
            debit_note.id,
            _voucher_details(debit_note, "debit_note_number"),
        )
        return Response({"success": True, "message": "Debit note cancelled successfully"})
