import uuid
from rest_framework import viewsets, permissions, status, views
from rest_framework.decorators import action
from rest_framework.response import Response
from apps.accounts.throttles import TenantScopedRateThrottle
from .models import PartyCategory, Party
from .serializers import PartyCategorySerializer, PartySerializer


def _num(value):
    return float(value or 0)


def _build_ledger_statement(party):
    from apps.sales.models import SalesInvoice, CreditNote
    from apps.purchases.models import PurchaseInvoice, DebitNote
    from apps.payments.models import PaymentIn, PaymentOut

    entries = [
        {
            "date": party.created_at.date().isoformat(),
            "type": "Opening Balance",
            "number": "-",
            "description": "Opening Balance",
            "debit": _num(party.opening_balance) if party.opening_balance_type == "debit" else 0.0,
            "credit": _num(party.opening_balance) if party.opening_balance_type == "credit" else 0.0,
            "status": "Opening",
        }
    ]

    for item in SalesInvoice.objects.filter(party=party).exclude(status="cancelled"):
        entries.append({
            "date": item.invoice_date.isoformat(),
            "type": "Sales Invoice",
            "number": item.invoice_number,
            "description": f"Sales Invoice Ref: {item.invoice_number}",
            "debit": _num(item.total_amount),
            "credit": 0.0,
            "status": item.status.title(),
        })

    for item in PaymentIn.objects.filter(party=party).exclude(status="void"):
        entries.append({
            "date": item.payment_date.isoformat(),
            "type": "Payment In",
            "number": item.payment_number,
            "description": f"Payment Received via {item.payment_mode.upper()}",
            "debit": 0.0,
            "credit": _num(item.amount_received),
            "status": "Received",
        })

    for item in CreditNote.objects.filter(party=party, status="credited"):
        entries.append({
            "date": item.note_date.isoformat(),
            "type": "Credit Note",
            "number": item.credit_note_number,
            "description": f"Credit Note: {item.reason or 'Sales return adjustment'}",
            "debit": 0.0,
            "credit": _num(item.total_amount),
            "status": item.status.title(),
        })

    for item in PurchaseInvoice.objects.filter(party=party).exclude(status="cancelled"):
        entries.append({
            "date": item.invoice_date.isoformat(),
            "type": "Purchase Invoice",
            "number": item.invoice_number,
            "description": f"Purchase Invoice Ref: {item.supplier_invoice_number or item.invoice_number}",
            "debit": 0.0,
            "credit": _num(item.total_amount),
            "status": item.status.title(),
        })

    for item in PaymentOut.objects.filter(party=party).exclude(status="void"):
        entries.append({
            "date": item.payment_date.isoformat(),
            "type": "Payment Out",
            "number": item.payment_number,
            "description": f"Payment Paid via {item.payment_mode.upper()}",
            "debit": _num(item.amount_paid),
            "credit": 0.0,
            "status": "Paid",
        })

    for item in DebitNote.objects.filter(party=party, status="credited"):
        entries.append({
            "date": item.note_date.isoformat(),
            "type": "Debit Note",
            "number": item.debit_note_number,
            "description": f"Debit Note: {item.reason or 'Purchase return adjustment'}",
            "debit": _num(item.total_amount),
            "credit": 0.0,
            "status": item.status.title(),
        })

    entries.sort(key=lambda row: (row["date"], row["type"], row["number"]))

    running = 0.0
    for entry in entries:
        running += entry["debit"] - entry["credit"]
        entry["amount"] = entry["debit"] or entry["credit"]
        entry["balance"] = running

    return entries


class PartyCategoryViewSet(viewsets.ModelViewSet):
    serializer_class = PartyCategorySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return PartyCategory.objects.none()
        return PartyCategory.objects.filter(business=self.request.business)

class PartyViewSet(viewsets.ModelViewSet):
    serializer_class = PartySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return Party.objects.none()
        
        queryset = Party.objects.filter(business=self.request.business, is_active=True)
        
        # Filtering by type or status
        party_type = self.request.query_params.get("party_type")
        if party_type:
            queryset = queryset.filter(party_type=party_type)
            
        return queryset

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])

    @action(detail=True, methods=["post"])
    def generate_shared_ledger(self, request, pk=None):
        """Generates a secure unique link token for customer/supplier portal access."""
        party = self.get_object()
        if not party.shared_ledger_token:
            party.shared_ledger_token = uuid.uuid4().hex[:20]
            party.save(update_fields=["shared_ledger_token", "updated_at"])

        ledger = _build_ledger_statement(party)
            
        return Response({
            "success": True,
            "shared_ledger_token": party.shared_ledger_token,
            "url": f"/shared-ledger/{party.shared_ledger_token}",
            "party": PartySerializer(party, context={"request": request}).data,
            "summary": {
                "entries": len(ledger),
                "currentBalance": ledger[-1]["balance"] if ledger else 0,
            },
        })

    @action(detail=False, methods=["get"], url_path="shared-ledgers")
    def shared_ledgers(self, request):
        rows = []
        linked_parties = self.get_queryset().exclude(shared_ledger_token__isnull=True).exclude(shared_ledger_token="")

        for party in linked_parties.select_related("category").order_by("name"):
            ledger = _build_ledger_statement(party)
            for index, entry in enumerate(ledger):
                rows.append({
                    "id": f"{party.shared_ledger_token}-{index}",
                    "partyId": str(party.id),
                    "partyName": party.name,
                    "token": party.shared_ledger_token,
                    "url": f"/shared-ledger/{party.shared_ledger_token}",
                    "date": entry["date"],
                    "transactionType": entry["type"],
                    "transactionNumber": entry["number"],
                    "amount": entry["amount"],
                    "status": entry["status"],
                    "balance": entry["balance"],
                })

        rows.sort(key=lambda row: (row["date"], row["partyName"], row["transactionNumber"]), reverse=True)
        return Response({
            "success": True,
            "rows": rows,
            "summary": {
                "linkedParties": linked_parties.count(),
                "transactions": len(rows),
                "viewOnlyMode": True,
            }
        })

    @action(detail=True, methods=["get"])
    def ledger_statement(self, request, pk=None):
        """Generates real-time ledger entries list sorted chronologically."""
        party = self.get_object()
            
        return Response({
            "success": True,
            "party": PartySerializer(party).data,
            "ledger": _build_ledger_statement(party)
        })


class SharedLedgerPortalView(views.APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [TenantScopedRateThrottle]
    throttle_scope = "public_share"

    def get(self, request, token):
        try:
            party = Party.objects.select_related("business", "category").get(
                shared_ledger_token=token,
                is_active=True,
            )
        except Party.DoesNotExist:
            return Response(
                {"success": False, "message": "Shared ledger link is invalid or expired."},
                status=status.HTTP_404_NOT_FOUND,
            )

        ledger = _build_ledger_statement(party)
        current_balance = ledger[-1]["balance"] if ledger else 0
        return Response({
            "success": True,
            "business": {
                "id": str(party.business_id),
                "name": party.business.name,
                "phone": party.business.phone or "",
                "gstin": party.business.gstin or "",
            },
            "party": PartySerializer(party).data,
            "ledger": ledger,
            "summary": {
                "entries": len(ledger),
                "currentBalance": current_balance,
                "toCollect": max(0, current_balance),
                "toPay": abs(min(0, current_balance)),
                "viewOnlyMode": True,
            }
        })
