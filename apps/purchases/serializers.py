from rest_framework import serializers
from django.db import transaction
from .models import (
    PurchaseInvoice, PurchaseInvoiceItem, PurchaseOrder, 
    PurchaseOrderItem, DebitNote
)
from apps.items.models import Item, PriceHistory, apply_stock_movement
from apps.payments.models import PaymentOut, PaymentOutSettlement
from decimal import Decimal


def _normalise_payment_mode(value):
    value = (value or "cash").lower()
    if value in {"bank", "upi", "cheque"}:
        return value
    return "cash"

class PurchaseInvoiceItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseInvoiceItem
        exclude = ["invoice"]

class PurchaseInvoiceSerializer(serializers.ModelSerializer):
    line_items = PurchaseInvoiceItemSerializer(many=True)
    party_name = serializers.CharField(source="party.name", read_only=True)
    payment_mode = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = PurchaseInvoice
        fields = [
            "id", "invoice_number", "supplier_invoice_number", "party", 
            "party_name", "invoice_date", "due_date", "subtotal", 
            "discount_amount", "taxable_amount", "cgst_amount", "sgst_amount", 
            "igst_amount", "cess_amount", "total_amount", "paid_amount", 
            "status", "line_items", "notes", "payment_mode", "created_by", "created_at", "updated_at"
        ]
        read_only_fields = ["id", "invoice_number", "created_by", "created_at", "updated_at"]

    def create(self, validated_data):
        request = self.context["request"]
        business = request.business
        line_items_data = validated_data.pop("line_items")
        payment_mode = _normalise_payment_mode(validated_data.pop("payment_mode", "cash"))
        
        with transaction.atomic():
            # 1. Generate unique sequential invoice number
            # Format: PUR/26-27/0001
            year_suffix = "26-27"
            prefix = "PUR"
            
            last_invoice = PurchaseInvoice.objects.filter(
                business=business, invoice_number__startswith=f"{prefix}/{year_suffix}/"
            ).order_by("-created_at").first()
            
            next_seq = 1
            if last_invoice:
                try:
                    last_seq_str = last_invoice.invoice_number.split("/")[-1]
                    next_seq = int(last_seq_str) + 1
                except (ValueError, IndexError):
                    next_seq = 1
                    
            invoice_num = f"{prefix}/{year_suffix}/{next_seq:04d}"
            
            # 2. Determine payment status based on paid amount
            total_amt = validated_data["total_amount"]
            requested_paid_amt = Decimal(str(validated_data.get("paid_amount", Decimal("0.00")) or 0))
            paid_amt = min(max(requested_paid_amt, Decimal("0.00")), total_amt)
            
            validated_data["status"] = "unpaid"
            validated_data["paid_amount"] = Decimal("0.00")
            validated_data["invoice_number"] = invoice_num
            validated_data["business"] = business
            validated_data["created_by"] = request.user
            
            invoice = PurchaseInvoice.objects.create(**validated_data)
            
            # 3. Handle line items & stock logic
            for order, item_data in enumerate(line_items_data):
                item_data.pop("sort_order", None)
                line_item = PurchaseInvoiceItem.objects.create(
                    invoice=invoice,
                    sort_order=order,
                    **item_data
                )
                
                # Update inventory & register StockMovement if active stock item
                if line_item.item:
                    actual_item = Item.objects.select_for_update().get(id=line_item.item.id, business=business)
                    purchased_qty = line_item.quantity
                    
                    apply_stock_movement(
                        business=business,
                        item=actual_item,
                        godown=actual_item.godown,
                        movement_type="purchase",
                        reference_type="purchase_invoice",
                        reference_id=invoice.id,
                        quantity=purchased_qty,
                        rate=line_item.rate,
                        created_by=request.user,
                        notes=f"Purchased via Invoice {invoice_num}"
                    )
                    
                    # Record Price History
                    PriceHistory.objects.create(
                        business=business,
                        item=actual_item,
                        party=invoice.party,
                        voucher_type="purchase_invoice",
                        rate=line_item.rate,
                        transaction_date=invoice.invoice_date
                    )

            if paid_amt > 0:
                self._record_initial_payment(invoice, paid_amt, payment_mode, request.user)
                    
            return invoice

    def _record_initial_payment(self, invoice, paid_amt, payment_mode, user):
        last_payment = PaymentOut.objects.select_for_update().filter(
            business=invoice.business
        ).order_by("-created_at").first()
        next_seq = 1
        if last_payment:
            try:
                next_seq = int(last_payment.payment_number.split("-")[-1]) + 1
            except (ValueError, IndexError):
                next_seq = 1

        payment = PaymentOut.objects.create(
            business=invoice.business,
            payment_number=f"PMTOUT-{next_seq:04d}",
            party=invoice.party,
            amount_paid=paid_amt,
            payment_mode=payment_mode,
            reference_number=invoice.invoice_number,
            notes=f"Payment made against Purchase Invoice {invoice.invoice_number}",
            created_by=user,
        )
        PaymentOutSettlement.objects.create(
            payment_out=payment,
            invoice=invoice,
            settled_amount=paid_amt,
        )
        invoice.paid_amount = paid_amt
        if invoice.paid_amount >= invoice.total_amount:
            invoice.status = "paid"
        elif invoice.paid_amount > 0:
            invoice.status = "partial"
        else:
            invoice.status = "unpaid"
        invoice.save(update_fields=["paid_amount", "status", "updated_at"])

    def update(self, instance, validated_data):
        if instance.status == "cancelled":
            raise serializers.ValidationError("Cancelled purchase invoices cannot be edited.")

        request = self.context.get("request")
        line_items_data = validated_data.pop("line_items", None)

        with transaction.atomic():
            if line_items_data is not None:
                self._reverse_invoice_stock(instance, request.user if request else None)

            for attr, value in validated_data.items():
                if attr in {"invoice_number", "business", "created_by"}:
                    continue
                setattr(instance, attr, value)

            total_amt = instance.total_amount
            paid_amt = instance.paid_amount
            if paid_amt >= total_amt:
                instance.status = "paid"
            elif paid_amt > 0:
                instance.status = "partial"
            else:
                instance.status = "unpaid"
            instance.save()

            if line_items_data is not None:
                instance.line_items.all().delete()
                for order, item_data in enumerate(line_items_data):
                    item_data.pop("sort_order", None)
                    line_item = PurchaseInvoiceItem.objects.create(
                        invoice=instance,
                        sort_order=order,
                        **item_data
                    )
                    self._apply_invoice_stock(instance, line_item, request.user if request else None)

            return instance

    def _apply_invoice_stock(self, invoice, line_item, user):
        if not line_item.item:
            return
        actual_item = Item.objects.select_for_update().get(id=line_item.item.id, business=invoice.business)
        apply_stock_movement(
            business=invoice.business,
            item=actual_item,
            godown=actual_item.godown,
            movement_type="purchase",
            reference_type="purchase_invoice",
            reference_id=invoice.id,
            quantity=line_item.quantity,
            rate=line_item.rate,
            created_by=user,
            notes=f"Purchased via Invoice {invoice.invoice_number}",
        )

    def _reverse_invoice_stock(self, invoice, user):
        for line_item in invoice.line_items.select_related("item"):
            if not line_item.item:
                continue
            actual_item = Item.objects.select_for_update().get(id=line_item.item.id, business=invoice.business)
            apply_stock_movement(
                business=invoice.business,
                item=actual_item,
                godown=actual_item.godown,
                movement_type="purchase_return",
                reference_type="purchase_invoice_update",
                reference_id=invoice.id,
                quantity=-line_item.quantity,
                rate=line_item.rate,
                created_by=user,
                notes=f"Reversed previous Purchase Invoice {invoice.invoice_number}",
                allow_negative=False,
            )

class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseOrderItem
        exclude = ["order"]

class PurchaseOrderSerializer(serializers.ModelSerializer):
    line_items = PurchaseOrderItemSerializer(many=True)
    party_name = serializers.CharField(source="party.name", read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = "__all__"
        read_only_fields = ["id", "business", "order_number", "converted_invoice", "created_at"]

    def create(self, validated_data):
        business = self.context["request"].business
        line_items_data = validated_data.pop("line_items")
        
        with transaction.atomic():
            year_suffix = "26-27"
            prefix = "PO"
            last_order = PurchaseOrder.objects.filter(
                business=business, order_number__startswith=f"{prefix}/{year_suffix}/"
            ).order_by("-created_at").first()
            next_seq = 1
            if last_order:
                try:
                    next_seq = int(last_order.order_number.split("/")[-1]) + 1
                except (ValueError, IndexError):
                    next_seq = 1
                    
            validated_data["order_number"] = f"{prefix}/{year_suffix}/{next_seq:04d}"
            validated_data["business"] = business
            
            order = PurchaseOrder.objects.create(**validated_data)
            
            for order_idx, item_data in enumerate(line_items_data):
                item_data.pop("sort_order", None)
                PurchaseOrderItem.objects.create(
                    order=order,
                    sort_order=order_idx,
                    **item_data
                )
                
            return order

    def update(self, instance, validated_data):
        if instance.status == "cancelled":
            raise serializers.ValidationError("Cancelled purchase orders cannot be edited.")
        line_items_data = validated_data.pop("line_items", None)

        with transaction.atomic():
            for attr, value in validated_data.items():
                if attr in {"business", "order_number", "converted_invoice"}:
                    continue
                setattr(instance, attr, value)
            instance.save()

            if line_items_data is not None:
                instance.line_items.all().delete()
                for order_idx, item_data in enumerate(line_items_data):
                    item_data.pop("sort_order", None)
                    PurchaseOrderItem.objects.create(
                        order=instance,
                        sort_order=order_idx,
                        **item_data
                    )

            return instance

class DebitNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = DebitNote
        fields = "__all__"
        read_only_fields = ["id", "business", "debit_note_number", "created_at"]

    def create(self, validated_data):
        business = self.context["request"].business
        
        with transaction.atomic():
            year_suffix = "26-27"
            prefix = "DN"
            last_note = DebitNote.objects.filter(
                business=business, debit_note_number__startswith=f"{prefix}/{year_suffix}/"
            ).order_by("-created_at").first()
            next_seq = 1
            if last_note:
                try:
                    next_seq = int(last_note.debit_note_number.split("/")[-1]) + 1
                except (ValueError, IndexError):
                    next_seq = 1
                    
            validated_data["debit_note_number"] = f"{prefix}/{year_suffix}/{next_seq:04d}"
            validated_data["business"] = business
            
            return DebitNote.objects.create(**validated_data)
