from rest_framework import serializers
from django.db import transaction
from django.utils import timezone
from .models import (
    PurchaseInvoice, PurchaseInvoiceItem, PurchaseOrder, 
    PurchaseOrderItem, PurchaseReturn, PurchaseReturnItem, DebitNote
)
from apps.accounts.sequences import next_model_document_number
from apps.items.models import Item, PriceHistory, apply_stock_movement
from apps.payments.models import PaymentOut, PaymentOutSettlement
from decimal import Decimal


def _normalise_payment_mode(value):
    value = (value or "cash").lower()
    if value in {"bank", "upi", "cheque"}:
        return value
    return "cash"


def _financial_year(business=None, document_date=None):
    date_value = document_date or timezone.localdate()
    fy_start_month = int(getattr(business, "fy_start_month", 4) or 4)
    start_year = date_value.year if date_value.month >= fy_start_month else date_value.year - 1
    return f"{str(start_year)[-2:]}-{str(start_year + 1)[-2:]}"


def _next_purchase_document_number(*, business, model, field_name, prefix, document_date=None):
    fy = _financial_year(business, document_date)
    number_prefix = f"{prefix}/{fy}/"
    return next_model_document_number(
        business=business,
        sequence_key=f"{model._meta.model_name}:{field_name}:{prefix}:{fy}",
        model=model,
        field_name=field_name,
        number_prefix=number_prefix,
    )

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
            invoice_num = _next_purchase_document_number(
                business=business,
                model=PurchaseInvoice,
                field_name="invoice_number",
                prefix="PUR",
                document_date=validated_data.get("invoice_date"),
            )
            
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
        payment = PaymentOut.objects.create(
            business=invoice.business,
            payment_number=next_model_document_number(
                business=invoice.business,
                sequence_key="pmtout:PMTOUT",
                model=PaymentOut,
                field_name="payment_number",
                number_prefix="PMTOUT-",
            ),
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
            validated_data["order_number"] = _next_purchase_document_number(
                business=business,
                model=PurchaseOrder,
                field_name="order_number",
                prefix="PO",
            )
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


class PurchaseReturnItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseReturnItem
        exclude = ["purchase_return"]


class PurchaseReturnSerializer(serializers.ModelSerializer):
    line_items = PurchaseReturnItemSerializer(many=True)
    party_name = serializers.CharField(source="party.name", read_only=True)
    original_invoice_number = serializers.CharField(source="original_invoice.invoice_number", read_only=True)

    class Meta:
        model = PurchaseReturn
        fields = [
            "id", "return_number", "party", "party_name", "original_invoice",
            "original_invoice_number", "reference_number", "return_date",
            "total_amount", "status", "reason", "line_items", "created_by",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "return_number", "party_name", "original_invoice_number",
            "status", "created_by", "created_at", "updated_at",
        ]

    def validate(self, attrs):
        business = self.context["request"].business
        party = attrs.get("party") or getattr(self.instance, "party", None)
        original_invoice = attrs.get("original_invoice") or getattr(self.instance, "original_invoice", None)
        line_items = attrs.get("line_items")

        if party and party.business_id != business.id:
            raise serializers.ValidationError({"party": "Choose a supplier from the active tenant."})
        if party and party.party_type not in ["supplier", "both"]:
            raise serializers.ValidationError({"party": "Purchase returns can only be linked to supplier parties."})
        if original_invoice:
            if original_invoice.business_id != business.id:
                raise serializers.ValidationError({"original_invoice": "Choose a purchase invoice from the active tenant."})
            if party and original_invoice.party_id != party.id:
                raise serializers.ValidationError({"original_invoice": "The linked purchase invoice must belong to the selected supplier."})
        if line_items is not None and len(line_items) == 0:
            raise serializers.ValidationError({"line_items": "Add at least one returned item."})
        for line in line_items or []:
            item = line.get("item")
            quantity = Decimal(str(line.get("quantity") or 0))
            if item and item.business_id != business.id:
                raise serializers.ValidationError({"line_items": "Choose returned items from the active tenant."})
            if quantity <= 0:
                raise serializers.ValidationError({"line_items": "Returned quantity must be greater than zero."})
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        business = request.business
        line_items_data = validated_data.pop("line_items")

        with transaction.atomic():
            validated_data["business"] = business
            validated_data["created_by"] = request.user
            validated_data["return_number"] = _next_purchase_document_number(
                business=business,
                model=PurchaseReturn,
                field_name="return_number",
                prefix="PR",
            )
            validated_data["status"] = "adjusted"
            purchase_return = PurchaseReturn.objects.create(**validated_data)

            for order, item_data in enumerate(line_items_data):
                item_data.pop("sort_order", None)
                line_item = PurchaseReturnItem.objects.create(
                    purchase_return=purchase_return,
                    sort_order=order,
                    **item_data,
                )
                self._apply_return_stock(purchase_return, line_item, request.user)

            return purchase_return

    def update(self, instance, validated_data):
        if instance.status == "cancelled":
            raise serializers.ValidationError("Cancelled purchase returns cannot be edited.")

        request = self.context.get("request")
        line_items_data = validated_data.pop("line_items", None)

        with transaction.atomic():
            if line_items_data is not None:
                self._reverse_return_stock(instance, request.user if request else None)

            for attr, value in validated_data.items():
                if attr in {"return_number", "business", "created_by", "status"}:
                    continue
                setattr(instance, attr, value)
            instance.status = "adjusted"
            instance.save()

            if line_items_data is not None:
                instance.line_items.all().delete()
                for order, item_data in enumerate(line_items_data):
                    item_data.pop("sort_order", None)
                    line_item = PurchaseReturnItem.objects.create(
                        purchase_return=instance,
                        sort_order=order,
                        **item_data,
                    )
                    self._apply_return_stock(instance, line_item, request.user if request else None)

            return instance

    def _apply_return_stock(self, purchase_return, line_item, user):
        if not line_item.item:
            return
        actual_item = Item.objects.select_for_update().get(id=line_item.item.id, business=purchase_return.business)
        apply_stock_movement(
            business=purchase_return.business,
            item=actual_item,
            godown=actual_item.godown,
            movement_type="purchase_return",
            reference_type="purchase_return",
            reference_id=purchase_return.id,
            quantity=-line_item.quantity,
            rate=line_item.rate,
            created_by=user,
            notes=f"Purchase return {purchase_return.return_number}",
            allow_negative=False,
        )

    def _reverse_return_stock(self, purchase_return, user):
        for line_item in purchase_return.line_items.select_related("item"):
            if not line_item.item:
                continue
            actual_item = Item.objects.select_for_update().get(id=line_item.item.id, business=purchase_return.business)
            apply_stock_movement(
                business=purchase_return.business,
                item=actual_item,
                godown=actual_item.godown,
                movement_type="purchase",
                reference_type="purchase_return_reverse",
                reference_id=purchase_return.id,
                quantity=line_item.quantity,
                rate=line_item.rate,
                created_by=user,
                notes=f"Reversed previous Purchase Return {purchase_return.return_number}",
            )


class DebitNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = DebitNote
        fields = "__all__"
        read_only_fields = ["id", "business", "debit_note_number", "created_at"]

    def create(self, validated_data):
        business = self.context["request"].business
        
        with transaction.atomic():
            validated_data["debit_note_number"] = _next_purchase_document_number(
                business=business,
                model=DebitNote,
                field_name="debit_note_number",
                prefix="DN",
            )
            validated_data["business"] = business
            
            return DebitNote.objects.create(**validated_data)
