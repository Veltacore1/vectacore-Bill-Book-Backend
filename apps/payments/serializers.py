from rest_framework import serializers
from django.db import transaction
from .models import PaymentGatewayOrder, PaymentIn, PaymentInSettlement, PaymentOut, PaymentOutSettlement
from apps.accounts.sequences import next_model_document_number
from apps.sales.models import SalesInvoice
from apps.purchases.models import PurchaseInvoice
from apps.parties.models import Party
from decimal import Decimal


def _set_invoice_payment_status(invoice):
    if invoice.status == "cancelled":
        invoice.save(update_fields=["paid_amount", "updated_at"])
        return
    if invoice.paid_amount >= invoice.total_amount:
        invoice.status = "paid"
    elif invoice.paid_amount > 0:
        invoice.status = "partial"
    else:
        invoice.status = "unpaid"
    invoice.save(update_fields=["paid_amount", "status", "updated_at"])


def _reverse_payment_in_settlements(payment):
    for settlement in payment.settlements.select_related("invoice"):
        invoice = settlement.invoice
        invoice.paid_amount = max(Decimal("0.00"), invoice.paid_amount - settlement.settled_amount)
        _set_invoice_payment_status(invoice)
    payment.settlements.all().delete()


def _reverse_payment_out_settlements(payment):
    for settlement in payment.settlements.select_related("invoice"):
        invoice = settlement.invoice
        invoice.paid_amount = max(Decimal("0.00"), invoice.paid_amount - settlement.settled_amount)
        _set_invoice_payment_status(invoice)
    payment.settlements.all().delete()


def _next_payment_number(model, business, prefix):
    return next_model_document_number(
        business=business,
        sequence_key=f"{prefix.lower()}:{prefix}",
        model=model,
        field_name="payment_number",
        number_prefix=f"{prefix}-",
    )


def _validate_payment_party(request, party, expected_type):
    if not request.business:
        raise serializers.ValidationError("No active tenant business.")
    if party.business_id != request.business.id:
        raise serializers.ValidationError("Party is not available for this tenant.")
    if party.party_type != expected_type:
        label = "customer" if expected_type == "customer" else "supplier"
        raise serializers.ValidationError(f"Select a {label} for this payment.")


def _validate_positive_amount(value, label):
    amount = Decimal(str(value or 0))
    if amount <= 0:
        raise serializers.ValidationError({label: "Payment amount must be greater than zero."})
    return amount


def _apply_payment_in_settlements(payment, allocations=None):
    remaining = Decimal(str(payment.amount_received))
    allocations = allocations or []

    if allocations:
        for allocation in allocations:
            if remaining <= 0:
                break
            invoice_id = allocation.get("invoice")
            requested = Decimal(str(allocation.get("settled_amount") or 0))
            if not invoice_id or requested <= 0:
                raise serializers.ValidationError("Each settlement needs an invoice and amount greater than zero.")
            invoice = SalesInvoice.objects.select_for_update().filter(
                id=invoice_id,
                business=payment.business,
                party=payment.party,
            ).exclude(status="cancelled").first()
            if not invoice:
                raise serializers.ValidationError("Settlement invoice is not available for this customer.")
            unpaid_amt = invoice.total_amount - invoice.paid_amount
            if unpaid_amt <= 0:
                raise serializers.ValidationError(f"Invoice {invoice.invoice_number} is already fully paid.")
            if requested > unpaid_amt:
                raise serializers.ValidationError(f"Settlement exceeds pending amount for {invoice.invoice_number}.")
            if requested > remaining:
                raise serializers.ValidationError("Settlement amount exceeds payment amount.")
            invoice.paid_amount += requested
            _set_invoice_payment_status(invoice)
            PaymentInSettlement.objects.create(
                payment_in=payment,
                invoice=invoice,
                settled_amount=requested,
            )
            remaining -= requested
    else:
        open_invoices = SalesInvoice.objects.select_for_update().filter(
            business=payment.business,
            party=payment.party
        ).exclude(status__in=["paid", "cancelled"]).order_by("invoice_date", "created_at")

        for invoice in open_invoices:
            if remaining <= 0:
                break
            unpaid_amt = invoice.total_amount - invoice.paid_amount
            settle_amt = min(remaining, unpaid_amt)
            invoice.paid_amount += settle_amt
            _set_invoice_payment_status(invoice)
            PaymentInSettlement.objects.create(
                payment_in=payment,
                invoice=invoice,
                settled_amount=settle_amt,
            )
            remaining -= settle_amt

    payment.is_advance = remaining > 0
    payment.save(update_fields=["is_advance"])


def _apply_payment_out_settlements(payment, allocations=None):
    remaining = Decimal(str(payment.amount_paid))
    allocations = allocations or []

    if allocations:
        for allocation in allocations:
            if remaining <= 0:
                break
            invoice_id = allocation.get("invoice")
            requested = Decimal(str(allocation.get("settled_amount") or 0))
            if not invoice_id or requested <= 0:
                raise serializers.ValidationError("Each settlement needs an invoice and amount greater than zero.")
            invoice = PurchaseInvoice.objects.select_for_update().filter(
                id=invoice_id,
                business=payment.business,
                party=payment.party,
            ).exclude(status="cancelled").first()
            if not invoice:
                raise serializers.ValidationError("Settlement invoice is not available for this supplier.")
            unpaid_amt = invoice.total_amount - invoice.paid_amount
            if unpaid_amt <= 0:
                raise serializers.ValidationError(f"Invoice {invoice.invoice_number} is already fully paid.")
            if requested > unpaid_amt:
                raise serializers.ValidationError(f"Settlement exceeds pending amount for {invoice.invoice_number}.")
            if requested > remaining:
                raise serializers.ValidationError("Settlement amount exceeds payment amount.")
            invoice.paid_amount += requested
            _set_invoice_payment_status(invoice)
            PaymentOutSettlement.objects.create(
                payment_out=payment,
                invoice=invoice,
                settled_amount=requested,
            )
            remaining -= requested
    else:
        open_invoices = PurchaseInvoice.objects.select_for_update().filter(
            business=payment.business,
            party=payment.party
        ).exclude(status__in=["paid", "cancelled"]).order_by("invoice_date", "created_at")

        for invoice in open_invoices:
            if remaining <= 0:
                break
            unpaid_amt = invoice.total_amount - invoice.paid_amount
            settle_amt = min(remaining, unpaid_amt)
            invoice.paid_amount += settle_amt
            _set_invoice_payment_status(invoice)
            PaymentOutSettlement.objects.create(
                payment_out=payment,
                invoice=invoice,
                settled_amount=settle_amt,
            )
            remaining -= settle_amt

    payment.is_advance = remaining > 0
    payment.save(update_fields=["is_advance"])

class PaymentInSettlementSerializer(serializers.ModelSerializer):
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)

    class Meta:
        model = PaymentInSettlement
        fields = ["id", "invoice", "invoice_number", "settled_amount"]

class PaymentInSerializer(serializers.ModelSerializer):
    settlements = PaymentInSettlementSerializer(many=True, read_only=True)
    party_name = serializers.CharField(source="party.name", read_only=True)
    settlement_allocations = serializers.ListField(
        child=serializers.DictField(),
        write_only=True,
        required=False,
    )

    class Meta:
        model = PaymentIn
        fields = [
            "id", "payment_number", "party", "party_name", "payment_date",
            "amount_received", "payment_mode", "reference_number", 
            "bank_account_id", "is_advance", "status", "cancellation_reason",
            "cancelled_at", "cancelled_by", "notes", "settlements",
            "settlement_allocations", "created_by", "created_at"
        ]
        read_only_fields = [
            "id", "payment_number", "is_advance", "status", "cancellation_reason",
            "cancelled_at", "cancelled_by", "created_by", "created_at"
        ]

    def validate(self, attrs):
        request = self.context["request"]
        party = attrs.get("party") or getattr(self.instance, "party", None)
        if party:
            _validate_payment_party(request, party, "customer")
        _validate_positive_amount(
            attrs.get("amount_received", getattr(self.instance, "amount_received", 0)),
            "amount_received",
        )
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        business = request.business
        settlement_allocations = validated_data.pop("settlement_allocations", None)
        
        with transaction.atomic():
            validated_data["payment_number"] = _next_payment_number(PaymentIn, business, "PMTIN")
            validated_data["business"] = business
            validated_data["created_by"] = request.user
            
            payment = PaymentIn.objects.create(**validated_data)
            _apply_payment_in_settlements(payment, settlement_allocations)
                
            return payment

    def update(self, instance, validated_data):
        if instance.status == "void":
            raise serializers.ValidationError("Voided payments cannot be edited.")

        request = self.context["request"]
        settlement_allocations = validated_data.pop("settlement_allocations", None)
        with transaction.atomic():
            payment = PaymentIn.objects.select_for_update().get(id=instance.id, business=request.business)
            _reverse_payment_in_settlements(payment)

            for attr, value in validated_data.items():
                if attr in {"payment_number", "business", "created_by", "status"}:
                    continue
                setattr(payment, attr, value)
            payment.is_advance = False
            payment.save()

            _apply_payment_in_settlements(payment, settlement_allocations)

            return payment

class PaymentOutSettlementSerializer(serializers.ModelSerializer):
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)

    class Meta:
        model = PaymentOutSettlement
        fields = ["id", "invoice", "invoice_number", "settled_amount"]

class PaymentOutSerializer(serializers.ModelSerializer):
    settlements = PaymentOutSettlementSerializer(many=True, read_only=True)
    party_name = serializers.CharField(source="party.name", read_only=True)
    settlement_allocations = serializers.ListField(
        child=serializers.DictField(),
        write_only=True,
        required=False,
    )

    class Meta:
        model = PaymentOut
        fields = [
            "id", "payment_number", "party", "party_name", "payment_date",
            "amount_paid", "payment_mode", "reference_number", 
            "bank_account_id", "is_advance", "status", "cancellation_reason",
            "cancelled_at", "cancelled_by", "notes", "settlements",
            "settlement_allocations", "created_by", "created_at"
        ]
        read_only_fields = [
            "id", "payment_number", "is_advance", "status", "cancellation_reason",
            "cancelled_at", "cancelled_by", "created_by", "created_at"
        ]

    def validate(self, attrs):
        request = self.context["request"]
        party = attrs.get("party") or getattr(self.instance, "party", None)
        if party:
            _validate_payment_party(request, party, "supplier")
        _validate_positive_amount(
            attrs.get("amount_paid", getattr(self.instance, "amount_paid", 0)),
            "amount_paid",
        )
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        business = request.business
        settlement_allocations = validated_data.pop("settlement_allocations", None)
        
        with transaction.atomic():
            validated_data["payment_number"] = _next_payment_number(PaymentOut, business, "PMTOUT")
            validated_data["business"] = business
            validated_data["created_by"] = request.user
            
            payment = PaymentOut.objects.create(**validated_data)
            _apply_payment_out_settlements(payment, settlement_allocations)
                
            return payment


class PaymentGatewayOrderSerializer(serializers.ModelSerializer):
    party_name = serializers.CharField(source="party.name", read_only=True)
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)
    payment_number = serializers.CharField(source="payment_in.payment_number", read_only=True)

    class Meta:
        model = PaymentGatewayOrder
        fields = [
            "id", "provider", "provider_order_id", "provider_payment_id",
            "provider_status", "receipt", "amount", "amount_subunits",
            "currency", "status", "signature_verified", "party", "party_name",
            "invoice", "invoice_number", "payment_in", "payment_number",
            "notes", "created_at", "updated_at", "paid_at",
        ]
        read_only_fields = fields


class PaymentGatewayOrderCreateSerializer(serializers.Serializer):
    party = serializers.PrimaryKeyRelatedField(queryset=Party.objects.all())
    invoice = serializers.PrimaryKeyRelatedField(queryset=SalesInvoice.objects.all(), required=False, allow_null=True)
    amount = serializers.DecimalField(max_digits=15, decimal_places=2)
    notes = serializers.DictField(required=False)

    def validate_amount(self, value):
        if value <= Decimal("0.00"):
            raise serializers.ValidationError("Payment amount must be greater than zero.")
        return value


class PaymentGatewayVerifySerializer(serializers.Serializer):
    razorpay_order_id = serializers.CharField(max_length=100)
    razorpay_payment_id = serializers.CharField(max_length=100)
    razorpay_signature = serializers.CharField(max_length=200)

    def update(self, instance, validated_data):
        if instance.status == "void":
            raise serializers.ValidationError("Voided payments cannot be edited.")

        request = self.context["request"]
        settlement_allocations = validated_data.pop("settlement_allocations", None)
        with transaction.atomic():
            payment = PaymentOut.objects.select_for_update().get(id=instance.id, business=request.business)
            _reverse_payment_out_settlements(payment)

            for attr, value in validated_data.items():
                if attr in {"payment_number", "business", "created_by", "status"}:
                    continue
                setattr(payment, attr, value)
            payment.is_advance = False
            payment.save()

            _apply_payment_out_settlements(payment, settlement_allocations)

            return payment
