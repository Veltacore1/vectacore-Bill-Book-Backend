import csv
import io
from collections import defaultdict
from datetime import date as date_cls, datetime, timedelta
from decimal import Decimal
from html import escape
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from django.db import transaction
from django.utils.text import slugify
from django.utils import timezone
from rest_framework import viewsets, permissions, status
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from apps.accounts.activity import write_activity
from apps.accounts.email_delivery import EmailDeliveryResult, is_email_recipient, send_email
from apps.accounts.throttles import TenantScopedRateThrottle
from .models import BankAccount, BankTransaction, Expense, AutomatedBill, ReportShare
from .serializers import (
    BankAccountSerializer, BankTransactionSerializer, 
    ExpenseSerializer, AutomatedBillSerializer, apply_bank_transaction_balance
)
from django.db.models import Sum

RUPEE = "\u20b9"

def _num(value):
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)

def _inr(value, decimals=2):
    amount = _num(value)
    sign = "- " if amount < 0 else ""
    raw = f"{abs(amount):.{decimals}f}"
    if "." in raw:
        whole, fraction = raw.split(".")
    else:
        whole, fraction = raw, ""
    if len(whole) > 3:
        head = whole[:-3]
        tail = whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{RUPEE} {whole}" if decimals == 0 else f"{sign}{RUPEE} {whole}.{fraction}"

def _date(value):
    return value.strftime("%d %b %Y") if value else ""

def _percent(value, total):
    total_amount = Decimal(str(total or 0))
    if total_amount == 0:
        return "0%"
    return f"{(Decimal(str(value or 0)) / total_amount * Decimal('100')):.1f}%"

def _report(
    report_id,
    name,
    category,
    description,
    metric_label,
    metric_value,
    columns,
    rows,
    favourite=False,
    badge=None,
    filters=None,
):
    payload = {
        "id": report_id,
        "name": name,
        "category": category,
        "description": description,
        "metricLabel": metric_label,
        "metricValue": metric_value,
        "columns": columns,
        "rows": rows,
        "favourite": favourite,
        "rowCount": len(rows),
        "generatedAt": timezone.now().isoformat(),
        "exportFileName": report_id,
        "filters": filters or {},
    }
    if badge:
        payload["badge"] = badge
    return payload


def _report_rows_for_export(report):
    rows = [[report["name"]]]
    for label, value in (report.get("filters") or {}).items():
        rows.append([label, value])
    rows.extend([
        [report["metricLabel"], report["metricValue"]],
        [],
        report["columns"],
        *report["rows"],
    ])
    return rows


def _report_csv(report):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(_report_rows_for_export(report))
    return output.getvalue()


def _report_html(report, business):
    filter_rows = "".join(
        f"<tr><td>{escape(str(label))}</td><td>{escape(str(value))}</td></tr>"
        for label, value in (report.get("filters") or {}).items()
    )
    header_cells = "".join(f"<th>{escape(str(column))}</th>" for column in report["columns"])
    body_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in report["rows"]
    )
    generated_at = timezone.localtime(timezone.now()).strftime("%d %b %Y, %I:%M %p")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{escape(report["name"])}</title>
  <style>
    @page {{ size: A4; margin: 12mm; }}
    body {{ font-family: Arial, Helvetica, sans-serif; color: #111827; margin: 24px; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; border-bottom: 2px solid #111827; padding-bottom: 12px; margin-bottom: 18px; }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    h2 {{ margin: 0; font-size: 16px; }}
    p {{ margin: 0; color: #4b5563; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 14px; }}
    th, td {{ border: 1px solid #cfd6e4; padding: 8px 10px; text-align: left; font-size: 12px; }}
    th {{ background: #f3f5f8; }}
    .metric {{ display: inline-grid; gap: 4px; border: 1px solid #cfd6e4; border-radius: 6px; padding: 10px 12px; margin: 12px 0; }}
    .metric span {{ color: #667085; font-size: 12px; font-weight: 700; }}
    .metric strong {{ font-size: 20px; }}
    .filters {{ max-width: 620px; }}
    .muted {{ color: #667085; font-size: 12px; }}
    .actions {{ margin-bottom: 12px; }}
    .actions button {{ border: 1px solid #cfd6e4; background: white; border-radius: 6px; padding: 8px 12px; font-weight: 700; }}
    @media print {{ body {{ margin: 0; }} .actions {{ display: none; }} }}
  </style>
</head>
<body>
  <div class="actions"><button onclick="window.print()">Print / Save PDF</button></div>
  <header>
    <div>
      <h2>{escape(business.name)}</h2>
      <p>{escape(business.gstin or "")}</p>
      <p>{escape(business.phone or "")}</p>
    </div>
    <div class="muted">Generated: {escape(generated_at)}</div>
  </header>
  <h1>{escape(report["name"])}</h1>
  <p>{escape(report["description"])}</p>
  <section class="metric"><span>{escape(report["metricLabel"])}</span><strong>{escape(report["metricValue"])}</strong></section>
  <table class="filters"><tbody>{filter_rows}</tbody></table>
  <table><thead><tr>{header_cells}</tr></thead><tbody>{body_rows}</tbody></table>
</body>
</html>"""


def _report_export_response(report, business, export_format):
    export_format = (export_format or "csv").lower()
    base_name = slugify(report.get("exportFileName") or report.get("id") or "report") or "report"

    if export_format == "html":
        return HttpResponse(_report_html(report, business), content_type="text/html; charset=utf-8")

    if export_format == "excel":
        response = HttpResponse(
            _report_html(report, business),
            content_type="application/vnd.ms-excel; charset=utf-8",
        )
        response["Content-Disposition"] = f'attachment; filename="{base_name}.xls"'
        return response

    if export_format == "pdf":
        html = _report_html(report, business)
        try:
            from weasyprint import HTML

            response = HttpResponse(HTML(string=html).write_pdf(), content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="{base_name}.pdf"'
            return response
        except Exception as exc:
            response = HttpResponse(html, content_type="text/html; charset=utf-8")
            response["X-Report-PDF-Fallback"] = str(exc)[:180]
            return response

    response = HttpResponse(_report_csv(report), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{base_name}.csv"'
    return response


def _share_query_params(request):
    return {
        key: value
        for key, value in request.query_params.items()
        if key not in {"report", "report_id", "export_format", "fmt"}
    }


def _report_share_url(request, share):
    return request.build_absolute_uri(f"/api/v1/accounting/reports/shared/{share.share_token}/")


def _delivery_status_for_share(delivery):
    if delivery.delivered:
        return "sent"
    if delivery.provider == "skipped":
        return "prepared"
    return "failed"


def _store_share_delivery(share, delivery):
    filters = dict(share.filters or {})
    filters["emailDelivery"] = delivery.as_dict()
    share.status = _delivery_status_for_share(delivery)
    share.filters = filters
    share.save(update_fields=["status", "filters", "updated_at"])


def _report_share_email_html(business, title, intro, share_rows):
    links_html = "".join(
        f"""
        <tr>
          <td style="padding:10px;border:1px solid #d8dee9;">{escape(row['name'])}</td>
          <td style="padding:10px;border:1px solid #d8dee9;">{escape(row['dateRange'] or '-')}</td>
          <td style="padding:10px;border:1px solid #d8dee9;"><a href="{escape(row['url'])}">Open report</a></td>
        </tr>
        """
        for row in share_rows
    )
    return f"""<!doctype html>
<html>
<body style="font-family:Arial,Helvetica,sans-serif;color:#111827;line-height:1.5;">
  <h2 style="margin:0 0 8px;">{escape(title)}</h2>
  <p style="margin:0 0 16px;">{escape(intro)}</p>
  <p style="margin:0 0 16px;"><strong>Business:</strong> {escape(business.name)}</p>
  <table style="border-collapse:collapse;width:100%;max-width:760px;">
    <thead>
      <tr>
        <th style="padding:10px;border:1px solid #d8dee9;text-align:left;background:#f3f5f8;">Report</th>
        <th style="padding:10px;border:1px solid #d8dee9;text-align:left;background:#f3f5f8;">Date Range</th>
        <th style="padding:10px;border:1px solid #d8dee9;text-align:left;background:#f3f5f8;">Link</th>
      </tr>
    </thead>
    <tbody>{links_html}</tbody>
  </table>
  <p style="margin-top:16px;color:#667085;font-size:12px;">These read-only links can be revoked from VastraBook settings.</p>
</body>
</html>"""


def _send_report_share_email(request, shares, *, title, intro):
    shares = list(shares)
    if not shares:
        return EmailDeliveryResult(False, "skipped", "No report share links were created.")
    recipient = shares[0].recipient
    if not is_email_recipient(recipient):
        return EmailDeliveryResult(
            delivered=False,
            provider="skipped",
            message="Recipient is not an email address; share links were prepared only.",
        )
    share_rows = [
        {
            "name": share.report_name,
            "dateRange": share.date_range,
            "url": _report_share_url(request, share),
        }
        for share in shares
    ]
    text = "\n".join(f"{row['name']}: {row['url']}" for row in share_rows)
    return send_email(
        to=recipient,
        subject=title,
        html=_report_share_email_html(request.business, title, intro, share_rows),
        text=text,
    )


def _movement_label(value):
    return (value or "").replace("_", " ").title() or "-"


class ReportsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _build_payload(self, request):
        business = request.business
        if not business:
            return None, Response(
                {"success": False, "message": "No active tenant business"},
                status=status.HTTP_404_NOT_FOUND
            )

        from apps.items.models import Item, ItemGodownStock, StockMovement
        from apps.parties.models import Party
        from apps.parties.serializers import PartySerializer
        from apps.payments.models import PaymentIn, PaymentOut
        from apps.purchases.models import PurchaseInvoice, PurchaseInvoiceItem
        from apps.sales.models import SalesInvoice, SalesInvoiceItem

        date_range = request.query_params.get("date_range") or request.query_params.get("range") or "Last 365 Days"
        party_id = request.query_params.get("party") or ""
        item_id = request.query_params.get("item") or ""
        today = timezone.localdate()

        def parse_date(value):
            if not value:
                return None
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                return None

        if date_range == "This Month":
            start_date = today.replace(day=1)
            end_date = today
        elif date_range == "Last Month":
            first_this_month = today.replace(day=1)
            end_date = first_this_month - timedelta(days=1)
            start_date = end_date.replace(day=1)
        elif date_range == "This Quarter":
            quarter_month = ((today.month - 1) // 3) * 3 + 1
            start_date = today.replace(month=quarter_month, day=1)
            end_date = today
        elif date_range == "Last 30 Days":
            start_date = today - timedelta(days=30)
            end_date = today
        elif date_range == "Custom":
            start_date = parse_date(request.query_params.get("from"))
            end_date = parse_date(request.query_params.get("to"))
        elif date_range == "All Time":
            start_date = None
            end_date = None
        else:
            start_date = today - timedelta(days=365)
            end_date = today

        selected_party = None
        selected_item = None
        if party_id:
            try:
                selected_party = Party.objects.filter(id=party_id, business=business, is_active=True).first()
            except (ValueError, DjangoValidationError):
                selected_party = None
            if not selected_party:
                return None, Response(
                    {"success": False, "message": "Party filter is not available for this tenant"},
                    status=status.HTTP_404_NOT_FOUND,
                )
        if item_id:
            try:
                selected_item = Item.objects.filter(id=item_id, business=business, is_active=True).first()
            except (ValueError, DjangoValidationError):
                selected_item = None
            if not selected_item:
                return None, Response(
                    {"success": False, "message": "Item filter is not available for this tenant"},
                    status=status.HTTP_404_NOT_FOUND,
                )

        def filter_date(queryset, field_name):
            if start_date:
                queryset = queryset.filter(**{f"{field_name}__gte": start_date})
            if end_date:
                queryset = queryset.filter(**{f"{field_name}__lte": end_date})
            return queryset

        sales = SalesInvoice.objects.filter(business=business).exclude(status="cancelled").select_related("party")
        purchases = PurchaseInvoice.objects.filter(business=business).exclude(status="cancelled").select_related("party")
        expenses = Expense.objects.filter(business=business)
        payments_in = PaymentIn.objects.filter(business=business, status="active").select_related("party")
        payments_out = PaymentOut.objects.filter(business=business, status="active").select_related("party")
        bank_transactions = BankTransaction.objects.filter(business=business).select_related("bank_account")
        stock_movements = StockMovement.objects.filter(business=business).select_related("item", "godown")

        sales = filter_date(sales, "invoice_date")
        purchases = filter_date(purchases, "invoice_date")
        expenses = filter_date(expenses, "expense_date")
        payments_in = filter_date(payments_in, "payment_date")
        payments_out = filter_date(payments_out, "payment_date")
        bank_transactions = filter_date(bank_transactions, "transaction_date")
        stock_movements = filter_date(stock_movements, "movement_date")

        if party_id:
            sales = sales.filter(party_id=party_id)
            purchases = purchases.filter(party_id=party_id)
            payments_in = payments_in.filter(party_id=party_id)
            payments_out = payments_out.filter(party_id=party_id)
        if item_id:
            sales = sales.filter(line_items__item_id=item_id).distinct()
            purchases = purchases.filter(line_items__item_id=item_id).distinct()
            stock_movements = stock_movements.filter(item_id=item_id)

        all_items = Item.objects.filter(business=business, is_active=True).select_related("category")
        items = all_items
        if selected_item:
            items = items.filter(id=selected_item.id)
        banks = BankAccount.objects.filter(business=business, is_active=True)
        all_parties = Party.objects.filter(business=business, is_active=True)
        parties = all_parties
        if selected_party:
            parties = parties.filter(id=selected_party.id)

        sales_total = sales.aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
        purchase_total = purchases.aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
        expense_total = expenses.aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
        output_gst = sum((row.cgst_amount + row.sgst_amount + row.igst_amount + row.cess_amount) for row in sales) or Decimal("0")
        input_gst = sum((row.cgst_amount + row.sgst_amount + row.igst_amount + row.cess_amount) for row in purchases) or Decimal("0")
        sales_taxable_total = sum((row.taxable_amount or row.subtotal) for row in sales) or Decimal("0")
        purchase_taxable_total = sum((row.taxable_amount or row.subtotal) for row in purchases) or Decimal("0")
        inventory_value = sum(item.current_stock * item.purchase_price for item in items) or Decimal("0")
        inventory_sale_value = sum(item.current_stock * item.selling_price for item in items) or Decimal("0")
        bank_balance = banks.aggregate(total=Sum("current_balance"))["total"] or Decimal("0")

        party_balances = []
        for party in parties:
            party_balances.append((party, PartySerializer().get_net_balance(party)))
        receivable = sum(max(0, balance) for _, balance in party_balances)
        payable = sum(abs(min(0, balance)) for _, balance in party_balances)
        active_filters = {
            "Date Range": date_range,
            "From": start_date.isoformat() if start_date else "Beginning",
            "To": end_date.isoformat() if end_date else "Today",
            "Party": selected_party.name if selected_party else "All Parties",
            "Item": selected_item.name if selected_item else "All Items",
        }

        sales_by_date = defaultdict(lambda: {"count": 0, "taxable": Decimal("0"), "gst": Decimal("0"), "total": Decimal("0")})
        for invoice in sales:
            bucket = sales_by_date[invoice.invoice_date]
            bucket["count"] += 1
            bucket["taxable"] += invoice.taxable_amount or invoice.subtotal
            bucket["gst"] += invoice.cgst_amount + invoice.sgst_amount + invoice.igst_amount + invoice.cess_amount
            bucket["total"] += invoice.total_amount

        purchase_by_party = defaultdict(lambda: {"count": 0, "paid": Decimal("0"), "unpaid": Decimal("0"), "total": Decimal("0")})
        for invoice in purchases:
            bucket = purchase_by_party[invoice.party.name]
            bucket["count"] += 1
            bucket["paid"] += invoice.paid_amount
            bucket["unpaid"] += max(Decimal("0"), invoice.total_amount - invoice.paid_amount)
            bucket["total"] += invoice.total_amount

        sales_lines = SalesInvoiceItem.objects.filter(invoice__in=sales).select_related("item", "invoice__party")
        purchase_lines = PurchaseInvoiceItem.objects.filter(invoice__in=purchases).select_related("item", "invoice__party")
        if item_id:
            sales_lines = sales_lines.filter(item_id=item_id)
            purchase_lines = purchase_lines.filter(item_id=item_id)

        hsn_sales = defaultdict(lambda: {"qty": Decimal("0"), "taxable": Decimal("0"), "tax": Decimal("0")})
        hsn_purchases = defaultdict(lambda: {"qty": Decimal("0"), "taxable": Decimal("0"), "tax": Decimal("0")})
        item_sales = defaultdict(lambda: {"qty": Decimal("0"), "sales": Decimal("0"), "cost": Decimal("0"), "tax": Decimal("0")})
        item_purchases = defaultdict(lambda: {"qty": Decimal("0"), "taxable": Decimal("0"), "tax": Decimal("0"), "total": Decimal("0")})
        party_profit = defaultdict(lambda: {"sales": Decimal("0"), "cost": Decimal("0"), "tax": Decimal("0"), "margin": Decimal("0")})
        tax_summary = defaultdict(lambda: {"sales_taxable": Decimal("0"), "sales_tax": Decimal("0"), "purchase_taxable": Decimal("0"), "purchase_tax": Decimal("0")})
        for line in sales_lines:
            hsn = line.hsn_code or (line.item.hsn_code if line.item else "NA") or "NA"
            hsn_sales[hsn]["qty"] += line.quantity
            hsn_sales[hsn]["taxable"] += line.taxable_amount
            hsn_sales[hsn]["tax"] += line.tax_amount
            item_key = line.item.name if line.item else line.item_name
            line_cost = (line.item.purchase_price if line.item else Decimal("0")) * line.quantity
            item_sales[item_key]["qty"] += line.quantity
            item_sales[item_key]["sales"] += line.taxable_amount
            item_sales[item_key]["tax"] += line.tax_amount
            item_sales[item_key]["cost"] += line_cost
            party_key = line.invoice.party.name
            party_profit[party_key]["sales"] += line.taxable_amount
            party_profit[party_key]["cost"] += line_cost
            party_profit[party_key]["tax"] += line.tax_amount
            party_profit[party_key]["margin"] += line.taxable_amount - line_cost
            tax_summary[str(line.gst_rate)]["sales_taxable"] += line.taxable_amount
            tax_summary[str(line.gst_rate)]["sales_tax"] += line.tax_amount
        for line in purchase_lines:
            hsn = (line.item.hsn_code if line.item else "NA") or "NA"
            purchase_tax = line.amount - line.taxable_amount
            hsn_purchases[hsn]["qty"] += line.quantity
            hsn_purchases[hsn]["taxable"] += line.taxable_amount
            hsn_purchases[hsn]["tax"] += purchase_tax
            item_key = line.item.name if line.item else line.item_name
            item_purchases[item_key]["qty"] += line.quantity
            item_purchases[item_key]["taxable"] += line.taxable_amount
            item_purchases[item_key]["tax"] += purchase_tax
            item_purchases[item_key]["total"] += line.amount
            tax_summary[str(line.gst_rate)]["purchase_taxable"] += line.taxable_amount
            tax_summary[str(line.gst_rate)]["purchase_tax"] += purchase_tax

        expense_by_category = defaultdict(lambda: {"count": 0, "total": Decimal("0"), "paid": Decimal("0")})
        for expense in expenses:
            bucket = expense_by_category[expense.expense_category]
            bucket["count"] += 1
            bucket["total"] += expense.total_amount
            bucket["paid"] += expense.paid_amount

        payments_by_mode = defaultdict(lambda: {"in": Decimal("0"), "out": Decimal("0")})
        for payment in payments_in:
            payments_by_mode[payment.payment_mode]["in"] += payment.amount_received
        for payment in payments_out:
            payments_by_mode[payment.payment_mode]["out"] += payment.amount_paid

        party_activity = defaultdict(
            lambda: {
                "name": "-",
                "type": "-",
                "opening": Decimal("0"),
                "sales": Decimal("0"),
                "receipts": Decimal("0"),
                "purchases": Decimal("0"),
                "payments": Decimal("0"),
                "closing": Decimal("0"),
            }
        )
        for party in parties:
            opening = party.opening_balance if party.opening_balance_type == "debit" else -party.opening_balance
            party_activity[party.id].update({
                "name": party.name,
                "type": party.party_type.title(),
                "opening": opening,
                "closing": opening,
            })
        for invoice in sales:
            party_activity[invoice.party_id]["sales"] += invoice.total_amount
            party_activity[invoice.party_id]["closing"] += invoice.total_amount
        for payment in payments_in:
            party_activity[payment.party_id]["receipts"] += payment.amount_received
            party_activity[payment.party_id]["closing"] -= payment.amount_received
        for invoice in purchases:
            party_activity[invoice.party_id]["purchases"] += invoice.total_amount
            party_activity[invoice.party_id]["closing"] -= invoice.total_amount
        for payment in payments_out:
            party_activity[payment.party_id]["payments"] += payment.amount_paid
            party_activity[payment.party_id]["closing"] += payment.amount_paid

        receivable_ageing = defaultdict(lambda: {"count": 0, "amount": Decimal("0")})
        for invoice in sales:
            outstanding = max(Decimal("0"), invoice.total_amount - invoice.paid_amount)
            if outstanding <= 0:
                continue
            days = max(0, (today - (invoice.due_date or invoice.invoice_date)).days)
            bucket = "0-30 Days" if days <= 30 else "31-60 Days" if days <= 60 else "61-90 Days" if days <= 90 else "90+ Days"
            receivable_ageing[bucket]["count"] += 1
            receivable_ageing[bucket]["amount"] += outstanding

        payable_ageing = defaultdict(lambda: {"count": 0, "amount": Decimal("0")})
        for invoice in purchases:
            outstanding = max(Decimal("0"), invoice.total_amount - invoice.paid_amount)
            if outstanding <= 0:
                continue
            days = max(0, (today - (invoice.due_date or invoice.invoice_date)).days)
            bucket = "0-30 Days" if days <= 30 else "31-60 Days" if days <= 60 else "61-90 Days" if days <= 90 else "90+ Days"
            payable_ageing[bucket]["count"] += 1
            payable_ageing[bucket]["amount"] += outstanding

        stock_by_category = defaultdict(lambda: {"qty": Decimal("0"), "purchase": Decimal("0"), "sale": Decimal("0")})
        for item in items:
            key = item.category.name if item.category else "-"
            stock_by_category[key]["qty"] += item.current_stock
            stock_by_category[key]["purchase"] += item.current_stock * item.purchase_price
            stock_by_category[key]["sale"] += item.current_stock * item.selling_price

        stock_by_godown = defaultdict(lambda: {"qty": Decimal("0"), "value": Decimal("0")})
        for stock in ItemGodownStock.objects.filter(business=business, item__in=items).select_related("godown", "item"):
            stock_by_godown[stock.godown.name]["qty"] += stock.current_stock
            stock_by_godown[stock.godown.name]["value"] += stock.current_stock * stock.item.purchase_price

        daybook_entries = []
        for invoice in sales.order_by("-invoice_date", "-created_at")[:40]:
            daybook_entries.append((invoice.invoice_date, [_date(invoice.invoice_date), "Sales Invoice", invoice.invoice_number, invoice.party.name, _inr(invoice.total_amount, 2)]))
        for invoice in purchases.order_by("-invoice_date", "-created_at")[:40]:
            daybook_entries.append((invoice.invoice_date, [_date(invoice.invoice_date), "Purchase Invoice", invoice.invoice_number, invoice.party.name, _inr(invoice.total_amount, 2)]))
        for payment in payments_in.order_by("-payment_date", "-created_at")[:40]:
            daybook_entries.append((payment.payment_date, [_date(payment.payment_date), "Payment In", payment.payment_number, payment.party.name, _inr(payment.amount_received, 2)]))
        for payment in payments_out.order_by("-payment_date", "-created_at")[:40]:
            daybook_entries.append((payment.payment_date, [_date(payment.payment_date), "Payment Out", payment.payment_number, payment.party.name, _inr(payment.amount_paid, 2)]))
        for expense in expenses.order_by("-expense_date", "-created_at")[:40]:
            daybook_entries.append((expense.expense_date, [_date(expense.expense_date), "Expense", expense.expense_number, expense.expense_category, _inr(expense.total_amount, 2)]))
        daybook_rows = [
            row
            for _, row in sorted(daybook_entries, key=lambda entry: entry[0] or date_cls.min, reverse=True)[:80]
        ]

        reports = [
            _report(
                "balance-sheet",
                "Balance Sheet",
                "Business",
                f"Assets, liabilities, and tenant capital position for {business.name}.",
                "Closing Balance",
                _inr((bank_balance + Decimal(str(receivable)) + inventory_value) - Decimal(str(payable)), 2),
                ["Particular", "Debit", "Credit"],
                [
                    ["Cash + Bank", _inr(bank_balance, 2), "-"],
                    ["Inventory Stock", _inr(inventory_value, 2), "-"],
                    ["Sundry Debtors", _inr(receivable, 2), "-"],
                    ["Sundry Creditors", "-", _inr(payable, 2)],
                ],
                favourite=True,
                filters=active_filters,
            ),
            _report(
                "profit-loss",
                "Profit And Loss Report",
                "Business",
                "Gross margin and operating result from sale, purchase, and expense registers.",
                "Gross Profit",
                _inr(sales_total - purchase_total - expense_total, 2),
                ["Particular", "Amount", "Margin"],
                [
                    ["Total Sales", _inr(sales_total, 2), "100%"],
                    ["Total Purchases", _inr(purchase_total, 2), _percent(purchase_total, sales_total)],
                    ["Direct Expenses", _inr(expense_total, 2), _percent(expense_total, sales_total)],
                    ["Gross Profit", _inr(sales_total - purchase_total - expense_total, 2), _percent(sales_total - purchase_total - expense_total, sales_total)],
                ],
                favourite=True,
                filters=active_filters,
            ),
            _report(
                "sales-summary",
                "Sales Summary",
                "Transaction",
                "Sales invoice totals with paid and unpaid split.",
                "Total Sales",
                _inr(sales_total, 2),
                ["Date", "Invoice Count", "Taxable", "GST", "Total"],
                [
                    [_date(day), str(values["count"]), _inr(values["taxable"], 2), _inr(values["gst"], 2), _inr(values["total"], 2)]
                    for day, values in sorted(sales_by_date.items(), reverse=True)
                ] or [["-", "0", _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                favourite=True,
                filters=active_filters,
            ),
            _report(
                "purchase-summary",
                "Purchase Summary",
                "Transaction",
                "Supplier purchase totals with paid and unpaid split.",
                "Total Purchases",
                _inr(purchase_total, 2),
                ["Supplier", "Invoices", "Total", "Paid", "Unpaid"],
                [
                    [supplier, str(values["count"]), _inr(values["total"], 2), _inr(values["paid"], 2), _inr(values["unpaid"], 2)]
                    for supplier, values in sorted(purchase_by_party.items())
                ] or [["-", "0", _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                filters=active_filters,
            ),
            _report(
                "sales-register",
                "Sales Register",
                "Transaction",
                "Invoice-wise sales register with payment status and tax breakup.",
                "Invoices",
                str(sales.count()),
                ["Date", "Invoice", "Party", "Taxable", "GST", "Discount", "Additional Charges", "Total", "Status"],
                [
                    [
                        _date(invoice.invoice_date),
                        invoice.invoice_number,
                        invoice.party.name,
                        _inr(invoice.taxable_amount or invoice.subtotal, 2),
                        _inr(invoice.cgst_amount + invoice.sgst_amount + invoice.igst_amount + invoice.cess_amount, 2),
                        _inr(invoice.discount_amount, 2),
                        _inr(invoice.additional_charges, 2),
                        _inr(invoice.total_amount, 2),
                        invoice.status.title(),
                    ]
                    for invoice in sales.order_by("-invoice_date", "-created_at")[:200]
                ] or [["-", "-", "-", _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2), "-"]],
                filters=active_filters,
            ),
            _report(
                "sales-tax-register",
                "Sales Tax Register",
                "GST",
                "Invoice-wise output tax register with CGST, SGST, IGST, cess, and taxable value.",
                "Output Tax",
                _inr(output_gst, 2),
                ["Date", "Invoice", "Party", "Taxable", "CGST", "SGST", "IGST", "Cess", "Total"],
                [
                    [
                        _date(invoice.invoice_date),
                        invoice.invoice_number,
                        invoice.party.name,
                        _inr(invoice.taxable_amount or invoice.subtotal, 2),
                        _inr(invoice.cgst_amount, 2),
                        _inr(invoice.sgst_amount, 2),
                        _inr(invoice.igst_amount, 2),
                        _inr(invoice.cess_amount, 2),
                        _inr(invoice.total_amount, 2),
                    ]
                    for invoice in sales.order_by("-invoice_date", "-created_at")[:200]
                ] or [["-", "-", "-", _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                badge="New",
                filters=active_filters,
            ),
            _report(
                "e-invoice-status",
                "E-Invoice Status Report",
                "GST",
                "IRN, acknowledgement, retry, and cancellation status for sales invoices.",
                "Generated",
                str(sales.filter(einvoice_status="generated").count()),
                ["Date", "Invoice", "Party", "IRN", "Ack No", "Status", "Retries", "Last Error"],
                [
                    [
                        _date(invoice.invoice_date),
                        invoice.invoice_number,
                        invoice.party.name,
                        invoice.irn or "-",
                        invoice.ack_number or "-",
                        invoice.einvoice_status.title(),
                        str(invoice.einvoice_retry_count),
                        invoice.einvoice_last_error or invoice.einvoice_cancel_reason or "-",
                    ]
                    for invoice in sales.order_by("-invoice_date", "-created_at")[:200]
                ] or [["-", "-", "-", "-", "-", "Pending", "0", "-"]],
                badge="New",
                filters=active_filters,
            ),
            _report(
                "purchase-register",
                "Purchase Register",
                "Transaction",
                "Supplier invoice register with input tax and payment status.",
                "Purchase Invoices",
                str(purchases.count()),
                ["Date", "Invoice", "Supplier Invoice", "Supplier", "Taxable", "GST", "Total", "Status"],
                [
                    [
                        _date(invoice.invoice_date),
                        invoice.invoice_number,
                        invoice.supplier_invoice_number or "-",
                        invoice.party.name,
                        _inr(invoice.taxable_amount or invoice.subtotal, 2),
                        _inr(invoice.cgst_amount + invoice.sgst_amount + invoice.igst_amount + invoice.cess_amount, 2),
                        _inr(invoice.total_amount, 2),
                        invoice.status.title(),
                    ]
                    for invoice in purchases.order_by("-invoice_date", "-created_at")[:200]
                ] or [["-", "-", "-", "-", _inr(0, 2), _inr(0, 2), _inr(0, 2), "-"]],
                filters=active_filters,
            ),
            _report(
                "purchase-tax-register",
                "Purchase Tax Register",
                "GST",
                "Supplier invoice-wise input tax credit register.",
                "Input Tax",
                _inr(input_gst, 2),
                ["Date", "Invoice", "Supplier", "Taxable", "CGST", "SGST", "IGST", "Cess", "Total"],
                [
                    [
                        _date(invoice.invoice_date),
                        invoice.invoice_number,
                        invoice.party.name,
                        _inr(invoice.taxable_amount or invoice.subtotal, 2),
                        _inr(invoice.cgst_amount, 2),
                        _inr(invoice.sgst_amount, 2),
                        _inr(invoice.igst_amount, 2),
                        _inr(invoice.cess_amount, 2),
                        _inr(invoice.total_amount, 2),
                    ]
                    for invoice in purchases.order_by("-invoice_date", "-created_at")[:200]
                ] or [["-", "-", "-", _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                badge="New",
                filters=active_filters,
            ),
            _report(
                "expense-summary",
                "Expense Summary",
                "Business",
                "Expense category totals from the tenant expense register.",
                "Total Expenses",
                _inr(expense_total, 2),
                ["Category", "Entries", "Total", "Paid", "Balance"],
                [
                    [category, str(values["count"]), _inr(values["total"], 2), _inr(values["paid"], 2), _inr(values["total"] - values["paid"], 2)]
                    for category, values in sorted(expense_by_category.items())
                ] or [["-", "0", _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                badge="New",
                filters=active_filters,
            ),
            _report(
                "expense-register",
                "Expense Register",
                "Business",
                "Expense voucher register with payment mode, reference, and paid balance.",
                "Expenses",
                str(expenses.count()),
                ["Date", "Expense No", "Category", "Mode", "Reference", "Total", "Paid", "Balance"],
                [
                    [
                        _date(expense.expense_date),
                        expense.expense_number,
                        expense.expense_category,
                        expense.payment_mode.title(),
                        expense.reference_number or "-",
                        _inr(expense.total_amount, 2),
                        _inr(expense.paid_amount, 2),
                        _inr(expense.total_amount - expense.paid_amount, 2),
                    ]
                    for expense in expenses.order_by("-expense_date", "-created_at")[:200]
                ] or [["-", "-", "-", "-", "-", _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                filters=active_filters,
            ),
            _report(
                "payment-in-report",
                "Payment In Report",
                "Transaction",
                "Customer receipts and settlement references.",
                "Received",
                _inr(sum(payment.amount_received for payment in payments_in), 2),
                ["Date", "Receipt", "Party", "Mode", "Reference", "Amount", "Status"],
                [
                    [
                        _date(payment.payment_date),
                        payment.payment_number,
                        payment.party.name,
                        payment.payment_mode.title(),
                        payment.reference_number or "-",
                        _inr(payment.amount_received, 2),
                        payment.status.title(),
                    ]
                    for payment in payments_in.order_by("-payment_date", "-created_at")[:200]
                ] or [["-", "-", "-", "-", "-", _inr(0, 2), "-"]],
                filters=active_filters,
            ),
            _report(
                "payment-out-report",
                "Payment Out Report",
                "Transaction",
                "Supplier payments and settlement references.",
                "Paid",
                _inr(sum(payment.amount_paid for payment in payments_out), 2),
                ["Date", "Payment", "Party", "Mode", "Reference", "Amount", "Status"],
                [
                    [
                        _date(payment.payment_date),
                        payment.payment_number,
                        payment.party.name,
                        payment.payment_mode.title(),
                        payment.reference_number or "-",
                        _inr(payment.amount_paid, 2),
                        payment.status.title(),
                    ]
                    for payment in payments_out.order_by("-payment_date", "-created_at")[:200]
                ] or [["-", "-", "-", "-", "-", _inr(0, 2), "-"]],
                filters=active_filters,
            ),
            _report(
                "daybook",
                "Daybook",
                "Transaction",
                "Sale, purchase, payment, and expense entries by date.",
                "Transactions",
                str(len(daybook_rows)),
                ["Date", "Voucher", "Number", "Party / Ledger", "Amount"],
                daybook_rows or [["-", "-", "-", "-", _inr(0, 2)]],
                filters=active_filters,
            ),
            _report(
                "payment-mode-summary",
                "Payment Mode Summary",
                "Transaction",
                "Cash, bank, UPI, and cheque movement across payment vouchers.",
                "Net Receipt",
                _inr(sum(row["in"] - row["out"] for row in payments_by_mode.values()), 2),
                ["Payment Mode", "Money In", "Money Out", "Net"],
                [
                    [mode.title(), _inr(values["in"], 2), _inr(values["out"], 2), _inr(values["in"] - values["out"], 2)]
                    for mode, values in sorted(payments_by_mode.items())
                ] or [["-", _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                filters=active_filters,
            ),
            _report(
                "cash-bank",
                "Cash and Bank Report (All Payments)",
                "Transaction",
                "Bank account transaction movement and current balances.",
                "Cash + Bank Balance",
                _inr(bank_balance, 2),
                ["Account", "Money In", "Money Out", "Balance"],
                [
                    [
                        bank.account_name,
                        _inr(sum(tx.amount for tx in bank_transactions.filter(bank_account=bank, transaction_type="deposit")), 2),
                        _inr(sum(tx.amount for tx in bank_transactions.filter(bank_account=bank, transaction_type="withdrawal")), 2),
                        _inr(bank.current_balance, 2),
                    ]
                    for bank in banks
                ] or [["Cash in hand", _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                filters=active_filters,
            ),
            _report(
                "bank-book",
                "Bank Book",
                "Transaction",
                "Bank and cash ledger entries by account with references.",
                "Entries",
                str(bank_transactions.count()),
                ["Date", "Account", "Type", "Reference", "Description", "Amount"],
                [
                    [
                        _date(transaction.transaction_date),
                        transaction.bank_account.account_name,
                        transaction.transaction_type.title(),
                        transaction.reference_number or "-",
                        transaction.description or "-",
                        _inr(transaction.amount, 2),
                    ]
                    for transaction in bank_transactions.order_by("-transaction_date", "-created_at")[:200]
                ] or [["-", "-", "-", "-", "-", _inr(0, 2)]],
                badge="New",
                filters=active_filters,
            ),
            _report(
                "cash-flow",
                "Cash Flow Summary",
                "Business",
                "Money-in and money-out summary from receipts, supplier payments, expenses, and bank entries.",
                "Net Cash Flow",
                _inr(
                    sum(payment.amount_received for payment in payments_in)
                    + sum(tx.amount for tx in bank_transactions if tx.transaction_type == "deposit")
                    - sum(payment.amount_paid for payment in payments_out)
                    - sum(expense.paid_amount for expense in expenses)
                    - sum(tx.amount for tx in bank_transactions if tx.transaction_type == "withdrawal"),
                    2,
                ),
                ["Particular", "Money In", "Money Out", "Net"],
                [
                    [
                        "Customer Receipts",
                        _inr(sum(payment.amount_received for payment in payments_in), 2),
                        "-",
                        _inr(sum(payment.amount_received for payment in payments_in), 2),
                    ],
                    [
                        "Supplier Payments",
                        "-",
                        _inr(sum(payment.amount_paid for payment in payments_out), 2),
                        _inr(-sum(payment.amount_paid for payment in payments_out), 2),
                    ],
                    [
                        "Expenses Paid",
                        "-",
                        _inr(sum(expense.paid_amount for expense in expenses), 2),
                        _inr(-sum(expense.paid_amount for expense in expenses), 2),
                    ],
                    [
                        "Bank Deposits",
                        _inr(sum(tx.amount for tx in bank_transactions if tx.transaction_type == "deposit"), 2),
                        "-",
                        _inr(sum(tx.amount for tx in bank_transactions if tx.transaction_type == "deposit"), 2),
                    ],
                    [
                        "Bank Withdrawals",
                        "-",
                        _inr(sum(tx.amount for tx in bank_transactions if tx.transaction_type == "withdrawal"), 2),
                        _inr(-sum(tx.amount for tx in bank_transactions if tx.transaction_type == "withdrawal"), 2),
                    ],
                ],
                badge="New",
                filters=active_filters,
            ),
            _report(
                "gstr-1",
                "GSTR-1 (Sales)",
                "GST",
                "Outward taxable supplies by invoice and HSN.",
                "Output GST",
                _inr(output_gst, 2),
                ["Section", "Taxable Value", "CGST", "SGST", "IGST", "Cess"],
                [
                    ["Sales", _inr(sales_taxable_total, 2), _inr(sum(row.cgst_amount for row in sales), 2), _inr(sum(row.sgst_amount for row in sales), 2), _inr(sum(row.igst_amount for row in sales), 2), _inr(sum(row.cess_amount for row in sales), 2)],
                    *[
                        [f"HSN {hsn}", _inr(values["taxable"], 2), "-", "-", "-", "-"]
                        for hsn, values in sorted(hsn_sales.items())
                    ],
                ],
                filters=active_filters,
            ),
            _report(
                "gstr-2",
                "GSTR-2 (Purchase)",
                "GST",
                "Purchase-side GST and input tax credit summary.",
                "Input GST",
                _inr(input_gst, 2),
                ["Supplier", "Invoice", "Taxable Value", "CGST", "SGST", "Total"],
                [
                    [invoice.party.name, invoice.invoice_number, _inr(invoice.taxable_amount or invoice.subtotal, 2), _inr(invoice.cgst_amount, 2), _inr(invoice.sgst_amount, 2), _inr(invoice.total_amount, 2)]
                    for invoice in purchases.order_by("-invoice_date", "-created_at")[:80]
                ] or [["-", "-", _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                filters=active_filters,
            ),
            _report(
                "gstr-3b",
                "GSTR-3b",
                "GST",
                "GST liability snapshot for the selected report period.",
                "Net Payable",
                _inr(output_gst - input_gst, 2),
                ["Type", "Taxable", "Tax", "Status"],
                [
                    ["Outward Supplies", _inr(sales_taxable_total, 2), _inr(output_gst, 2), "Ready"],
                    ["Input Tax Credit", _inr(purchase_taxable_total, 2), _inr(input_gst, 2), "Matched"],
                    ["Net Liability", "-", _inr(output_gst - input_gst, 2), "Payable" if output_gst >= input_gst else "Credit"],
                ],
                filters=active_filters,
            ),
            _report(
                "tax-summary",
                "GST / Tax Summary",
                "GST",
                "Rate-wise output tax, input tax, and net liability.",
                "Net GST",
                # Sum the same rate-wise rows this report actually displays,
                # not the separate invoice-header output_gst/input_gst
                # aggregates - those are rounded once per invoice total,
                # while these rows sum each line's own already-rounded
                # tax_amount, so the two totals can drift a paisa or two
                # apart on multi-line invoices. Keep this report's own
                # header self-consistent with its own rows.
                _inr(
                    sum((v["sales_tax"] for v in tax_summary.values()), Decimal("0"))
                    - sum((v["purchase_tax"] for v in tax_summary.values()), Decimal("0")),
                    2,
                ),
                ["GST Rate", "Sales Taxable", "Output Tax", "Purchase Taxable", "Input Tax", "Net"],
                [
                    [f"{rate}%", _inr(values["sales_taxable"], 2), _inr(values["sales_tax"], 2), _inr(values["purchase_taxable"], 2), _inr(values["purchase_tax"], 2), _inr(values["sales_tax"] - values["purchase_tax"], 2)]
                    for rate, values in sorted(tax_summary.items(), key=lambda row: Decimal(row[0]))
                ] or [["-", _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                favourite=True,
                badge="New",
                filters=active_filters,
            ),
            _report(
                "gst-sales-hsn",
                "GST Sales (With HSN)",
                "GST",
                "HSN-wise taxable sale report.",
                "HSN Lines",
                str(len(hsn_sales)),
                ["HSN", "Qty", "Taxable", "Tax"],
                [
                    [hsn, f"{_num(values['qty']):g} PCS", _inr(values["taxable"], 2), _inr(values["tax"], 2)]
                    for hsn, values in sorted(hsn_sales.items())
                ] or [["-", "0 PCS", _inr(0, 2), _inr(0, 2)]],
                filters=active_filters,
            ),
            _report(
                "hsn-summary",
                "HSN Summary",
                "GST",
                "Combined HSN summary across sales and purchases for GST review.",
                "HSN Codes",
                str(len(set(hsn_sales.keys()) | set(hsn_purchases.keys()))),
                ["HSN", "Sales Qty", "Sales Taxable", "Sales Tax", "Purchase Qty", "Purchase Taxable", "Purchase Tax"],
                [
                    [
                        hsn,
                        f"{_num(hsn_sales[hsn]['qty']):g} PCS",
                        _inr(hsn_sales[hsn]["taxable"], 2),
                        _inr(hsn_sales[hsn]["tax"], 2),
                        f"{_num(hsn_purchases[hsn]['qty']):g} PCS",
                        _inr(hsn_purchases[hsn]["taxable"], 2),
                        _inr(hsn_purchases[hsn]["tax"], 2),
                    ]
                    for hsn in sorted(set(hsn_sales.keys()) | set(hsn_purchases.keys()))
                ] or [["-", "0 PCS", _inr(0, 2), _inr(0, 2), "0 PCS", _inr(0, 2), _inr(0, 2)]],
                favourite=True,
                badge="New",
                filters=active_filters,
            ),
            _report(
                "stock-summary",
                "Stock Summary",
                "Item",
                "Closing stock quantity and stock value by category.",
                "Stock Value",
                _inr(inventory_value, 2),
                ["Category", "Stock Qty", "Purchase Value", "Selling Value"],
                [
                    [category, f"{_num(values['qty']):g} PCS", _inr(values["purchase"], 2), _inr(values["sale"], 2)]
                    for category, values in sorted(stock_by_category.items())
                ] or [["-", "0 PCS", _inr(0, 2), _inr(0, 2)]],
                favourite=True,
                filters=active_filters,
            ),
            _report(
                "godown-stock",
                "Godown Stock Summary",
                "Item",
                "Per-godown closing quantity and stock value.",
                "Godowns",
                str(len(stock_by_godown)),
                ["Godown", "Stock Qty", "Stock Value"],
                [
                    [godown, f"{_num(values['qty']):g} PCS", _inr(values["value"], 2)]
                    for godown, values in sorted(stock_by_godown.items())
                ] or [["-", "0 PCS", _inr(0, 2)]],
                badge="New",
                filters=active_filters,
            ),
            _report(
                "stock-ledger",
                "Stock Ledger",
                "Item",
                "Chronological stock movement ledger across sales, purchases, returns, adjustments, and transfers.",
                "Movements",
                str(stock_movements.count()),
                ["Date", "Item", "Godown", "Movement", "Qty", "Rate", "Balance After", "Reference"],
                [
                    [
                        _date(movement.movement_date),
                        movement.item.name,
                        movement.godown.name if movement.godown else "-",
                        _movement_label(movement.movement_type),
                        f"{_num(movement.quantity):g} {movement.item.unit}",
                        _inr(movement.rate or 0, 2),
                        f"{_num(movement.balance_after):g} {movement.item.unit}",
                        movement.reference_type or "-",
                    ]
                    for movement in stock_movements.order_by("-movement_date", "-created_at")[:200]
                ] or [["-", "-", "-", "-", "0 PCS", _inr(0, 2), "0 PCS", "-"]],
                badge="New",
                filters=active_filters,
            ),
            _report(
                "stock-detail",
                "Stock Detail Report",
                "Item",
                "Item-wise stock movement with SKU and HSN details.",
                "Tracked SKUs",
                str(items.count()),
                ["Item Name", "SKU Code", "HSN", "Current Stock", "Stock Value"],
                [
                    [item.name, item.item_code or "-", item.hsn_code or "-", f"{_num(item.current_stock):g} {item.unit}", _inr(item.current_stock * item.purchase_price, 2)]
                    for item in items.order_by("name")[:200]
                ] or [["-", "-", "-", "0 PCS", _inr(0, 2)]],
                filters=active_filters,
            ),
            _report(
                "stock-valuation",
                "Stock Valuation Report",
                "Item",
                "Item-wise purchase valuation, sale valuation, and estimated markup on closing stock.",
                "Purchase Value",
                _inr(inventory_value, 2),
                ["Item", "SKU", "Qty", "Purchase Value", "Sale Value", "Markup"],
                [
                    [
                        item.name,
                        item.item_code or "-",
                        f"{_num(item.current_stock):g} {item.unit}",
                        _inr(item.current_stock * item.purchase_price, 2),
                        _inr(item.current_stock * item.selling_price, 2),
                        _inr((item.current_stock * item.selling_price) - (item.current_stock * item.purchase_price), 2),
                    ]
                    for item in items.order_by("name")[:200]
                ] or [["-", "-", "0 PCS", _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                favourite=True,
                badge="New",
                filters=active_filters,
            ),
            _report(
                "item-sales",
                "Item Sales Report",
                "Item",
                "Item-wise quantity, sales value, tax, and margin from sales invoices.",
                "Sold Items",
                str(len(item_sales)),
                ["Item", "Qty", "Sales", "Cost", "Margin", "Tax"],
                [
                    [item, f"{_num(values['qty']):g} PCS", _inr(values["sales"], 2), _inr(values["cost"], 2), _inr(values["sales"] - values["cost"], 2), _inr(values["tax"], 2)]
                    for item, values in sorted(item_sales.items())
                ] or [["-", "0 PCS", _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                filters=active_filters,
            ),
            _report(
                "item-purchase",
                "Item Purchase Report",
                "Item",
                "Item-wise purchase quantity, taxable value, input tax, and gross value.",
                "Purchased Items",
                str(len(item_purchases)),
                ["Item", "Qty", "Taxable", "Input Tax", "Total"],
                [
                    [
                        item,
                        f"{_num(values['qty']):g} PCS",
                        _inr(values["taxable"], 2),
                        _inr(values["tax"], 2),
                        _inr(values["total"], 2),
                    ]
                    for item, values in sorted(item_purchases.items())
                ] or [["-", "0 PCS", _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                badge="New",
                filters=active_filters,
            ),
            _report(
                "item-profitability",
                "Item Profitability Report",
                "Item",
                "Item-wise sales margin using stored purchase price snapshots from inventory.",
                "Gross Margin",
                _inr(sum(values["sales"] - values["cost"] for values in item_sales.values()), 2),
                ["Item", "Sales", "Cost", "Margin", "Margin %"],
                [
                    [
                        item,
                        _inr(values["sales"], 2),
                        _inr(values["cost"], 2),
                        _inr(values["sales"] - values["cost"], 2),
                        _percent(values["sales"] - values["cost"], values["sales"]),
                    ]
                    for item, values in sorted(item_sales.items())
                ] or [["-", _inr(0, 2), _inr(0, 2), _inr(0, 2), "0%"]],
                favourite=True,
                badge="New",
                filters=active_filters,
            ),
            _report(
                "low-stock",
                "Low Stock Summary",
                "Item",
                "Items below configured stock warning levels.",
                "Low Stock Items",
                str(sum(1 for item in items if item.low_stock_qty is not None and item.current_stock <= item.low_stock_qty)),
                ["Item", "Current Stock", "Warning Qty", "Status"],
                [
                    [
                        item.name,
                        f"{_num(item.current_stock):g} {item.unit}",
                        f"{_num(item.low_stock_qty):g}" if item.low_stock_qty is not None else "-",
                        "Low" if item.low_stock_qty is not None and item.current_stock <= item.low_stock_qty else "Warning Disabled",
                    ]
                    for item in items.order_by("name")[:200]
                ] or [["-", "0 PCS", "-", "-"]],
                filters=active_filters,
            ),
            _report(
                "party-ledger",
                "Party Ledger",
                "Party",
                "Customer and supplier balance movement.",
                "Net Receivable",
                _inr(Decimal(str(receivable)) - Decimal(str(payable)), 2),
                ["Party", "Type", "Balance", "State"],
                [
                    [party.name, party.party_type.title(), _inr(balance, 2), party.state or "-"]
                    for party, balance in party_balances[:200]
                ] or [["-", "-", _inr(0, 2), "-"]],
                favourite=True,
                filters=active_filters,
            ),
            _report(
                "party-statement",
                "Party Statement (Ledger)",
                "Party",
                "Party-wise opening balance, sales, receipts, purchases, payments, and closing balance.",
                "Parties",
                str(len(party_activity)),
                ["Party", "Type", "Opening", "Sales", "Receipts", "Purchases", "Payments", "Closing"],
                [
                    [
                        values["name"],
                        values["type"],
                        _inr(values["opening"], 2),
                        _inr(values["sales"], 2),
                        _inr(values["receipts"], 2),
                        _inr(values["purchases"], 2),
                        _inr(values["payments"], 2),
                        _inr(values["closing"], 2),
                    ]
                    for values in sorted(party_activity.values(), key=lambda row: row["name"])[:200]
                ] or [["-", "-", _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                filters=active_filters,
            ),
            _report(
                "party-wise-profit",
                "Party Wise Profit Report",
                "Party",
                "Customer-wise gross sales margin based on sold item purchase prices.",
                "Gross Margin",
                _inr(sum(values["margin"] for values in party_profit.values()), 2),
                ["Party", "Sales", "Cost", "Margin", "Output Tax"],
                [
                    [
                        party,
                        _inr(values["sales"], 2),
                        _inr(values["cost"], 2),
                        _inr(values["margin"], 2),
                        _inr(values["tax"], 2),
                    ]
                    for party, values in sorted(party_profit.items())
                ] or [["-", _inr(0, 2), _inr(0, 2), _inr(0, 2), _inr(0, 2)]],
                badge="New",
                filters=active_filters,
            ),
            _report(
                "outstanding",
                "Receivable / Payable Summary",
                "Party",
                "Outstanding party balances calculated from invoices and payments.",
                "To Collect",
                _inr(receivable, 2),
                ["Particular", "Amount", "Count"],
                [
                    ["To Collect", _inr(receivable, 2), str(sum(1 for _, value in party_balances if value > 0))],
                    ["To Pay", _inr(payable, 2), str(sum(1 for _, value in party_balances if value < 0))],
                ],
                filters=active_filters,
            ),
            _report(
                "receivable-ageing",
                "Receivable Ageing Report",
                "Party",
                "Customer receivables grouped by overdue bucket.",
                "To Collect",
                _inr(receivable, 2),
                ["Bucket", "Invoices", "Amount"],
                [
                    [bucket, str(receivable_ageing[bucket]["count"]), _inr(receivable_ageing[bucket]["amount"], 2)]
                    for bucket in ["0-30 Days", "31-60 Days", "61-90 Days", "90+ Days"]
                ],
                badge="New",
                filters=active_filters,
            ),
            _report(
                "payable-ageing",
                "Payable Ageing Report",
                "Party",
                "Supplier payables grouped by overdue bucket.",
                "To Pay",
                _inr(payable, 2),
                ["Bucket", "Invoices", "Amount"],
                [
                    [bucket, str(payable_ageing[bucket]["count"]), _inr(payable_ageing[bucket]["amount"], 2)]
                    for bucket in ["0-30 Days", "31-60 Days", "61-90 Days", "90+ Days"]
                ],
                filters=active_filters,
            ),
        ]

        return {
            "success": True,
            "dateRanges": ["Last 365 Days", "Last 30 Days", "This Month", "Last Month", "This Quarter", "All Time", "Custom"],
            "activeFilters": active_filters,
            "generatedAt": timezone.now().isoformat(),
            "parties": [{"id": str(party.id), "name": party.name, "type": party.party_type} for party in all_parties.order_by("name")],
            "items": [{"id": str(item.id), "name": item.name, "code": item.item_code or ""} for item in all_items.order_by("name")],
            "reports": reports,
        }, None

    def get(self, request):
        payload, error_response = self._build_payload(request)
        if error_response:
            return error_response
        return Response(payload)


class ReportExportView(ReportsView):
    def get(self, request):
        payload, error_response = self._build_payload(request)
        if error_response:
            return error_response

        report_id = request.query_params.get("report") or request.query_params.get("id")
        export_format = request.query_params.get("export_format") or request.query_params.get("fmt") or "csv"
        report = next((row for row in payload["reports"] if row["id"] == report_id), None)
        if not report:
            return Response(
                {"success": False, "message": "Report not found for this tenant and filter set"},
                status=status.HTTP_404_NOT_FOUND,
            )

        return _report_export_response(report, request.business, export_format)


class ReportShareView(ReportsView):
    def post(self, request):
        payload, error_response = self._build_payload(request)
        if error_response:
            return error_response

        report_id = (request.data.get("report") or request.data.get("report_id") or "").strip()
        recipient = (request.data.get("recipient") or "").strip()
        if not report_id:
            return Response(
                {"success": False, "message": "Report is required before sharing"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not recipient:
            return Response(
                {"success": False, "message": "CA email or mobile is required before sharing"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        report = next((row for row in payload["reports"] if row["id"] == report_id), None)
        if not report:
            return Response(
                {"success": False, "message": "Report not found for this tenant and filter set"},
                status=status.HTTP_404_NOT_FOUND,
            )

        filters = report.get("filters") or {}
        stored_filters = {**filters, "_queryParams": _share_query_params(request)}
        share = ReportShare.objects.create(
            business=request.business,
            report_id=report["id"],
            report_name=report["name"],
            recipient=recipient,
            date_range=filters.get("Date Range", ""),
            filters=stored_filters,
            created_by=request.user if request.user and request.user.is_authenticated else None,
        )
        delivery = _send_report_share_email(
            request,
            [share],
            title=f"{request.business.name} - {report['name']}",
            intro=f"{request.business.name} shared a read-only report link with you.",
        )
        _store_share_delivery(share, delivery)
        write_activity(
            business=request.business,
            user=request.user,
            action="report_shared",
            entity_type="report_share",
            entity_id=share.id,
            details={
                "report": share.report_id,
                "recipient": recipient,
                "status": share.status,
                "provider": delivery.provider,
                "delivered": delivery.delivered,
            },
        )
        return Response(
            {
                "success": True,
                "message": delivery.message,
                "share": {
                    "id": str(share.id),
                    "reportId": share.report_id,
                    "reportName": share.report_name,
                    "recipient": share.recipient,
                    "status": share.status,
                    "shareToken": share.share_token,
                    "createdAt": share.created_at.isoformat(),
                    "delivery": delivery.as_dict(),
                },
            },
            status=status.HTTP_201_CREATED,
        )


class SharedReportView(ReportsView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [TenantScopedRateThrottle]
    throttle_scope = "public_share"

    class _SharedRequest:
        def __init__(self, business, query_params):
            self.business = business
            self.query_params = query_params

    def get(self, request, share_token):
        share = (
            ReportShare.objects.select_related("business")
            .filter(share_token=share_token)
            .exclude(status="revoked")
            .first()
        )
        if not share:
            return Response(
                {"success": False, "message": "Report share link is invalid or revoked"},
                status=status.HTTP_404_NOT_FOUND,
            )

        query_params = dict((share.filters or {}).get("_queryParams") or {})
        if share.date_range and "date_range" not in query_params:
            query_params["date_range"] = share.date_range
        shared_request = self._SharedRequest(share.business, query_params)
        payload, error_response = self._build_payload(shared_request)
        if error_response:
            return error_response

        report = next((row for row in payload["reports"] if row["id"] == share.report_id), None)
        if not report:
            return Response(
                {"success": False, "message": "Shared report no longer exists"},
                status=status.HTTP_404_NOT_FOUND,
            )

        export_format = request.query_params.get("export_format") or request.query_params.get("fmt") or "html"
        if export_format == "json":
            return Response({"success": True, "report": report})
        return _report_export_response(report, share.business, export_format)


class BankAccountViewSet(viewsets.ModelViewSet):
    serializer_class = BankAccountSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return BankAccount.objects.none()
        return BankAccount.objects.filter(business=self.request.business)

    @action(detail=False, methods=["post"])
    def transfer(self, request):
        if not request.business:
            return Response({"success": False, "message": "No active business"}, status=status.HTTP_404_NOT_FOUND)

        from_account_id = request.data.get("from_account")
        to_account_id = request.data.get("to_account")
        amount = Decimal(str(request.data.get("amount", 0)))
        description = request.data.get("description") or "Bank transfer"

        if not from_account_id or not to_account_id:
            return Response({"success": False, "message": "Source and destination accounts are required"}, status=status.HTTP_400_BAD_REQUEST)
        if from_account_id == to_account_id:
            return Response({"success": False, "message": "Choose two different accounts"}, status=status.HTTP_400_BAD_REQUEST)
        if amount <= 0:
            return Response({"success": False, "message": "Amount must be greater than zero"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            accounts = {
                str(account.id): account
                for account in BankAccount.objects.select_for_update().filter(
                    business=request.business,
                    id__in=[from_account_id, to_account_id],
                    is_active=True,
                )
            }
            from_account = accounts.get(str(from_account_id))
            to_account = accounts.get(str(to_account_id))
            if not from_account or not to_account:
                return Response({"success": False, "message": "Account not found"}, status=status.HTTP_404_NOT_FOUND)

            from_account.current_balance -= amount
            to_account.current_balance += amount
            from_account.save()
            to_account.save()

            withdrawal = BankTransaction.objects.create(
                business=request.business,
                bank_account=from_account,
                transaction_type="withdrawal",
                amount=amount,
                reference_number=request.data.get("reference_number") or None,
                description=f"Transfer to {to_account.account_name}: {description}",
            )
            deposit = BankTransaction.objects.create(
                business=request.business,
                bank_account=to_account,
                transaction_type="deposit",
                amount=amount,
                reference_number=request.data.get("reference_number") or None,
                description=f"Transfer from {from_account.account_name}: {description}",
            )

        return Response({
            "success": True,
            "withdrawal": BankTransactionSerializer(withdrawal).data,
            "deposit": BankTransactionSerializer(deposit).data,
        })

class BankTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = BankTransactionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return BankTransaction.objects.none()
        
        queryset = BankTransaction.objects.filter(business=self.request.business)
        bank_account = self.request.query_params.get("bank_account")
        if bank_account:
            queryset = queryset.filter(bank_account_id=bank_account)
            
        return queryset.order_by("-created_at")

    def destroy(self, request, *args, **kwargs):
        transaction_obj = self.get_object()
        with transaction.atomic():
            transaction_obj = BankTransaction.objects.select_for_update().get(
                id=transaction_obj.id,
                business=request.business,
            )
            account = BankAccount.objects.select_for_update().get(
                id=transaction_obj.bank_account_id,
                business=request.business,
            )
            apply_bank_transaction_balance(
                account,
                transaction_obj.transaction_type,
                transaction_obj.amount,
                reverse=True,
            )
            transaction_obj.delete()
        return Response({"success": True, "message": "Bank transaction deleted and balance reversed."})

class ExpenseViewSet(viewsets.ModelViewSet):
    serializer_class = ExpenseSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return Expense.objects.none()
        
        queryset = Expense.objects.filter(business=self.request.business)
        category = self.request.query_params.get("category")
        if category:
            queryset = queryset.filter(expense_category=category)
            
        return queryset.order_by("-created_at")

    @action(detail=False, methods=["get"])
    def category_totals(self, request):
        """Returns summarized totals grouped by expense category."""
        if not request.business:
            return Response({"success": False, "message": "No active business"})
            
        # Group and sum total amounts by category
        totals = Expense.objects.filter(
            business=request.business
        ).values("expense_category").annotate(total=Sum("total_amount")).order_by("-total")
        
        return Response({
            "success": True,
            "data": totals
        })

class AutomatedBillViewSet(viewsets.ModelViewSet):
    serializer_class = AutomatedBillSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return AutomatedBill.objects.none()
        return AutomatedBill.objects.filter(business=self.request.business)
