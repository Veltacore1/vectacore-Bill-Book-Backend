from decimal import Decimal
from math import ceil
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework import serializers
from .messaging import sms_provider_ready
from .models import SMSCampaign, SMSCreditLedger, SMSRecipient, SMSTemplate, OnlineOrder
from apps.parties.models import Party


def _money(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _financial_year():
    today = timezone.localdate()
    start_year = today.year if today.month >= 4 else today.year - 1
    return f"{str(start_year)[-2:]}-{str(start_year + 1)[-2:]}"


class OnlineOrderSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    item_code = serializers.CharField(source="item.item_code", read_only=True)
    current_stock = serializers.DecimalField(source="item.current_stock", max_digits=15, decimal_places=3, read_only=True)
    party_name = serializers.CharField(source="party.name", read_only=True)

    class Meta:
        model = OnlineOrder
        fields = [
            "id", "order_number", "party", "party_name", "item", "item_name", "item_code",
            "current_stock", "customer_name", "customer_mobile", "customer_email", "delivery_address",
            "delivery_city", "delivery_state", "delivery_pincode",
            "quantity", "unit_price", "taxable_amount", "tax_amount", "total_amount",
            "payment_status", "dispatch_status", "source", "shipping_provider", "shipping_status",
            "shiprocket_order_id", "shiprocket_shipment_id", "shiprocket_awb_code",
            "shiprocket_courier_name", "shipping_label_url", "tracking_url", "stock_deducted", "notes",
            "order_date", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "order_number", "unit_price", "taxable_amount", "tax_amount",
            "total_amount", "shipping_provider", "shipping_status", "shiprocket_order_id",
            "shiprocket_shipment_id", "shiprocket_awb_code", "shiprocket_courier_name",
            "shipping_label_url", "tracking_url", "stock_deducted", "order_date", "created_by",
            "created_at", "updated_at",
        ]

    def validate(self, attrs):
        business = self.context["request"].business
        item = attrs.get("item") or getattr(self.instance, "item", None)
        party = attrs.get("party") or getattr(self.instance, "party", None)
        quantity = attrs.get("quantity") or getattr(self.instance, "quantity", None)

        if item and item.business_id != business.id:
            raise serializers.ValidationError({"item": "Choose an item from the active tenant."})
        if party and party.business_id != business.id:
            raise serializers.ValidationError({"party": "Choose a customer from the active tenant."})
        if party and party.party_type not in ["customer", "both"]:
            raise serializers.ValidationError({"party": "Online orders can only be linked to customer parties."})
        if quantity is not None and Decimal(str(quantity)) <= 0:
            raise serializers.ValidationError({"quantity": "Quantity must be greater than zero."})

        return attrs

    def _apply_amounts(self, validated_data):
        item = validated_data["item"]
        quantity = Decimal(str(validated_data["quantity"]))
        unit_price = _money(item.selling_price)
        taxable = _money(unit_price * quantity)
        tax = _money(taxable * (Decimal(str(item.gst_rate or 0)) / Decimal("100")))
        validated_data["unit_price"] = unit_price
        validated_data["taxable_amount"] = taxable
        validated_data["tax_amount"] = tax
        validated_data["total_amount"] = _money(taxable + tax)

    def create(self, validated_data):
        request = self.context["request"]
        business = request.business
        party = validated_data.get("party")

        if party:
            validated_data["customer_name"] = validated_data.get("customer_name") or party.name
            validated_data["customer_mobile"] = validated_data.get("customer_mobile") or party.mobile
            validated_data["customer_email"] = validated_data.get("customer_email") or party.email
            validated_data["delivery_address"] = validated_data.get("delivery_address") or party.address
            validated_data["delivery_city"] = validated_data.get("delivery_city") or party.city
            validated_data["delivery_state"] = validated_data.get("delivery_state") or party.state
            validated_data["delivery_pincode"] = validated_data.get("delivery_pincode") or party.pincode

        with transaction.atomic():
            prefix = business.invoice_prefix or "CSM"
            fy = _financial_year()
            order_prefix = f"{prefix}/ONL/{fy}/"
            last_order = OnlineOrder.objects.filter(
                business=business,
                order_number__startswith=order_prefix,
            ).order_by("-created_at").first()
            next_seq = 1
            if last_order:
                try:
                    next_seq = int(last_order.order_number.split("/")[-1]) + 1
                except (ValueError, IndexError):
                    next_seq = 1

            validated_data["business"] = business
            validated_data["created_by"] = request.user
            validated_data["order_number"] = f"{order_prefix}{next_seq:04d}"
            self._apply_amounts(validated_data)
            return OnlineOrder.objects.create(**validated_data)

    def update(self, instance, validated_data):
        if instance.dispatch_status != "new" and any(key in validated_data for key in ["item", "quantity"]):
            raise serializers.ValidationError("Packed or dispatched orders cannot change item or quantity.")

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if "item" in validated_data or "quantity" in validated_data:
            amounts = {"item": instance.item, "quantity": instance.quantity}
            self._apply_amounts(amounts)
            instance.unit_price = amounts["unit_price"]
            instance.taxable_amount = amounts["taxable_amount"]
            instance.tax_amount = amounts["tax_amount"]
            instance.total_amount = amounts["total_amount"]

        instance.save()
        return instance


def sms_credit_balance(business):
    totals = SMSCreditLedger.objects.filter(business=business).values("entry_type").annotate(total=Sum("credits"))
    credits = sum(row["total"] or 0 for row in totals if row["entry_type"] == "credit")
    debits = sum(row["total"] or 0 for row in totals if row["entry_type"] == "debit")
    return credits - debits


class SMSTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SMSTemplate
        fields = ["id", "name", "category", "message", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        validated_data["business"] = self.context["request"].business
        return SMSTemplate.objects.create(**validated_data)


class SMSRecipientSerializer(serializers.ModelSerializer):
    class Meta:
        model = SMSRecipient
        fields = [
            "id", "party", "party_name", "mobile", "status",
            "provider", "provider_message_id", "sent_at", "delivered_at",
            "error_message", "created_at",
        ]
        read_only_fields = fields


class SMSCreditLedgerSerializer(serializers.ModelSerializer):
    class Meta:
        model = SMSCreditLedger
        fields = ["id", "entry_type", "credits", "reference", "notes", "created_at"]
        read_only_fields = fields


class SMSCampaignSerializer(serializers.ModelSerializer):
    template_name = serializers.CharField(source="template.name", read_only=True)
    recipients = SMSRecipientSerializer(many=True, read_only=True)
    party_ids = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        allow_empty=True,
    )
    send_now = serializers.BooleanField(write_only=True, required=False, default=True)

    class Meta:
        model = SMSCampaign
        fields = [
            "id", "campaign_number", "name", "template", "template_name",
            "audience", "message", "recipient_count", "delivered_count",
            "failed_count", "credit_cost", "status", "queued_at",
            "completed_at", "created_by", "created_at", "updated_at",
            "recipients", "party_ids", "send_now",
        ]
        read_only_fields = [
            "id", "campaign_number", "recipient_count", "delivered_count",
            "failed_count", "credit_cost", "status", "queued_at",
            "completed_at", "created_by", "created_at", "updated_at",
            "recipients",
        ]

    def validate(self, attrs):
        business = self.context["request"].business
        template = attrs.get("template")
        audience = attrs.get("audience", "all_customers")
        party_ids = attrs.get("party_ids", [])
        message = (attrs.get("message") or "").strip()

        if template and template.business_id != business.id:
            raise serializers.ValidationError({"template": "Choose a template from the active tenant."})
        if not message:
            raise serializers.ValidationError({"message": "Message is required."})
        if audience == "manual" and not party_ids:
            raise serializers.ValidationError({"party_ids": "Choose at least one customer for a selected audience campaign."})

        return attrs

    def _recipient_parties(self, business, audience, party_ids):
        queryset = Party.objects.filter(
            business=business,
            is_active=True,
            party_type__in=["customer", "both"],
            mobile__isnull=False,
        ).exclude(mobile__exact="").exclude(mobile="-").order_by("name")

        if audience == "manual":
            queryset = queryset.filter(id__in=party_ids)

        return list(queryset)

    def create(self, validated_data):
        request = self.context["request"]
        business = request.business
        party_ids = validated_data.pop("party_ids", [])
        send_now = validated_data.pop("send_now", True)
        audience = validated_data.get("audience", "all_customers")
        message = validated_data["message"].strip()
        recipients = self._recipient_parties(business, audience, party_ids)
        segments = max(1, ceil(len(message) / 160))
        credit_cost = len(recipients) * segments

        if send_now and not recipients:
            raise serializers.ValidationError({"audience": "No reachable customers with mobile numbers found."})
        if send_now:
            ready, provider, message = sms_provider_ready()
            if not ready:
                raise serializers.ValidationError({"provider": message or f"SMS provider {provider} is not ready."})
        if send_now and sms_credit_balance(business) < credit_cost:
            raise serializers.ValidationError({"credits": "Not enough SMS credits for this campaign."})

        with transaction.atomic():
            prefix = business.invoice_prefix or "CSM"
            fy = _financial_year()
            campaign_prefix = f"{prefix}/SMS/{fy}/"
            last_campaign = SMSCampaign.objects.filter(
                business=business,
                campaign_number__startswith=campaign_prefix,
            ).order_by("-created_at").first()
            next_seq = 1
            if last_campaign:
                try:
                    next_seq = int(last_campaign.campaign_number.split("/")[-1]) + 1
                except (ValueError, IndexError):
                    next_seq = 1

            validated_data["business"] = business
            validated_data["created_by"] = request.user
            validated_data["campaign_number"] = f"{campaign_prefix}{next_seq:04d}"
            validated_data["recipient_count"] = len(recipients)
            validated_data["credit_cost"] = credit_cost
            validated_data["status"] = "queued" if send_now else "draft"
            if send_now:
                validated_data["queued_at"] = timezone.now()

            campaign = SMSCampaign.objects.create(**validated_data)
            SMSRecipient.objects.bulk_create([
                SMSRecipient(
                    business=business,
                    campaign=campaign,
                    party=party,
                    party_name=party.name,
                    mobile=party.mobile,
                    status="queued" if send_now else "queued",
                )
                for party in recipients
            ])

            if send_now and credit_cost:
                SMSCreditLedger.objects.create(
                    business=business,
                    entry_type="debit",
                    credits=credit_cost,
                    reference=campaign.campaign_number,
                    notes=f"Queued {campaign.name} to {len(recipients)} customers",
                )

            return campaign
