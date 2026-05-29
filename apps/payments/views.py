from decimal import Decimal
from html import escape
from django.http import HttpResponse
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import transaction
from django.utils import timezone
from .models import PaymentIn, PaymentOut
from .serializers import (
    PaymentInSerializer, PaymentOutSerializer,
    _reverse_payment_in_settlements, _reverse_payment_out_settlements
)
from apps.accounts.activity import write_activity

RUPEE = "\u20b9"


def _inr(value):
    amount = Decimal(str(value or 0))
    sign = "- " if amount < 0 else ""
    raw = f"{abs(amount):.2f}"
    whole, fraction = raw.split(".")
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
    return f"{sign}{RUPEE} {whole}.{fraction}"


def _date(value):
    return value.strftime("%d %b %Y") if value else "-"


def _write_payment_activity(request, action, entity_type, payment, details):
    write_activity(
        business=request.business,
        user=request.user,
        action=action,
        entity_type=entity_type,
        entity_id=payment.id,
        details={
            "paymentNumber": payment.payment_number,
            "party": payment.party.name,
            "status": payment.status,
            **details,
        },
    )


def _payment_status(payment, paid_label):
    return "Void" if payment.status == "void" else paid_label


def _settlement_rows(payment):
    return [
        {
            "invoice": settlement.invoice.invoice_number,
            "amount": settlement.settled_amount,
        }
        for settlement in payment.settlements.select_related("invoice")
    ]


def _receipt_text(*, business, title, number_label, number, party_label, party_name, date, amount_label, amount, mode, reference, status_label, settlements, notes, cancellation_reason):
    lines = [
        business.name,
        f"{title} {number}",
        f"{number_label}: {number}",
        f"{party_label}: {party_name}",
        f"Date: {_date(date)}",
        f"{amount_label}: {_inr(amount)}",
        f"Mode: {mode.title()}",
        f"Reference: {reference or '-'}",
        f"Status: {status_label}",
    ]
    if settlements:
        lines.append("Settlements:")
        for settlement in settlements:
            lines.append(f"- {settlement['invoice']}: {_inr(settlement['amount'])}")
    if notes:
        lines.append(f"Notes: {notes}")
    if cancellation_reason:
        lines.append(f"Void reason: {cancellation_reason}")
    return "\n".join(lines)


def _receipt_html(*, business, title, number_label, number, party_label, party_name, date, amount_label, amount, mode, reference, status_label, settlements, notes, cancellation_reason):
    settlement_rows = "".join(
        f"<tr><td>{escape(row['invoice'])}</td><td class='right'>{escape(_inr(row['amount']))}</td></tr>"
        for row in settlements
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{escape(title)} {escape(number)}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: #20242b; font: 14px Arial, sans-serif; background: #fff; }}
    .sheet {{ width: 760px; margin: 24px auto; border: 1px solid #d8dde8; border-radius: 8px; overflow: hidden; }}
    header {{ display: flex; justify-content: space-between; gap: 24px; padding: 22px 26px; border-bottom: 1px solid #e6e9f2; }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    h2 {{ margin: 0 0 4px; font-size: 17px; }}
    p {{ margin: 0; color: #667085; }}
    .badge {{ display: inline-block; padding: 4px 10px; border-radius: 999px; color: #22543d; background: #dff7e8; font-weight: 700; }}
    .badge.void {{ color: #7f1d1d; background: #fee2e2; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border-bottom: 1px solid #e6e9f2; }}
    .field {{ padding: 14px 26px; border-right: 1px solid #edf0f6; border-bottom: 1px solid #edf0f6; min-height: 62px; }}
    .field span {{ display: block; color: #667085; font-size: 12px; margin-bottom: 6px; }}
    .field strong {{ font-size: 15px; }}
    table {{ width: calc(100% - 52px); margin: 20px 26px; border-collapse: collapse; }}
    th, td {{ padding: 10px 12px; border: 1px solid #e1e5ef; text-align: left; }}
    th {{ background: #f5f6fa; color: #4b5565; }}
    .right {{ text-align: right; }}
    .total {{ display: flex; justify-content: flex-end; padding: 0 26px 24px; font-size: 17px; font-weight: 800; }}
    footer {{ padding: 14px 26px; color: #667085; border-top: 1px solid #e6e9f2; }}
    .actions {{ width: 760px; margin: 18px auto -8px; }}
    .actions button {{ border: 1px solid #cfd6e4; background: white; border-radius: 6px; padding: 8px 12px; font-weight: 700; }}
    @media print {{ .actions {{ display: none; }} body {{ background: #fff; }} .sheet {{ margin: 0; width: 100%; border: 0; }} }}
  </style>
</head>
<body>
  <div class="actions"><button onclick="window.print()">Print / Save PDF</button></div>
  <article class="sheet">
    <header>
      <div>
        <h2>{escape(business.name)}</h2>
        <p>{escape(business.gstin or "")}</p>
        <p>{escape(business.phone or "")}</p>
      </div>
      <span class="badge {escape(status_label.lower())}">{escape(status_label)}</span>
    </header>
    <section class="grid">
      <div class="field"><span>{escape(number_label)}</span><strong>{escape(number)}</strong></div>
      <div class="field"><span>Date</span><strong>{escape(_date(date))}</strong></div>
      <div class="field"><span>{escape(party_label)}</span><strong>{escape(party_name)}</strong></div>
      <div class="field"><span>{escape(amount_label)}</span><strong>{escape(_inr(amount))}</strong></div>
      <div class="field"><span>Payment Mode</span><strong>{escape(mode.title())}</strong></div>
      <div class="field"><span>Reference</span><strong>{escape(reference or "-")}</strong></div>
    </section>
    {f"<table><thead><tr><th>Invoice</th><th class='right'>Settled Amount</th></tr></thead><tbody>{settlement_rows}</tbody></table>" if settlements else ""}
    <div class="total">{escape(_inr(amount))}</div>
    {f"<footer>{escape(cancellation_reason or notes)}</footer>" if cancellation_reason or notes else ""}
  </article>
</body>
</html>"""


def _receipt_response(request, payment, *, title, number_label, number, party_label, party_name, date, amount_label, amount, paid_label):
    settlements = _settlement_rows(payment)
    status_label = _payment_status(payment, paid_label)
    payload = {
        "business": request.business,
        "title": title,
        "number_label": number_label,
        "number": number,
        "party_label": party_label,
        "party_name": party_name,
        "date": date,
        "amount_label": amount_label,
        "amount": amount,
        "mode": payment.payment_mode,
        "reference": payment.reference_number,
        "status_label": status_label,
        "settlements": settlements,
        "notes": payment.notes or "",
        "cancellation_reason": payment.cancellation_reason or "",
    }
    export_format = request.query_params.get("export_format") or request.query_params.get("fmt") or "html"
    if export_format == "text":
        return HttpResponse(_receipt_text(**payload), content_type="text/plain; charset=utf-8")
    return HttpResponse(_receipt_html(**payload), content_type="text/html; charset=utf-8")


class PaymentDeleteBlockedMixin:
    def destroy(self, request, *args, **kwargs):
        return Response(
            {"success": False, "message": "Use void so settlements and balances are reversed safely."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )


class PaymentInViewSet(PaymentDeleteBlockedMixin, viewsets.ModelViewSet):
    serializer_class = PaymentInSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return PaymentIn.objects.none()
        
        queryset = (
            PaymentIn.objects.filter(business=self.request.business)
            .select_related("party")
            .prefetch_related("settlements__invoice")
        )
        party = self.request.query_params.get("party")
        if party:
            queryset = queryset.filter(party_id=party)
            
        return queryset.order_by("-created_at")

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        payment = self.get_object()
        if payment.status == "void":
            return Response({"success": False, "message": "Payment is already void"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            payment = PaymentIn.objects.select_for_update().get(id=payment.id, business=request.business)
            _reverse_payment_in_settlements(payment)
            payment.status = "void"
            payment.is_advance = False
            payment.cancellation_reason = request.data.get("reason", "Voided by user request")
            payment.cancelled_at = timezone.now()
            payment.cancelled_by = request.user
            payment.save(update_fields=["status", "is_advance", "cancellation_reason", "cancelled_at", "cancelled_by"])
            _write_payment_activity(
                request,
                "payment_in_voided",
                "payment_in",
                payment,
                {
                    "amount": payment.amount_received,
                    "reason": payment.cancellation_reason,
                    "settlementsReversed": True,
                },
            )

        return Response({"success": True, "message": "Payment-in voided and settlements reversed successfully"})

    @action(detail=True, methods=["get"])
    def receipt(self, request, pk=None):
        payment = self.get_object()
        return _receipt_response(
            request,
            payment,
            title="Payment Receipt",
            number_label="Payment Number",
            number=payment.payment_number,
            party_label="Customer",
            party_name=payment.party.name,
            date=payment.payment_date,
            amount_label="Amount Received",
            amount=payment.amount_received,
            paid_label="Received",
        )

class PaymentOutViewSet(PaymentDeleteBlockedMixin, viewsets.ModelViewSet):
    serializer_class = PaymentOutSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return PaymentOut.objects.none()
        
        queryset = (
            PaymentOut.objects.filter(business=self.request.business)
            .select_related("party")
            .prefetch_related("settlements__invoice")
        )
        party = self.request.query_params.get("party")
        if party:
            queryset = queryset.filter(party_id=party)
            
        return queryset.order_by("-created_at")

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        payment = self.get_object()
        if payment.status == "void":
            return Response({"success": False, "message": "Payment is already void"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            payment = PaymentOut.objects.select_for_update().get(id=payment.id, business=request.business)
            _reverse_payment_out_settlements(payment)
            payment.status = "void"
            payment.is_advance = False
            payment.cancellation_reason = request.data.get("reason", "Voided by user request")
            payment.cancelled_at = timezone.now()
            payment.cancelled_by = request.user
            payment.save(update_fields=["status", "is_advance", "cancellation_reason", "cancelled_at", "cancelled_by"])
            _write_payment_activity(
                request,
                "payment_out_voided",
                "payment_out",
                payment,
                {
                    "amount": payment.amount_paid,
                    "reason": payment.cancellation_reason,
                    "settlementsReversed": True,
                },
            )

        return Response({"success": True, "message": "Payment-out voided and settlements reversed successfully"})

    @action(detail=True, methods=["get"])
    def receipt(self, request, pk=None):
        payment = self.get_object()
        return _receipt_response(
            request,
            payment,
            title="Payment Voucher",
            number_label="Payment Number",
            number=payment.payment_number,
            party_label="Supplier",
            party_name=payment.party.name,
            date=payment.payment_date,
            amount_label="Amount Paid",
            amount=payment.amount_paid,
            paid_label="Paid",
        )
