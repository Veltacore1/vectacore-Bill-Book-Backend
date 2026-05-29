import uuid
import json
import urllib.error
import urllib.request
from io import BytesIO
from html import escape
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .models import (
    SalesInvoice, EInvoiceLog, Quotation, DeliveryChallan, CreditNote,
    ProformaInvoice, SalesReturn
)
from .serializers import (
    SalesInvoiceSerializer, EInvoiceLogSerializer, QuotationSerializer, 
    DeliveryChallanSerializer, CreditNoteSerializer,
    ProformaInvoiceSerializer, SalesReturnSerializer
)
from apps.items.models import Item, apply_stock_movement
from apps.payments.models import PaymentInSettlement
from apps.business_settings.models import BusinessPreference, InvoiceSettings
from django.template.loader import render_to_string
from django.http import HttpResponse
from django.shortcuts import get_object_or_404

from apps.accounts.activity import write_activity


LOCAL_EINVOICE_PROVIDERS = {"local_stub", "demo", "stub"}


class ProviderConfigurationError(Exception):
    pass


class ProviderDeliveryError(Exception):
    pass


def _voucher_type(label):
    return label.lower().replace(" ", "_").replace("-", "_")


def _write_sales_activity(request, action, entity_type, entity_id, details):
    write_activity(
        business=request.business,
        user=request.user,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
    )


def _voucher_activity_details(voucher, number_attr, **extra):
    party = getattr(voucher, "party", None)
    details = {
        "number": getattr(voucher, number_attr),
        "party": party.name if party else "",
        "status": getattr(voucher, "status", ""),
        "totalAmount": getattr(voucher, "total_amount", None),
    }
    details.update(extra)
    return details


def _einvoice_provider_name():
    return (getattr(settings, "E_INVOICE_PROVIDER", "disabled") or "disabled").strip().lower()


def _post_provider_json(path, payload):
    provider = _einvoice_provider_name()
    base_url = getattr(settings, "E_INVOICE_API_URL", "").rstrip("/")
    token = getattr(settings, "E_INVOICE_API_TOKEN", "")
    if provider in {"disabled", ""}:
        raise ProviderConfigurationError("E-Invoice provider is not configured.")
    if provider in LOCAL_EINVOICE_PROVIDERS:
        raise ProviderConfigurationError("Local stub provider does not call external e-invoice APIs.")
    if not base_url or not token:
        raise ProviderConfigurationError("E-Invoice API URL and token are required for production provider mode.")

    request = urllib.request.Request(
        f"{base_url}/{path.lstrip('/')}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise ProviderDeliveryError(details or f"E-Invoice provider returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise ProviderDeliveryError(f"E-Invoice provider is unreachable: {error.reason}") from error

    try:
        return json.loads(body) if body else {}
    except json.JSONDecodeError as error:
        raise ProviderDeliveryError("E-Invoice provider returned invalid JSON.") from error


def _invoice_line_from_register_line(line_item):
    amount = float(line_item.amount)
    return {
        "item": line_item.item.id if line_item.item else None,
        "item_name": line_item.item_name,
        "item_code": line_item.item.item_code if line_item.item else "",
        "hsn_code": line_item.item.hsn_code if line_item.item else "",
        "unit": line_item.item.unit if line_item.item else "PCS",
        "quantity": float(line_item.quantity),
        "free_quantity": 0,
        "mrp": float(line_item.item.mrp or line_item.rate) if line_item.item else float(line_item.rate),
        "rate": float(line_item.rate),
        "discount_pct": 0,
        "discount_amount": 0,
        "gst_rate": float(line_item.gst_rate),
        "taxable_amount": amount,
        "tax_amount": 0,
        "amount": amount,
    }


def _converted_invoice_payload(invoice, request):
    return SalesInvoiceSerializer(invoice, context={"request": request}).data if invoice else None


def _convert_sales_register_voucher_to_invoice(
    *,
    viewset,
    request,
    pk,
    source_label,
    number_attr,
    subtotal_attr=None,
    terms_attr=None,
):
    with transaction.atomic():
        queryset = (
            viewset.get_queryset()
            .select_for_update(of=("self",))
            .select_related("party")
            .prefetch_related("line_items__item")
        )
        source = get_object_or_404(queryset, pk=pk)
        source_number = getattr(source, number_attr)

        if source.status == "converted" or source.converted_invoice_id:
            invoice = source.converted_invoice
            invoice_number = invoice.invoice_number if invoice else "the linked invoice"
            return Response(
                {
                    "success": False,
                    "message": f"{source_label} {source_number} is already converted to sales invoice {invoice_number}.",
                    "invoice": _converted_invoice_payload(invoice, request),
                },
                status=status.HTTP_409_CONFLICT,
            )

        if source.status == "cancelled":
            return Response(
                {"success": False, "message": f"Cancelled {source_label.lower()}s cannot be converted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        line_items = list(source.line_items.all())
        if not line_items:
            return Response(
                {"success": False, "message": f"Add at least one item before converting {source_label.lower()} {source_number}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        invoice_data = {
            "party": str(source.party_id),
            "subtotal": float(getattr(source, subtotal_attr or "total_amount", source.total_amount) or 0),
            "taxable_amount": float(getattr(source, subtotal_attr or "total_amount", source.total_amount) or 0),
            "total_amount": float(source.total_amount),
            "paid_amount": 0,
            "notes": f"Converted from {source_label} No: {source_number}",
            "line_items": [_invoice_line_from_register_line(line_item) for line_item in line_items],
        }
        if terms_attr and hasattr(source, terms_attr):
            invoice_data["terms"] = getattr(source, terms_attr) or ""

        serializer = SalesInvoiceSerializer(data=invoice_data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        invoice = serializer.save()

        source.status = "converted"
        source.converted_invoice = invoice
        source.save(update_fields=["status", "converted_invoice"])
        source_type = _voucher_type(source_label)
        _write_sales_activity(
            request,
            f"{source_type}_converted_to_sales_invoice",
            source_type,
            source.id,
            _voucher_activity_details(
                source,
                number_attr,
                convertedInvoiceId=invoice.id,
                convertedInvoiceNumber=invoice.invoice_number,
            ),
        )

    return Response(
        {
            "success": True,
            "message": f"{source_label} {source_number} successfully converted to sales invoice {invoice.invoice_number}.",
            "invoice": _converted_invoice_payload(invoice, request),
        },
        status=status.HTTP_201_CREATED,
    )

class LifecycleDeleteBlockedMixin:
    def destroy(self, request, *args, **kwargs):
        return Response(
            {"success": False, "message": "Use the cancel/void action so stock and ledger entries are reversed safely."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )


def _einvoice_record(invoice):
    latest_logs = invoice.einvoice_logs.all()[:5]
    return {
        "id": str(invoice.id),
        "invoice_number": invoice.invoice_number,
        "party_name": invoice.party.name,
        "party_gstin": invoice.party.gstin or "",
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else "",
        "total_amount": float(invoice.total_amount),
        "invoice_status": invoice.status,
        "irn": invoice.irn or "",
        "ack_number": invoice.ack_number or "",
        "ack_date": invoice.ack_date.isoformat() if invoice.ack_date else "",
        "qr_code_data": invoice.qr_code_data or "",
        "einvoice_status": invoice.einvoice_status,
        "einvoice_provider": invoice.einvoice_provider,
        "einvoice_retry_count": invoice.einvoice_retry_count,
        "einvoice_last_error": invoice.einvoice_last_error or "",
        "einvoice_cancel_reason": invoice.einvoice_cancel_reason or "",
        "einvoice_cancelled_at": invoice.einvoice_cancelled_at.isoformat() if invoice.einvoice_cancelled_at else "",
        "logs": EInvoiceLogSerializer(latest_logs, many=True).data,
    }


def _write_einvoice_log(invoice, event, status_value, request, request_payload, response_payload, message):
    return EInvoiceLog.objects.create(
        business=invoice.business,
        invoice=invoice,
        event=event,
        status=status_value,
        provider=invoice.einvoice_provider or "local_stub",
        request_payload=request_payload,
        response_payload=response_payload,
        message=message,
        created_by=request.user if request and request.user.is_authenticated else None,
    )


def _fail_einvoice(invoice, request, event, request_payload, message, http_status=status.HTTP_400_BAD_REQUEST):
    invoice.einvoice_status = "failed"
    invoice.einvoice_provider = _einvoice_provider_name()
    invoice.einvoice_last_error = message
    invoice.save(update_fields=["einvoice_status", "einvoice_provider", "einvoice_retry_count", "einvoice_last_error", "updated_at"])
    _write_einvoice_log(
        invoice=invoice,
        event=event,
        status_value="failed",
        request=request,
        request_payload=request_payload,
        response_payload={"error": message, "provider": invoice.einvoice_provider},
        message=message,
    )
    _write_sales_activity(
        request,
        "einvoice_failed",
        "sales_invoice",
        invoice.id,
        {
            "invoiceNumber": invoice.invoice_number,
            "provider": invoice.einvoice_provider,
            "event": event,
            "message": message,
        },
    )
    return Response(
        {"success": False, "message": message, "invoice": _einvoice_record(invoice)},
        status=http_status,
    )


def _money(value):
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0
    return f"Rs. {amount:,.2f}"


def _date(value):
    return value.strftime("%d %b %Y") if value else "-"


def _business_address(business):
    return ", ".join(
        str(part)
        for part in [business.address, business.city, business.state, business.pincode]
        if part
    )


def _invoice_print_settings(business):
    invoice_settings, _ = InvoiceSettings.objects.get_or_create(
        business=business,
        defaults={"invoice_prefix": business.invoice_prefix or "INV"},
    )
    business_preferences, _ = BusinessPreference.objects.get_or_create(
        business=business,
        defaults={
            "enable_gst_billing": bool(business.gstin),
            "show_upi_on_invoice": bool(business.upi_id),
        },
    )
    return invoice_settings, business_preferences


def _bank_lines(details):
    if not isinstance(details, dict):
        return []
    labels = (
        ("holder", "Account Holder"),
        ("bank", "Bank"),
        ("account_no", "Account No."),
        ("ifsc", "IFSC"),
        ("branch", "Branch"),
    )
    return [(label, str(details.get(key) or "")) for key, label in labels if details.get(key)]


def _render_sales_invoice_print_html(invoice, template="a4"):
    business = invoice.business
    party = invoice.party
    invoice_settings, business_preferences = _invoice_print_settings(business)
    lines = list(invoice.line_items.all().order_by("sort_order"))
    balance = invoice.total_amount - invoice.paid_amount
    template = "thermal" if template == "thermal" else "a4"
    theme_color = invoice_settings.theme_color or "#5b44d8"
    paper_size = "A5" if invoice_settings.paper_size == "A5" else "A4"
    thermal_width = "80mm" if invoice_settings.thermal_paper_size == "3inch" else "58mm"
    thermal_advanced = invoice_settings.thermal_theme == "advanced"
    bank_lines = _bank_lines(business.bank_account_details)
    should_show_logo = business_preferences.show_logo_on_invoice and (invoice_settings.logo_url or business.logo_url)
    signature_url = invoice_settings.signature_url or business.signature_url
    terms = invoice.terms or business.terms_conditions or "Thank you for your business."

    columns = [
        ("S.NO.", lambda index, line: str(index), "right"),
        ("ITEMS", lambda index, line: f"<strong>{escape(line.item_name)}</strong><small>{escape(line.item_code or '')}</small>", "left"),
    ]
    if invoice_settings.show_hsn:
        columns.append(("HSN", lambda index, line: escape(line.hsn_code or "-"), "right"))
    columns.append(("QTY", lambda index, line: f"{line.quantity:g} {escape(line.unit or 'PCS')}", "right"))
    if invoice_settings.show_free_qty:
        columns.append(("FREE", lambda index, line: f"{line.free_quantity:g}", "right"))
    if invoice_settings.show_mrp:
        columns.append(("MRP", lambda index, line: _money(line.mrp), "right"))
    columns.append(("RATE", lambda index, line: _money(line.rate), "right"))
    if invoice_settings.show_discount:
        columns.append(("DISC.", lambda index, line: f"{line.discount_pct:g}%<small>{_money(line.discount_amount)}</small>", "right"))
    columns.extend([
        ("TAXABLE", lambda index, line: _money(line.taxable_amount), "right"),
        ("GST", lambda index, line: f"{line.gst_rate:g}%", "right"),
        ("AMOUNT", lambda index, line: _money(line.amount), "right"),
    ])

    rows = []
    for index, line in enumerate(lines, start=1):
        cells = "".join(
            f"""<td class="{align}">{renderer(index, line)}</td>"""
            for _, renderer, align in columns
        )
        rows.append(f"""
          <tr>
            {cells}
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
    header_html = "".join(f"<th>{escape(title)}</th>" for title, _, _ in columns)
    no_rows_colspan = len(columns)
    bank_html = "".join(f"<p><strong>{escape(label)}:</strong> {escape(value)}</p>" for label, value in bank_lines)
    signature_html = (
        f"""<img class="signature-img" src="{escape(signature_url)}" alt="Signature" />"""
        if signature_url else ""
    )
    logo_html = (
        f"""<img class="invoice-logo" src="{escape(invoice_settings.logo_url or business.logo_url or '')}" alt="{escape(business.name or 'Business')} logo" />"""
        if should_show_logo else ""
    )

    if template == "thermal":
        body = f"""
          <main class="thermal-receipt">
            <header>
              {logo_html}
              <h1>{escape(business.name or "Business")}</h1>
              <p>{escape(_business_address(business) or business.phone or "")}</p>
              {'<p>GSTIN: ' + escape(business.gstin or '-') + '</p>' if thermal_advanced else ''}
              <strong>TAX INVOICE</strong>
            </header>
            <section class="thermal-meta">
              <span>Invoice</span><b>{escape(invoice.invoice_number)}</b>
              <span>Date</span><b>{_date(invoice.invoice_date)}</b>
              <span>Party</span><b>{escape(party.name)}</b>
            </section>
            <section class="thermal-items">
              {''.join(thermal_rows)}
            </section>
            <section class="thermal-totals">
              <span>Subtotal</span><b>{_money(invoice.subtotal)}</b>
              {'<span>Discount</span><b>- ' + _money(invoice.discount_amount) + '</b>' if invoice.discount_amount else ''}
              <span>CGST</span><b>{_money(invoice.cgst_amount)}</b>
              <span>SGST</span><b>{_money(invoice.sgst_amount)}</b>
              {'<span>' + escape(invoice.additional_charges_label or 'Additional') + '</span><b>' + _money(invoice.additional_charges) + '</b>' if invoice.additional_charges else ''}
              <span>Total</span><strong>{_money(invoice.total_amount)}</strong>
              <span>Paid</span><b>{_money(invoice.paid_amount)}</b>
              <span>Balance</span><strong>{_money(balance)}</strong>
            </section>
            {('<section class="thermal-bank">' + bank_html + '</section>') if thermal_advanced and bank_html else ''}
            <footer>{escape(terms)}</footer>
          </main>
        """
    else:
        body = f"""
          <main class="a4-sheet">
            <header class="invoice-head">
              <div>
                <strong>TAX INVOICE</strong>
                <span>{'ORIGINAL / DUPLICATE' if business_preferences.print_original_duplicate else 'ORIGINAL FOR RECIPIENT'}</span>
              </div>
              <b>{escape(business.name or "Business")}</b>
            </header>
            <section class="invoice-grid">
              <div class="business-block">
                {logo_html}
                <h1>{escape(business.name or "Business")}</h1>
                <p>{escape(_business_address(business) or "-")}</p>
                <p><strong>GSTIN:</strong> {escape(business.gstin or "-")}</p>
                <p><strong>Mobile:</strong> {escape(business.phone or "-")}</p>
                {bank_html}
              </div>
              <div class="meta-block">
                <p><strong>Invoice No.</strong><span>{escape(invoice.invoice_number)}</span></p>
                <p><strong>Invoice Date</strong><span>{_date(invoice.invoice_date)}</span></p>
                <p><strong>Due Date</strong><span>{_date(invoice.due_date)}</span></p>
              </div>
              <div class="party-block">
                <strong>BILL TO</strong>
                <b>{escape(party.name)}</b>
                <span>{escape(party.mobile or "")}</span>
                <span>Place of Supply: {escape(invoice.place_of_supply or business.state or "-")}</span>
              </div>
              <div class="party-block">
                <strong>SHIP TO</strong>
                <b>{escape(party.name)}</b>
              </div>
            </section>
            <table class="line-table">
              <thead>
                <tr>
                  {header_html}
                </tr>
              </thead>
              <tbody>{''.join(rows) or f'<tr><td colspan="{no_rows_colspan}">No line items</td></tr>'}</tbody>
            </table>
            <section class="totals-grid">
              <div>
                <strong>Total Amount In Words</strong>
                <span>{_money(invoice.total_amount)} only</span>
                <strong>Terms & Conditions</strong>
                <span>{escape(terms)}</span>
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
                  <tr><td>{escape(invoice.additional_charges_label or "Additional Charges")}</td><td>{_money(invoice.additional_charges)}</td></tr>
                  <tr class="grand"><td>Total</td><td>{_money(invoice.total_amount)}</td></tr>
                  <tr><td>Paid</td><td>{_money(invoice.paid_amount)}</td></tr>
                  <tr class="balance"><td>Balance</td><td>{_money(balance)}</td></tr>
                </tbody>
              </table>
            </section>
            <footer class="invoice-footer">
              {signature_html}
              <span>Authorized signatory for {escape(business.name or "Business")}</span>
            </footer>
          </main>
        """

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{escape(invoice.invoice_number)} {template.title()} Print</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #eef1f5; color: #111827; font-family: Arial, Helvetica, sans-serif; }}
    .toolbar {{ position: sticky; top: 0; z-index: 2; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 18px; border-bottom: 1px solid #d6dce7; background: #fff; }}
    .toolbar div {{ display: flex; align-items: center; gap: 8px; }}
    .toolbar strong {{ font-size: 15px; }}
    .toolbar button {{ height: 36px; border: 1px solid #cbd5e1; border-radius: 6px; background: #fff; color: #1f2937; padding: 0 12px; font-weight: 700; cursor: pointer; }}
    .toolbar button.primary {{ border-color: #5b44d8; background: #5b44d8; color: #fff; }}
    .a4-sheet {{ width: {'148mm' if paper_size == 'A5' else '210mm'}; min-height: {'210mm' if paper_size == 'A5' else '297mm'}; margin: 18px auto; padding: 12mm; background: #fff; box-shadow: 0 18px 50px rgba(15, 23, 42, 0.18); font-size: 12px; }}
    .invoice-head {{ display: flex; justify-content: space-between; align-items: center; padding-bottom: 8px; }}
    .invoice-head div {{ display: flex; align-items: center; gap: 8px; }}
    .invoice-head strong {{ font-size: 18px; }}
    .invoice-head span {{ border: 1px solid #8c95aa; color: #566174; padding: 4px 7px; font-weight: 800; }}
    .invoice-grid {{ display: grid; grid-template-columns: 1fr 1fr; border: 1px solid #111; }}
    .invoice-grid > div {{ min-height: 34mm; border-right: 1px solid #111; border-bottom: 1px solid #111; padding: 8px; }}
    .invoice-grid > div:nth-child(2n) {{ border-right: 0; }}
    h1 {{ margin: 0 0 6px; color: {escape(theme_color)}; font-size: 20px; }}
    p {{ margin: 3px 0; line-height: 1.4; }}
    .meta-block {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; text-align: center; align-content: center; }}
    .meta-block p {{ display: grid; gap: 6px; }}
    .party-block {{ display: grid; align-content: start; gap: 6px; }}
    .line-table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
    .line-table th, .line-table td {{ border: 1px solid #111; padding: 6px; text-align: right; vertical-align: top; }}
    .line-table th {{ background: color-mix(in srgb, {escape(theme_color)} 14%, white); font-weight: 800; }}
    .line-table th:nth-child(2), .line-table td:nth-child(2) {{ text-align: left; }}
    .line-table td.left {{ text-align: left; }}
    .line-table td.right {{ text-align: right; }}
    .line-table small {{ display: block; color: #6b7280; margin-top: 3px; }}
    .totals-grid {{ display: grid; grid-template-columns: 1fr 72mm; gap: 12px; margin-top: 10px; }}
    .totals-grid > div {{ border: 1px solid #111; padding: 8px; display: grid; gap: 7px; align-content: start; }}
    .totals-grid table {{ width: 100%; border-collapse: collapse; }}
    .totals-grid td {{ border: 1px solid #111; padding: 7px; }}
    .totals-grid td:last-child {{ text-align: right; font-weight: 800; }}
    .grand td {{ background: color-mix(in srgb, {escape(theme_color)} 14%, white); font-size: 14px; }}
    .balance td {{ color: #d91f2a; }}
    .invoice-footer {{ min-height: 26mm; border: 1px solid #111; border-top: 0; display: flex; align-items: flex-end; justify-content: flex-end; padding: 10px; }}
    .invoice-logo {{ max-width: 72px; max-height: 48px; object-fit: contain; display: block; margin-bottom: 6px; }}
    .signature-img {{ max-width: 130px; max-height: 62px; object-fit: contain; display: block; margin-right: 16px; }}
    .thermal-receipt {{ width: {thermal_width}; min-height: 120mm; margin: 18px auto; padding: 4mm; background: #fff; box-shadow: 0 18px 50px rgba(15, 23, 42, 0.18); font-size: 10px; }}
    .thermal-receipt header {{ text-align: center; border-bottom: 1px dashed #111; padding-bottom: 6px; }}
    .thermal-receipt h1 {{ color: #111; font-size: 14px; }}
    .thermal-meta, .thermal-totals {{ display: grid; grid-template-columns: 1fr auto; gap: 4px 8px; padding: 7px 0; border-bottom: 1px dashed #111; }}
    .thermal-line {{ display: grid; gap: 3px; padding: 6px 0; border-bottom: 1px dotted #bbb; }}
    .thermal-line strong, .thermal-totals strong {{ font-size: 12px; text-align: right; }}
    .thermal-bank {{ padding: 7px 0; border-bottom: 1px dashed #111; }}
    .thermal-bank p {{ margin: 2px 0; }}
    .thermal-receipt footer {{ text-align: center; padding-top: 8px; }}
    @page {{ size: {f'{thermal_width} auto' if template == 'thermal' else paper_size}; margin: {'2mm' if template == 'thermal' else '8mm'}; }}
    @media print {{
      body {{ background: #fff; }}
      .toolbar {{ display: none; }}
      .a4-sheet, .thermal-receipt {{ margin: 0; box-shadow: none; }}
    }}
  </style>
</head>
<body>
  <div class="toolbar">
    <strong>{escape(invoice.invoice_number)} - {template.title()} preview</strong>
    <div>
      <button onclick="window.close()">Close</button>
      <button class="primary" onclick="window.print()">Print</button>
    </div>
  </div>
  {body}
</body>
</html>"""


class SalesInvoiceViewSet(LifecycleDeleteBlockedMixin, viewsets.ModelViewSet):
    serializer_class = SalesInvoiceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return SalesInvoice.objects.none()
        
        queryset = SalesInvoice.objects.filter(business=self.request.business)
        
        # Filter by status
        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)
            
        # Filter by party
        party = self.request.query_params.get("party")
        if party:
            queryset = queryset.filter(party_id=party)
            
        return queryset.order_by("-created_at")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Cancels invoice, restores stock, and reverses invoice-created payment allocations."""
        invoice_ref = self.get_object()
        reason = request.data.get("reason", "Cancelled by user request")
            
        with transaction.atomic():
            invoice = (
                SalesInvoice.objects.select_for_update()
                .prefetch_related("line_items")
                .get(id=invoice_ref.id, business=request.business)
            )
            if invoice.status == "cancelled":
                return Response(
                    {"success": False, "message": "Invoice is already cancelled"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            invoice.status = "cancelled"
            invoice.cancellation_reason = reason
            invoice.cancelled_at = timezone.now()
            invoice.cancelled_by = request.user
            invoice.paid_amount = 0
            invoice.save(update_fields=[
                "status",
                "cancellation_reason",
                "cancelled_at",
                "cancelled_by",
                "paid_amount",
                "updated_at",
            ])
            
            # Restore all inventory items in this invoice
            for line_item in invoice.line_items.all():
                if line_item.item:
                    actual_item = Item.objects.select_for_update().get(id=line_item.item.id, business=request.business)
                    restored_qty = line_item.quantity + line_item.free_quantity
                    
                    apply_stock_movement(
                        business=request.business,
                        item=actual_item,
                        godown=actual_item.godown,
                        movement_type="sales_return",
                        reference_type="sales_invoice_cancel",
                        reference_id=invoice.id,
                        quantity=restored_qty,
                        rate=line_item.rate,
                        created_by=request.user,
                        notes=f"Restored via Invoice Cancellation {invoice.invoice_number}"
                    )

            for settlement in PaymentInSettlement.objects.select_related("payment_in").filter(
                invoice=invoice,
                payment_in__business=request.business,
                payment_in__status="active",
            ):
                payment = settlement.payment_in
                has_other_allocations = payment.settlements.exclude(invoice=invoice).exists()
                if has_other_allocations:
                    settlement.delete()
                    continue

                payment.status = "void"
                payment.cancellation_reason = f"Sales invoice {invoice.invoice_number} cancelled: {reason}"
                payment.cancelled_at = timezone.now()
                payment.cancelled_by = request.user
                payment.save(update_fields=[
                    "status",
                    "cancellation_reason",
                    "cancelled_at",
                    "cancelled_by",
                ])

            invoice.refresh_from_db()
            serialized_invoice = SalesInvoiceSerializer(invoice, context={"request": request}).data
            _write_sales_activity(
                request,
                "sales_invoice_cancelled",
                "sales_invoice",
                invoice.id,
                {
                    "invoiceNumber": invoice.invoice_number,
                    "party": invoice.party.name,
                    "reason": reason,
                    "totalAmount": invoice.total_amount,
                    "linkedPaymentsReversed": True,
                },
            )

        return Response({
            "success": True,
            "message": "Invoice cancelled, stock restored, and linked payments reversed successfully",
            "invoice": serialized_invoice,
        })

    @action(detail=True, methods=["post"])
    def trigger_einvoice(self, request, pk=None):
        """Runs the local/provider-stub IRN lifecycle and persists status/logs."""
        return self._run_einvoice_generation(request, self.get_object(), event="generate")

    @action(detail=True, methods=["post"])
    def retry_einvoice(self, request, pk=None):
        return self._run_einvoice_generation(request, self.get_object(), event="retry")

    @action(detail=True, methods=["post"])
    def cancel_einvoice(self, request, pk=None):
        invoice = self.get_object()
        reason = request.data.get("reason") or "Cancelled from e-invoicing workspace"

        if invoice.einvoice_status == "cancelled":
            return Response(
                {"success": False, "message": "E-Invoice is already cancelled"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not invoice.irn or invoice.einvoice_status != "generated":
            return Response(
                {"success": False, "message": "Only generated e-invoices can be cancelled"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            invoice = SalesInvoice.objects.select_for_update().get(id=invoice.id, business=request.business)
            if invoice.einvoice_status == "cancelled":
                return Response(
                    {"success": False, "message": "E-Invoice is already cancelled"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not invoice.irn or invoice.einvoice_status != "generated":
                return Response(
                    {"success": False, "message": "Only generated e-invoices can be cancelled"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            provider_name = (invoice.einvoice_provider or _einvoice_provider_name()).strip().lower()
            if provider_name not in LOCAL_EINVOICE_PROVIDERS:
                request_payload = {"reason": reason, "irn": invoice.irn, "invoice_number": invoice.invoice_number}
                try:
                    provider_response = _post_provider_json("cancel", request_payload)
                except (ProviderConfigurationError, ProviderDeliveryError) as error:
                    invoice.einvoice_last_error = str(error)
                    invoice.save(update_fields=["einvoice_last_error", "updated_at"])
                    _write_einvoice_log(
                        invoice=invoice,
                        event="cancel",
                        status_value="failed",
                        request=request,
                        request_payload=request_payload,
                        response_payload={"error": str(error), "provider": provider_name},
                        message=str(error),
                    )
                    _write_sales_activity(
                        request,
                        "einvoice_cancel_failed",
                        "sales_invoice",
                        invoice.id,
                        {
                            "invoiceNumber": invoice.invoice_number,
                            "irn": invoice.irn,
                            "provider": provider_name,
                            "message": str(error),
                        },
                    )
                    return Response(
                        {"success": False, "message": str(error), "invoice": _einvoice_record(invoice)},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            invoice.einvoice_status = "cancelled"
            invoice.einvoice_cancel_reason = reason
            invoice.einvoice_cancelled_at = timezone.now()
            invoice.einvoice_last_error = ""
            invoice.save(update_fields=[
                "einvoice_status", "einvoice_cancel_reason", "einvoice_cancelled_at",
                "einvoice_last_error", "updated_at",
            ])
            _write_einvoice_log(
                invoice=invoice,
                event="cancel",
                status_value="cancelled",
                request=request,
                request_payload={"reason": reason, "irn": invoice.irn},
                response_payload={
                    "cancelled_at": invoice.einvoice_cancelled_at.isoformat(),
                    "provider": invoice.einvoice_provider,
                },
                message="E-Invoice cancelled successfully",
            )
            _write_sales_activity(
                request,
                "einvoice_cancelled",
                "sales_invoice",
                invoice.id,
                {
                    "invoiceNumber": invoice.invoice_number,
                    "irn": invoice.irn,
                    "provider": invoice.einvoice_provider,
                    "reason": reason,
                },
            )

        return Response({
            "success": True,
            "message": "E-Invoice cancelled successfully",
            "invoice": _einvoice_record(invoice),
        })

    @action(detail=True, methods=["get"])
    def einvoice_logs(self, request, pk=None):
        invoice = self.get_object()
        logs = invoice.einvoice_logs.all()
        return Response({"success": True, "logs": EInvoiceLogSerializer(logs, many=True).data})

    @action(detail=True, methods=["get"])
    def einvoice_qr(self, request, pk=None):
        invoice = self.get_object()
        if not invoice.qr_code_data or invoice.einvoice_status != "generated":
            return Response(
                {"success": False, "message": "QR payload is available only after e-invoice generation"},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            import qrcode
            import qrcode.image.svg
        except ImportError:
            return Response(
                {"success": False, "message": "QR renderer package is not installed"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        image = qrcode.make(invoice.qr_code_data, image_factory=qrcode.image.svg.SvgImage)
        output = BytesIO()
        image.save(output)
        return HttpResponse(output.getvalue(), content_type="image/svg+xml")

    @action(detail=False, methods=["get"])
    def einvoice_register(self, request):
        queryset = self.get_queryset().select_related("party").prefetch_related("einvoice_logs")
        status_param = request.query_params.get("einvoice_status")
        if status_param:
            queryset = queryset.filter(einvoice_status=status_param)
        return Response({
            "success": True,
            "invoices": [_einvoice_record(invoice) for invoice in queryset.order_by("-invoice_date", "-created_at")[:200]],
        })

    def _run_einvoice_generation(self, request, invoice, event):
        if invoice.status == "cancelled":
            return Response(
                {"success": False, "message": "Cancelled sales invoices cannot generate e-invoices"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if invoice.einvoice_status == "generated" and invoice.irn:
            return Response(
                {"success": False, "message": "E-Invoice already generated for this voucher"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if invoice.einvoice_status == "cancelled":
            return Response(
                {"success": False, "message": "Cancelled e-invoices cannot be regenerated"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            invoice = SalesInvoice.objects.select_for_update().select_related("business", "party").get(
                id=invoice.id,
                business=request.business,
            )
            if invoice.status == "cancelled":
                return Response(
                    {"success": False, "message": "Cancelled sales invoices cannot generate e-invoices"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if invoice.einvoice_status == "generated" and invoice.irn:
                return Response(
                    {"success": False, "message": "E-Invoice already generated for this voucher"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if invoice.einvoice_status == "cancelled":
                return Response(
                    {"success": False, "message": "Cancelled e-invoices cannot be regenerated"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            invoice.einvoice_retry_count += 1
            invoice.einvoice_provider = _einvoice_provider_name()
            request_payload = {
                "invoice_number": invoice.invoice_number,
                "supplier_gstin": invoice.business.gstin or "",
                "party_gstin": invoice.party.gstin or "",
                "amount": str(invoice.total_amount),
                "attempt": invoice.einvoice_retry_count,
                "lines": [
                    {
                        "item_name": line.item_name,
                        "item_code": line.item_code,
                        "hsn_code": line.hsn_code,
                        "quantity": str(line.quantity),
                        "rate": str(line.rate),
                        "gst_rate": str(line.gst_rate),
                        "amount": str(line.amount),
                    }
                    for line in invoice.line_items.all().order_by("sort_order")
                ],
            }

            if not invoice.business.gstin:
                return _fail_einvoice(
                    invoice,
                    request,
                    event,
                    request_payload,
                    "Business GSTIN is required for e-invoicing.",
                )

            if invoice.einvoice_provider not in LOCAL_EINVOICE_PROVIDERS:
                try:
                    provider_response = _post_provider_json("generate", request_payload)
                    irn = provider_response.get("irn") or provider_response.get("Irn") or provider_response.get("IRN")
                    ack_number = provider_response.get("ack_number") or provider_response.get("AckNo") or provider_response.get("ackNumber")
                    qr_code_data = provider_response.get("qr_code_data") or provider_response.get("SignedQRCode") or provider_response.get("qrCodeData")
                    if not irn or not ack_number or not qr_code_data:
                        raise ProviderDeliveryError("E-Invoice provider response must include IRN, acknowledgement number, and QR payload.")
                except (ProviderConfigurationError, ProviderDeliveryError) as error:
                    return _fail_einvoice(invoice, request, event, request_payload, str(error))

                ack_time = timezone.now()
                invoice.irn = str(irn)
                invoice.ack_number = str(ack_number)
                invoice.ack_date = ack_time
                invoice.qr_code_data = str(qr_code_data)
                invoice.einvoice_status = "generated"
                invoice.einvoice_last_error = ""
                invoice.einvoice_cancel_reason = ""
                invoice.einvoice_cancelled_at = None
                invoice.save(update_fields=[
                    "irn", "ack_number", "ack_date", "qr_code_data", "einvoice_status",
                    "einvoice_provider", "einvoice_retry_count", "einvoice_last_error",
                    "einvoice_cancel_reason", "einvoice_cancelled_at", "updated_at",
                ])
                _write_einvoice_log(
                    invoice=invoice,
                    event=event,
                    status_value="success",
                    request=request,
                    request_payload=request_payload,
                    response_payload=provider_response,
                    message=f"E-Invoice generated through {invoice.einvoice_provider}",
                )
                _write_sales_activity(
                    request,
                    "einvoice_generated",
                    "sales_invoice",
                    invoice.id,
                    {
                        "invoiceNumber": invoice.invoice_number,
                        "irn": invoice.irn,
                        "ackNumber": invoice.ack_number,
                        "provider": invoice.einvoice_provider,
                        "event": event,
                    },
                )

                return Response({
                    "success": True,
                    "message": "E-Invoice generated successfully",
                    "irn": invoice.irn,
                    "qr_code_data": invoice.qr_code_data,
                    "invoice": _einvoice_record(invoice),
                })

            irn = uuid.uuid5(uuid.NAMESPACE_DNS, f"{invoice.business_id}:{invoice.invoice_number}:{invoice.einvoice_retry_count}").hex.upper()
            ack_time = timezone.now()
            invoice.irn = irn
            invoice.ack_number = f"ACK{ack_time.strftime('%y%m%d%H%M%S')}"
            invoice.ack_date = ack_time
            invoice.qr_code_data = (
                f"GSTIN:{invoice.business.gstin}|IRN:{irn}|INV:{invoice.invoice_number}|"
                f"DATE:{invoice.invoice_date.isoformat()}|AMT:{invoice.total_amount}|ACK:{invoice.ack_number}"
            )
            invoice.einvoice_status = "generated"
            invoice.einvoice_provider = _einvoice_provider_name()
            invoice.einvoice_last_error = ""
            invoice.einvoice_cancel_reason = ""
            invoice.einvoice_cancelled_at = None
            invoice.save(update_fields=[
                "irn", "ack_number", "ack_date", "qr_code_data", "einvoice_status",
                "einvoice_provider", "einvoice_retry_count", "einvoice_last_error",
                "einvoice_cancel_reason", "einvoice_cancelled_at", "updated_at",
            ])
            _write_einvoice_log(
                invoice=invoice,
                event=event,
                status_value="success",
                request=request,
                request_payload=request_payload,
                response_payload={
                    "irn": invoice.irn,
                    "ack_number": invoice.ack_number,
                    "ack_date": invoice.ack_date.isoformat(),
                    "qr_code_data": invoice.qr_code_data,
                },
                message="E-Invoice generated successfully in local provider stub",
            )
            _write_sales_activity(
                request,
                "einvoice_generated",
                "sales_invoice",
                invoice.id,
                {
                    "invoiceNumber": invoice.invoice_number,
                    "irn": invoice.irn,
                    "ackNumber": invoice.ack_number,
                    "provider": invoice.einvoice_provider,
                    "event": event,
                },
            )

        return Response({
            "success": True,
            "message": "E-Invoice generated successfully",
            "irn": invoice.irn,
            "qr_code_data": invoice.qr_code_data,
            "invoice": _einvoice_record(invoice),
        })

    @action(detail=True, methods=["get"])
    def print_pdf(self, request, pk=None):
        """Returns a print-ready invoice HTML template for browser PDF or thermal print."""
        invoice = self.get_object()
        template = request.query_params.get("template") or request.query_params.get("format") or "a4"
        return HttpResponse(_render_sales_invoice_print_html(invoice, template), content_type="text/html")

class QuotationViewSet(LifecycleDeleteBlockedMixin, viewsets.ModelViewSet):
    serializer_class = QuotationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return Quotation.objects.none()
        return Quotation.objects.filter(business=self.request.business).order_by("-created_at")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        quotation = self.get_object()
        if quotation.status == "cancelled":
            return Response({"success": False, "message": "Quotation is already cancelled"}, status=status.HTTP_400_BAD_REQUEST)
        if quotation.status == "converted":
            return Response({"success": False, "message": "Converted quotations cannot be cancelled"}, status=status.HTTP_400_BAD_REQUEST)
        quotation.status = "cancelled"
        quotation.save(update_fields=["status"])
        _write_sales_activity(
            request,
            "quotation_cancelled",
            "quotation",
            quotation.id,
            _voucher_activity_details(quotation, "quotation_number"),
        )
        return Response({"success": True, "message": "Quotation cancelled successfully"})

    @action(detail=True, methods=["post"])
    def convert_to_invoice(self, request, pk=None):
        """Converts an open quotation into a Sales Invoice."""
        return _convert_sales_register_voucher_to_invoice(
            viewset=self,
            request=request,
            pk=pk,
            source_label="Quotation",
            number_attr="quotation_number",
            subtotal_attr="subtotal",
            terms_attr="terms",
        )

class DeliveryChallanViewSet(LifecycleDeleteBlockedMixin, viewsets.ModelViewSet):
    serializer_class = DeliveryChallanSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return DeliveryChallan.objects.none()
        return DeliveryChallan.objects.filter(business=self.request.business).order_by("-created_at")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        challan = self.get_object()
        if challan.status == "cancelled":
            return Response({"success": False, "message": "Challan is already cancelled"}, status=status.HTTP_400_BAD_REQUEST)
        if challan.status == "converted":
            return Response({"success": False, "message": "Converted challans cannot be cancelled"}, status=status.HTTP_400_BAD_REQUEST)
        challan.status = "cancelled"
        challan.save(update_fields=["status"])
        _write_sales_activity(
            request,
            "delivery_challan_cancelled",
            "delivery_challan",
            challan.id,
            _voucher_activity_details(challan, "challan_number"),
        )
        return Response({"success": True, "message": "Delivery challan cancelled successfully"})

    @action(detail=True, methods=["post"])
    def convert_to_invoice(self, request, pk=None):
        """Converts delivery challan into a Sales Invoice."""
        return _convert_sales_register_voucher_to_invoice(
            viewset=self,
            request=request,
            pk=pk,
            source_label="Delivery Challan",
            number_attr="challan_number",
        )

class CreditNoteViewSet(LifecycleDeleteBlockedMixin, viewsets.ModelViewSet):
    serializer_class = CreditNoteSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return CreditNote.objects.none()
        return CreditNote.objects.filter(business=self.request.business).order_by("-created_at")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        credit_note = self.get_object()
        if credit_note.status == "cancelled":
            return Response({"success": False, "message": "Credit note is already cancelled"}, status=status.HTTP_400_BAD_REQUEST)
        credit_note.status = "cancelled"
        credit_note.save(update_fields=["status"])
        _write_sales_activity(
            request,
            "credit_note_cancelled",
            "credit_note",
            credit_note.id,
            _voucher_activity_details(credit_note, "credit_note_number"),
        )
        return Response({"success": True, "message": "Credit note cancelled successfully"})

class ProformaInvoiceViewSet(LifecycleDeleteBlockedMixin, viewsets.ModelViewSet):
    serializer_class = ProformaInvoiceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return ProformaInvoice.objects.none()

        queryset = ProformaInvoice.objects.filter(business=self.request.business)

        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)

        party = self.request.query_params.get("party")
        if party:
            queryset = queryset.filter(party_id=party)

        return queryset.order_by("-created_at")

    def perform_create(self, serializer):
        serializer.save(business=self.request.business)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        proforma = self.get_object()
        if proforma.status == "cancelled":
            return Response({"success": False, "message": "Proforma invoice is already cancelled"}, status=status.HTTP_400_BAD_REQUEST)
        if proforma.status == "converted":
            return Response({"success": False, "message": "Converted proforma invoices cannot be cancelled"}, status=status.HTTP_400_BAD_REQUEST)
        proforma.status = "cancelled"
        proforma.save(update_fields=["status"])
        _write_sales_activity(
            request,
            "proforma_invoice_cancelled",
            "proforma_invoice",
            proforma.id,
            _voucher_activity_details(proforma, "proforma_number"),
        )
        return Response({"success": True, "message": "Proforma invoice cancelled successfully"})

    @action(detail=True, methods=["post"])
    def convert_to_invoice(self, request, pk=None):
        """Converts an open proforma invoice into a Sales Invoice."""
        return _convert_sales_register_voucher_to_invoice(
            viewset=self,
            request=request,
            pk=pk,
            source_label="Proforma Invoice",
            number_attr="proforma_number",
        )

class SalesReturnViewSet(LifecycleDeleteBlockedMixin, viewsets.ModelViewSet):
    serializer_class = SalesReturnSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return SalesReturn.objects.none()

        queryset = SalesReturn.objects.filter(business=self.request.business)

        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)

        party = self.request.query_params.get("party")
        if party:
            queryset = queryset.filter(party_id=party)

        return queryset.order_by("-created_at")

    def perform_create(self, serializer):
        serializer.save(business=self.request.business)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        sales_return = self.get_object()
        if sales_return.status == "cancelled":
            return Response({"success": False, "message": "Sales return is already cancelled"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            for line_item in sales_return.line_items.select_related("item"):
                if not line_item.item:
                    continue
                actual_item = Item.objects.select_for_update().get(id=line_item.item.id, business=request.business)
                apply_stock_movement(
                    business=request.business,
                    item=actual_item,
                    godown=actual_item.godown,
                    movement_type="adjustment_out",
                    reference_type="sales_return_cancel",
                    reference_id=sales_return.id,
                    quantity=-line_item.quantity,
                    rate=line_item.rate,
                    created_by=request.user,
                    notes=f"Reversed via Sales Return Cancellation {sales_return.return_number}",
                    allow_negative=False,
                )

            sales_return.status = "cancelled"
            sales_return.save(update_fields=["status"])
            _write_sales_activity(
                request,
                "sales_return_cancelled",
                "sales_return",
                sales_return.id,
                _voucher_activity_details(sales_return, "return_number"),
            )

        return Response({"success": True, "message": "Sales return cancelled and stock reversed successfully"})
