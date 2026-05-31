from rest_framework import serializers
from django.db import transaction
from django.utils import timezone
from .models import (
    SalesInvoice, SalesInvoiceItem, EInvoiceLog, Quotation, QuotationItem, 
    DeliveryChallan, DeliveryChallanItem, CreditNote,
    ProformaInvoice, ProformaInvoiceItem, SalesReturn, SalesReturnItem
)
from apps.items.models import Item, PriceHistory, apply_stock_movement
from apps.payments.models import PaymentIn, PaymentInSettlement
from apps.business_settings.models import InvoiceSettings
from apps.accounts.sequences import next_model_document_number
from decimal import Decimal


def _normalise_payment_mode(value):
    value = (value or "cash").lower()
    if value in {"bank", "upi", "cheque"}:
        return value
    return "cash"


def _line_stock_quantity(line_item):
    quantity = Decimal(str(line_item.quantity or 0))
    free_quantity = Decimal(str(line_item.free_quantity or 0))
    return quantity + free_quantity


def _financial_year_suffix(business, invoice_date):
    fy_start_month = int(getattr(business, "fy_start_month", 4) or 4)
    start_year = invoice_date.year if invoice_date.month >= fy_start_month else invoice_date.year - 1
    return f"{start_year % 100:02d}-{(start_year + 1) % 100:02d}"


def _next_invoice_number(business, invoice_settings, invoice_date):
    prefix = (invoice_settings.invoice_prefix or business.invoice_prefix or "INV").strip() or "INV"
    if invoice_settings.reset_each_year:
        year_suffix = _financial_year_suffix(business, invoice_date)
        number_prefix = f"{prefix}/{year_suffix}/"
        sequence_key = f"sales_invoice:{prefix}:{year_suffix}"
    else:
        number_prefix = f"{prefix}/"
        sequence_key = f"sales_invoice:{prefix}:all"

    return next_model_document_number(
        business=business,
        sequence_key=sequence_key,
        model=SalesInvoice,
        field_name="invoice_number",
        number_prefix=number_prefix,
    )


def _next_plain_document_number(*, business, model, field_name):
    return next_model_document_number(
        business=business,
        sequence_key=f"{model._meta.model_name}:{field_name}",
        model=model,
        field_name=field_name,
        number_prefix="",
        width=0,
    )


def _serializer_business(serializer):
    request = serializer.context.get("request")
    return getattr(request, "business", None)


def _validate_business_party(serializer, party):
    business = _serializer_business(serializer)
    if business and (party.business_id != business.id or not party.is_active):
        raise serializers.ValidationError("Select an active party from this business.")
    return party


def _validate_register_line_items(serializer, line_items):
    business = _serializer_business(serializer)
    if not line_items:
        raise serializers.ValidationError("Add at least one item before saving this voucher.")

    for index, line_item in enumerate(line_items, start=1):
        item = line_item.get("item")
        if item and business and (item.business_id != business.id or not item.is_active):
            raise serializers.ValidationError(f"Line {index}: select an active item from this business.")
        if Decimal(str(line_item.get("quantity") or 0)) <= 0:
            raise serializers.ValidationError(f"Line {index}: quantity must be greater than zero.")
        if Decimal(str(line_item.get("rate") or 0)) < 0:
            raise serializers.ValidationError(f"Line {index}: rate cannot be negative.")
        if Decimal(str(line_item.get("amount") or 0)) < 0:
            raise serializers.ValidationError(f"Line {index}: amount cannot be negative.")

    return line_items

class SalesInvoiceItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = SalesInvoiceItem
        exclude = ["invoice"]

class EInvoiceLogSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.name", read_only=True)

    class Meta:
        model = EInvoiceLog
        fields = [
            "id", "event", "status", "provider", "request_payload",
            "response_payload", "message", "created_by_name", "created_at"
        ]
        read_only_fields = fields

class SalesInvoiceSerializer(serializers.ModelSerializer):
    line_items = SalesInvoiceItemSerializer(many=True)
    party_name = serializers.CharField(source="party.name", read_only=True)
    party_mobile = serializers.CharField(source="party.mobile", read_only=True)
    party_gstin = serializers.CharField(source="party.gstin", read_only=True)
    einvoice_logs = EInvoiceLogSerializer(many=True, read_only=True)
    payment_mode = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = SalesInvoice
        fields = [
            "id", "invoice_number", "party", "party_name", "party_mobile",
            "invoice_date", "due_date", "place_of_supply", "vehicle_no",
            "transport_name", "lr_no", "eway_bill_no", "grn_no", "grn_date",
            "cin_no", "cin_date", "subtotal", "discount_amount", "discount_pct",
            "taxable_amount", "cgst_amount", "sgst_amount", "igst_amount",
            "cess_amount", "additional_charges", "additional_charges_label",
            "round_off", "total_amount", "paid_amount", "status", "line_items",
            "irn", "ack_number", "ack_date", "qr_code_data", "einvoice_status",
            "einvoice_provider", "einvoice_retry_count", "einvoice_last_error",
            "einvoice_cancel_reason", "einvoice_cancelled_at", "einvoice_logs",
            "notes", "terms", "party_gstin",
            "is_pos", "payment_mode", "created_by", "created_at", "updated_at"
        ]
        read_only_fields = [
            "id", "invoice_number", "irn", "ack_number", "ack_date", "qr_code_data",
            "einvoice_status", "einvoice_provider", "einvoice_retry_count",
            "einvoice_last_error", "einvoice_cancel_reason", "einvoice_cancelled_at",
            "einvoice_logs", "created_by", "created_at", "updated_at",
        ]

    def validate_party(self, party):
        request = self.context.get("request")
        business = getattr(request, "business", None)
        if business and (party.business_id != business.id or not party.is_active):
            raise serializers.ValidationError("Select an active party from this business.")
        return party

    def validate_line_items(self, line_items):
        request = self.context.get("request")
        business = getattr(request, "business", None)
        if not line_items:
            raise serializers.ValidationError("Add at least one item before saving the invoice.")

        for index, line_item in enumerate(line_items, start=1):
            item = line_item.get("item")
            if item and business and (item.business_id != business.id or not item.is_active):
                raise serializers.ValidationError(f"Line {index}: select an active item from this business.")

            stock_qty = Decimal(str(line_item.get("quantity") or 0)) + Decimal(str(line_item.get("free_quantity") or 0))
            if stock_qty <= 0:
                raise serializers.ValidationError(f"Line {index}: quantity must be greater than zero.")
            if Decimal(str(line_item.get("rate") or 0)) < 0:
                raise serializers.ValidationError(f"Line {index}: rate cannot be negative.")

        return line_items

    def validate(self, attrs):
        total_amount = Decimal(str(attrs.get("total_amount", getattr(self.instance, "total_amount", 0)) or 0))
        paid_amount = Decimal(str(attrs.get("paid_amount", getattr(self.instance, "paid_amount", 0)) or 0))

        if total_amount <= 0:
            raise serializers.ValidationError({"total_amount": "Invoice total must be greater than zero."})
        if paid_amount < 0:
            raise serializers.ValidationError({"paid_amount": "Paid amount cannot be negative."})
        if paid_amount > total_amount:
            raise serializers.ValidationError({"paid_amount": "Paid amount cannot be greater than invoice total."})

        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        business = request.business
        line_items_data = validated_data.pop("line_items")
        payment_mode = _normalise_payment_mode(validated_data.pop("payment_mode", "cash"))
        
        with transaction.atomic():
            invoice_settings, _ = InvoiceSettings.objects.select_for_update().get_or_create(
                business=business,
                defaults={"invoice_prefix": business.invoice_prefix or "INV"},
            )
            invoice_date = validated_data.get("invoice_date") or timezone.localdate()
            invoice_num = _next_invoice_number(business, invoice_settings, invoice_date)
            
            # 2. Determine payment status based on paid amount
            total_amt = validated_data["total_amount"]
            requested_paid_amt = Decimal(str(validated_data.get("paid_amount", Decimal("0.00")) or 0))
            paid_amt = min(max(requested_paid_amt, Decimal("0.00")), total_amt)
            
            validated_data["status"] = "unpaid"
            validated_data["paid_amount"] = Decimal("0.00")
            validated_data["invoice_number"] = invoice_num
            validated_data["business"] = business
            validated_data["created_by"] = request.user
            
            invoice = SalesInvoice.objects.create(**validated_data)
            
            # 3. Handle line items & stock logic
            for order, item_data in enumerate(line_items_data):
                item_data.pop("sort_order", None)
                line_item = SalesInvoiceItem.objects.create(
                    invoice=invoice,
                    sort_order=order,
                    **item_data
                )
                
                # Update inventory & register StockMovement if active stock item
                if line_item.item:
                    actual_item = Item.objects.select_for_update().get(
                        id=line_item.item.id,
                        business=business,
                        is_active=True,
                    )
                    sold_qty = _line_stock_quantity(line_item)
                    
                    stock, movement = apply_stock_movement(
                        business=business,
                        item=actual_item,
                        godown=actual_item.godown,
                        movement_type="sale",
                        reference_type="sales_invoice",
                        reference_id=invoice.id,
                        quantity=-sold_qty,
                        rate=line_item.rate,
                        created_by=request.user,
                        notes=f"Sold via Invoice {invoice_num}",
                        allow_negative=False,
                    )
                    
                    # Record Price History for fast autofill
                    PriceHistory.objects.create(
                        business=business,
                        item=actual_item,
                        party=invoice.party,
                        voucher_type="sales_invoice",
                        rate=line_item.rate,
                        transaction_date=invoice.invoice_date
                    )

            if paid_amt > 0:
                self._record_initial_payment(invoice, paid_amt, payment_mode, request.user)
                    
            return invoice

    def _record_initial_payment(self, invoice, paid_amt, payment_mode, user):
        payment = PaymentIn.objects.create(
            business=invoice.business,
            payment_number=next_model_document_number(
                business=invoice.business,
                sequence_key="pmtin:PMTIN",
                model=PaymentIn,
                field_name="payment_number",
                number_prefix="PMTIN-",
            ),
            party=invoice.party,
            amount_received=paid_amt,
            payment_mode=payment_mode,
            reference_number=invoice.invoice_number,
            notes=f"Payment received against Invoice {invoice.invoice_number}",
            created_by=user,
        )
        PaymentInSettlement.objects.create(
            payment_in=payment,
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
            raise serializers.ValidationError("Cancelled invoices cannot be edited.")

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
                    line_item = SalesInvoiceItem.objects.create(
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
        sold_qty = _line_stock_quantity(line_item)
        apply_stock_movement(
            business=invoice.business,
            item=actual_item,
            godown=actual_item.godown,
            movement_type="sale",
            reference_type="sales_invoice",
            reference_id=invoice.id,
            quantity=-sold_qty,
            rate=line_item.rate,
            created_by=user,
            notes=f"Sold via Invoice {invoice.invoice_number}",
            allow_negative=False,
        )

    def _reverse_invoice_stock(self, invoice, user):
        for line_item in invoice.line_items.select_related("item"):
            if not line_item.item:
                continue
            actual_item = Item.objects.select_for_update().get(id=line_item.item.id, business=invoice.business)
            restored_qty = _line_stock_quantity(line_item)
            apply_stock_movement(
                business=invoice.business,
                item=actual_item,
                godown=actual_item.godown,
                movement_type="sales_return",
                reference_type="sales_invoice_update",
                reference_id=invoice.id,
                quantity=restored_qty,
                rate=line_item.rate,
                created_by=user,
                notes=f"Reversed previous Invoice {invoice.invoice_number}",
            )

class QuotationItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuotationItem
        exclude = ["quotation"]

class QuotationSerializer(serializers.ModelSerializer):
    line_items = QuotationItemSerializer(many=True)
    party_name = serializers.CharField(source="party.name", read_only=True)

    class Meta:
        model = Quotation
        fields = "__all__"
        read_only_fields = ["id", "business", "quotation_number", "converted_invoice", "created_by", "created_at"]

    def validate_party(self, party):
        return _validate_business_party(self, party)

    def validate_line_items(self, line_items):
        return _validate_register_line_items(self, line_items)

    def create(self, validated_data):
        request = self.context["request"]
        business = request.business
        line_items_data = validated_data.pop("line_items")
        
        with transaction.atomic():
            validated_data["quotation_number"] = _next_plain_document_number(
                business=business,
                model=Quotation,
                field_name="quotation_number",
            )
            validated_data["business"] = business
            validated_data["created_by"] = request.user
            
            quotation = Quotation.objects.create(**validated_data)
            
            for order, item_data in enumerate(line_items_data):
                item_data.pop("sort_order", None)
                QuotationItem.objects.create(
                    quotation=quotation,
                    sort_order=order,
                    **item_data
                )
                
            return quotation

    def update(self, instance, validated_data):
        if instance.status == "cancelled":
            raise serializers.ValidationError("Cancelled quotations cannot be edited.")
        line_items_data = validated_data.pop("line_items", None)

        with transaction.atomic():
            for attr, value in validated_data.items():
                if attr in {"business", "quotation_number", "created_by", "converted_invoice"}:
                    continue
                setattr(instance, attr, value)
            instance.save()

            if line_items_data is not None:
                instance.line_items.all().delete()
                for order, item_data in enumerate(line_items_data):
                    item_data.pop("sort_order", None)
                    QuotationItem.objects.create(
                        quotation=instance,
                        sort_order=order,
                        **item_data
                    )

            return instance

class DeliveryChallanItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryChallanItem
        exclude = ["challan"]

class DeliveryChallanSerializer(serializers.ModelSerializer):
    line_items = DeliveryChallanItemSerializer(many=True)
    party_name = serializers.CharField(source="party.name", read_only=True)

    class Meta:
        model = DeliveryChallan
        fields = "__all__"
        read_only_fields = ["id", "business", "challan_number", "converted_invoice", "created_at"]

    def validate_party(self, party):
        return _validate_business_party(self, party)

    def validate_line_items(self, line_items):
        return _validate_register_line_items(self, line_items)

    def create(self, validated_data):
        business = self.context["request"].business
        line_items_data = validated_data.pop("line_items")
        
        with transaction.atomic():
            validated_data["challan_number"] = _next_plain_document_number(
                business=business,
                model=DeliveryChallan,
                field_name="challan_number",
            )
            validated_data["business"] = business
            
            challan = DeliveryChallan.objects.create(**validated_data)
            
            for order, item_data in enumerate(line_items_data):
                item_data.pop("sort_order", None)
                DeliveryChallanItem.objects.create(
                    challan=challan,
                    sort_order=order,
                    **item_data
                )
                
            return challan

    def update(self, instance, validated_data):
        if instance.status == "cancelled":
            raise serializers.ValidationError("Cancelled challans cannot be edited.")
        line_items_data = validated_data.pop("line_items", None)

        with transaction.atomic():
            for attr, value in validated_data.items():
                if attr in {"business", "challan_number", "converted_invoice"}:
                    continue
                setattr(instance, attr, value)
            instance.save()

            if line_items_data is not None:
                instance.line_items.all().delete()
                for order, item_data in enumerate(line_items_data):
                    item_data.pop("sort_order", None)
                    DeliveryChallanItem.objects.create(
                        challan=instance,
                        sort_order=order,
                        **item_data
                    )

            return instance

class ProformaInvoiceItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProformaInvoiceItem
        exclude = ["proforma"]

class ProformaInvoiceSerializer(serializers.ModelSerializer):
    line_items = ProformaInvoiceItemSerializer(many=True)
    party_name = serializers.CharField(source="party.name", read_only=True)

    class Meta:
        model = ProformaInvoice
        fields = "__all__"
        read_only_fields = [
            "id", "business", "proforma_number", "converted_invoice", "created_at"
        ]

    def validate_party(self, party):
        return _validate_business_party(self, party)

    def validate_line_items(self, line_items):
        return _validate_register_line_items(self, line_items)

    def create(self, validated_data):
        request = self.context["request"]
        business = validated_data.pop("business", None) or request.business
        line_items_data = validated_data.pop("line_items")

        with transaction.atomic():
            validated_data["proforma_number"] = _next_plain_document_number(
                business=business,
                model=ProformaInvoice,
                field_name="proforma_number",
            )
            validated_data["business"] = business

            proforma = ProformaInvoice.objects.create(**validated_data)

            for order, item_data in enumerate(line_items_data):
                item_data.pop("sort_order", None)
                ProformaInvoiceItem.objects.create(
                    proforma=proforma,
                    sort_order=order,
                    **item_data
                )

            return proforma

    def update(self, instance, validated_data):
        if instance.status == "cancelled":
            raise serializers.ValidationError("Cancelled proforma invoices cannot be edited.")
        line_items_data = validated_data.pop("line_items", None)

        with transaction.atomic():
            for attr, value in validated_data.items():
                if attr in {"business", "proforma_number", "converted_invoice"}:
                    continue
                setattr(instance, attr, value)
            instance.save()

            if line_items_data is not None:
                instance.line_items.all().delete()
                for order, item_data in enumerate(line_items_data):
                    item_data.pop("sort_order", None)
                    ProformaInvoiceItem.objects.create(
                        proforma=instance,
                        sort_order=order,
                        **item_data
                    )

            return instance

class SalesReturnItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = SalesReturnItem
        exclude = ["sales_return"]

class SalesReturnSerializer(serializers.ModelSerializer):
    line_items = SalesReturnItemSerializer(many=True)
    party_name = serializers.CharField(source="party.name", read_only=True)
    original_invoice_number = serializers.CharField(source="original_invoice.invoice_number", read_only=True)

    class Meta:
        model = SalesReturn
        fields = "__all__"
        read_only_fields = ["id", "business", "return_number", "created_at"]

    def create(self, validated_data):
        request = self.context["request"]
        business = validated_data.pop("business", None) or request.business
        line_items_data = validated_data.pop("line_items")

        with transaction.atomic():
            validated_data["return_number"] = _next_plain_document_number(
                business=business,
                model=SalesReturn,
                field_name="return_number",
            )
            validated_data["business"] = business

            sales_return = SalesReturn.objects.create(**validated_data)

            for order, item_data in enumerate(line_items_data):
                item_data.pop("sort_order", None)
                return_item = SalesReturnItem.objects.create(
                    sales_return=sales_return,
                    sort_order=order,
                    **item_data
                )
                self._restore_returned_stock(business, request.user, sales_return, return_item)

            return sales_return

    def update(self, instance, validated_data):
        if instance.status == "cancelled":
            raise serializers.ValidationError("Cancelled sales returns cannot be edited.")
        line_items_data = validated_data.pop("line_items", None)
        request = self.context.get("request")

        with transaction.atomic():
            for attr, value in validated_data.items():
                if attr in {"business", "return_number"}:
                    continue
                setattr(instance, attr, value)
            instance.save()

            if line_items_data is not None:
                for existing_item in instance.line_items.select_related("item"):
                    self._reverse_returned_stock(instance.business, request.user if request else None, instance, existing_item)
                instance.line_items.all().delete()
                for order, item_data in enumerate(line_items_data):
                    item_data.pop("sort_order", None)
                    return_item = SalesReturnItem.objects.create(
                        sales_return=instance,
                        sort_order=order,
                        **item_data
                    )
                    self._restore_returned_stock(instance.business, request.user if request else None, instance, return_item)

            return instance

    def _restore_returned_stock(self, business, user, sales_return, return_item):
        if not return_item.item:
            return

        actual_item = Item.objects.select_for_update().get(id=return_item.item.id)
        apply_stock_movement(
            business=business,
            item=actual_item,
            godown=actual_item.godown,
            movement_type="sales_return",
            reference_type="sales_return",
            reference_id=sales_return.id,
            quantity=return_item.quantity,
            rate=return_item.rate,
            created_by=user,
            notes=f"Returned via Sales Return {sales_return.return_number}"
        )

    def _reverse_returned_stock(self, business, user, sales_return, return_item):
        if not return_item.item:
            return

        actual_item = Item.objects.select_for_update().get(id=return_item.item.id)
        apply_stock_movement(
            business=business,
            item=actual_item,
            godown=actual_item.godown,
            movement_type="adjustment_out",
            reference_type="sales_return_update",
            reference_id=sales_return.id,
            quantity=-return_item.quantity,
            rate=return_item.rate,
            created_by=user,
            notes=f"Reversed previous Sales Return {sales_return.return_number}",
            allow_negative=False,
        )

class CreditNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = CreditNote
        fields = "__all__"
        read_only_fields = ["id", "credit_note_number", "created_at"]

    def create(self, validated_data):
        business = self.context["request"].business
        
        with transaction.atomic():
            validated_data["credit_note_number"] = _next_plain_document_number(
                business=business,
                model=CreditNote,
                field_name="credit_note_number",
            )
            validated_data["business"] = business
            
            return CreditNote.objects.create(**validated_data)
