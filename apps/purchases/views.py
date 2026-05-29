from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import transaction
from django.utils import timezone
from .models import PurchaseInvoice, PurchaseOrder, DebitNote
from .serializers import (
    PurchaseInvoiceSerializer, PurchaseOrderSerializer, DebitNoteSerializer
)
from apps.accounts.activity import write_activity
from apps.items.models import Item, apply_stock_movement
from apps.payments.models import PaymentOutSettlement


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
            invoice_data = {
                "party": order.party.id,
                "subtotal": float(order.total_amount),
                "total_amount": float(order.total_amount),
                "notes": f"Converted from Purchase Order No: {order.order_number}",
                "line_items": []
            }
            
            for line_item in order.line_items.all():
                invoice_data["line_items"].append({
                    "item": line_item.item.id if line_item.item else None,
                    "item_name": line_item.item_name,
                    "quantity": float(line_item.quantity),
                    "rate": float(line_item.rate),
                    "gst_rate": float(line_item.gst_rate),
                    "taxable_amount": float(line_item.amount),
                    "amount": float(line_item.amount)
                })
                
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
