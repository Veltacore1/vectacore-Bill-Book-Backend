from rest_framework import serializers
from .models import PartyCategory, Party
from django.db.models import Sum

class PartyCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = PartyCategory
        fields = ["id", "name"]

    def create(self, validated_data):
        business = self.context["request"].business
        category = PartyCategory.objects.create(business=business, **validated_data)
        return category

class PartySerializer(serializers.ModelSerializer):
    category_details = PartyCategorySerializer(source="category", read_only=True)
    net_balance = serializers.SerializerMethodField()

    class Meta:
        model = Party
        fields = [
            "id", "name", "mobile", "email", "gstin", "pan", "address",
            "city", "state", "pincode", "party_type", "category", 
            "category_details", "opening_balance", "opening_balance_type",
            "credit_limit", "credit_days", "shared_ledger_token", "is_active",
            "net_balance", "created_at", "updated_at"
        ]
        read_only_fields = ["id", "shared_ledger_token", "created_at", "updated_at"]

    def validate_category(self, value):
        if value and value.business_id != self.context["request"].business.id:
            raise serializers.ValidationError("Choose a category from the active tenant.")
        return value

    def create(self, validated_data):
        business = self.context["request"].business
        party = Party.objects.create(business=business, **validated_data)
        return party

    def get_net_balance(self, obj):
        # Starts with opening balance
        net = obj.opening_balance if obj.opening_balance_type == "debit" else -obj.opening_balance
        
        # We can dynamically add from transactions. Since models might be queried conditionally:
        # Let's import inside the function to prevent circular imports
        from apps.sales.models import SalesInvoice, CreditNote
        from apps.purchases.models import PurchaseInvoice, DebitNote
        from apps.payments.models import PaymentIn, PaymentOut
        
        # 1. Add Sales Invoices
        sales_total = SalesInvoice.objects.filter(
            party=obj
        ).exclude(status="cancelled").aggregate(total=Sum("total_amount"))["total"] or 0
        net += sales_total

        # 2. Subtract Payments In
        payments_in_total = PaymentIn.objects.filter(
            party=obj
        ).exclude(
            status="void"
        ).aggregate(total=Sum("amount_received"))["total"] or 0
        net -= payments_in_total

        # 3. Subtract Credit Notes
        credit_total = CreditNote.objects.filter(
            party=obj, status="credited"
        ).aggregate(total=Sum("total_amount"))["total"] or 0
        net -= credit_total

        # 4. Subtract Purchase Invoices
        purchase_total = PurchaseInvoice.objects.filter(
            party=obj
        ).exclude(status="cancelled").aggregate(total=Sum("total_amount"))["total"] or 0
        net -= purchase_total

        # 5. Add Payments Out
        payments_out_total = PaymentOut.objects.filter(
            party=obj
        ).exclude(
            status="void"
        ).aggregate(total=Sum("amount_paid"))["total"] or 0
        net += payments_out_total

        # 6. Add Debit Notes
        debit_total = DebitNote.objects.filter(
            party=obj, status="credited"
        ).aggregate(total=Sum("total_amount"))["total"] or 0
        net += debit_total

        return float(net)
