from rest_framework import serializers
from django.db import models, transaction
from django.utils import timezone
from .models import (
    ItemCategory, Godown, Item, ItemGodownStock, StockMovement,
    GodownTransfer, PriceHistory, ItemPartyPrice, ItemOffer, BarcodeLabel, BARCODE_LABEL_SIZES,
    apply_stock_movement, generate_barcode_svg
)
from decimal import Decimal

class ItemCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ItemCategory
        fields = ["id", "name"]

    def create(self, validated_data):
        business = self.context["request"].business
        category = ItemCategory.objects.create(business=business, **validated_data)
        return category

class GodownSerializer(serializers.ModelSerializer):
    class Meta:
        model = Godown
        fields = ["id", "name", "address", "is_default"]

    def validate_name(self, value):
        request = self.context["request"]
        queryset = Godown.objects.filter(business=request.business, name__iexact=value.strip())
        if self.instance:
            queryset = queryset.exclude(id=self.instance.id)
        if queryset.exists():
            raise serializers.ValidationError("A godown with this name already exists.")
        return value

    def create(self, validated_data):
        business = self.context["request"].business
        godown = Godown.objects.create(business=business, **validated_data)
        return godown

class ItemGodownStockSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    item_code = serializers.CharField(source="item.item_code", read_only=True)
    category_name = serializers.SerializerMethodField()
    godown_name = serializers.CharField(source="godown.name", read_only=True)
    unit = serializers.CharField(source="item.unit", read_only=True)
    purchase_price = serializers.DecimalField(source="item.purchase_price", max_digits=15, decimal_places=2, read_only=True)
    stock_value = serializers.SerializerMethodField()

    class Meta:
        model = ItemGodownStock
        fields = [
            "id", "item", "item_name", "godown", "godown_name",
            "item_code", "category_name", "unit", "purchase_price",
            "opening_stock", "current_stock", "stock_value", "created_at", "updated_at"
        ]
        read_only_fields = ["id", "current_stock", "created_at", "updated_at"]

    def get_stock_value(self, obj):
        return obj.current_stock * obj.item.purchase_price

    def get_category_name(self, obj):
        return obj.item.category.name if obj.item.category else "-"

class ItemSerializer(serializers.ModelSerializer):
    category_details = ItemCategorySerializer(source="category", read_only=True)
    godown_details = GodownSerializer(source="godown", read_only=True)
    godown_stocks = ItemGodownStockSerializer(many=True, read_only=True)
    party_prices = serializers.SerializerMethodField()
    active_offer = serializers.SerializerMethodField()
    gst_rate = serializers.DecimalField(max_digits=5, decimal_places=2)

    class Meta:
        model = Item
        fields = [
            "id", "name", "item_code", "barcode", "hsn_code", "category",
            "category_details", "unit", "selling_price", "purchase_price",
            "mrp", "wholesale_price", "gst_rate", "cess_rate", "tax_inclusive",
            "opening_stock", "current_stock", "low_stock_qty", "godown",
            "godown_details", "godown_stocks", "secondary_unit", "serialisation_enabled",
            "default_discount_pct", "default_discount_type", "show_online_store",
            "color", "cin_date", "grn_date", "bill_no", "party_prices",
            "active_offer", "is_active", "description", "created_at", "updated_at"
        ]
        read_only_fields = ["id", "current_stock", "created_at", "updated_at"]

    def get_party_prices(self, obj):
        prices = obj.party_prices.select_related("party").filter(business=obj.business).order_by("party__name")
        return ItemPartyPriceSerializer(prices, many=True).data

    def get_active_offer(self, obj):
        today = timezone.localdate()
        offer = (
            obj.offers.filter(business=obj.business, status="active")
            .filter(models.Q(starts_on__isnull=True) | models.Q(starts_on__lte=today))
            .filter(models.Q(ends_on__isnull=True) | models.Q(ends_on__gte=today))
            .order_by("-updated_at")
            .first()
        )
        return ItemOfferSerializer(offer).data if offer else None

    def validate_category(self, value):
        request = self.context["request"]
        if value and value.business_id != request.business.id:
            raise serializers.ValidationError("Choose an item category from the active tenant.")
        return value

    def validate_godown(self, value):
        request = self.context["request"]
        if value and value.business_id != request.business.id:
            raise serializers.ValidationError("Choose a godown from the active tenant.")
        return value

    def create(self, validated_data):
        request = self.context["request"]
        business = request.business

        with transaction.atomic():
            opening_stock = validated_data.get("opening_stock", Decimal("0.000"))
            validated_data["current_stock"] = opening_stock

            item = Item.objects.create(business=business, **validated_data)

            if opening_stock > 0:
                stock, movement = apply_stock_movement(
                    business=business,
                    item=item,
                    godown=item.godown,
                    movement_type="opening",
                    reference_type="item_init",
                    reference_id=item.id,
                    quantity=opening_stock,
                    rate=item.purchase_price,
                    created_by=request.user,
                    notes="Initial Opening Stock Entry"
                )
                stock.opening_stock = opening_stock
                stock.save(update_fields=["opening_stock", "updated_at"])

        return item


class ItemPartyPriceSerializer(serializers.ModelSerializer):
    party_name = serializers.CharField(source="party.name", read_only=True)
    party_mobile = serializers.CharField(source="party.mobile", read_only=True)

    class Meta:
        model = ItemPartyPrice
        fields = [
            "id", "item", "party", "party_name", "party_mobile",
            "sales_price", "tax_inclusive", "created_at", "updated_at"
        ]
        read_only_fields = ["id", "party_name", "party_mobile", "created_at", "updated_at"]

    def validate_item(self, value):
        request = self.context["request"]
        if value.business_id != request.business.id or not value.is_active:
            raise serializers.ValidationError("Choose an active item from the active tenant.")
        return value

    def validate_party(self, value):
        request = self.context["request"]
        if value.business_id != request.business.id or not value.is_active:
            raise serializers.ValidationError("Choose an active party from the active tenant.")
        return value

    def create(self, validated_data):
        request = self.context["request"]
        return ItemPartyPrice.objects.create(business=request.business, **validated_data)


class ItemOfferSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    item_code = serializers.CharField(source="item.item_code", read_only=True)
    selling_price = serializers.DecimalField(source="item.selling_price", max_digits=15, decimal_places=2, read_only=True)
    offer_price = serializers.DecimalField(max_digits=15, decimal_places=2, read_only=True)

    class Meta:
        model = ItemOffer
        fields = [
            "id", "item", "item_name", "item_code", "title", "discount_type",
            "discount_value", "selling_price", "offer_price", "starts_on",
            "ends_on", "channel", "status", "notes", "created_at", "updated_at"
        ]
        read_only_fields = ["id", "item_name", "item_code", "selling_price", "offer_price", "created_at", "updated_at"]

    def validate_item(self, value):
        request = self.context["request"]
        if value.business_id != request.business.id or not value.is_active:
            raise serializers.ValidationError("Choose an active item from the active tenant.")
        return value

    def validate_discount_value(self, value):
        if value <= 0:
            raise serializers.ValidationError("Discount must be greater than zero.")
        return value

    def validate(self, attrs):
        discount_type = attrs.get("discount_type", getattr(self.instance, "discount_type", "percent"))
        discount_value = attrs.get("discount_value", getattr(self.instance, "discount_value", Decimal("0.00")))
        starts_on = attrs.get("starts_on", getattr(self.instance, "starts_on", None))
        ends_on = attrs.get("ends_on", getattr(self.instance, "ends_on", None))

        if discount_type == "percent" and discount_value > Decimal("100.00"):
            raise serializers.ValidationError({"discount_value": "Percentage discount cannot exceed 100."})
        if starts_on and ends_on and ends_on < starts_on:
            raise serializers.ValidationError({"ends_on": "End date cannot be before start date."})
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        return ItemOffer.objects.create(business=request.business, created_by=request.user, **validated_data)

class StockMovementSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    godown_name = serializers.CharField(source="godown.name", read_only=True)

    class Meta:
        model = StockMovement
        fields = "__all__"
        read_only_fields = ["id", "balance_after", "created_at"]

class GodownTransferSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    from_godown_name = serializers.CharField(source="from_godown.name", read_only=True)
    to_godown_name = serializers.CharField(source="to_godown.name", read_only=True)

    class Meta:
        model = GodownTransfer
        fields = [
            "id", "item", "item_name", "from_godown", "from_godown_name",
            "to_godown", "to_godown_name", "quantity", "transfer_date",
            "notes", "created_at"
        ]
        read_only_fields = ["id", "item_name", "from_godown_name", "to_godown_name", "transfer_date", "created_at"]


class BarcodeLabelSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    item_code = serializers.CharField(source="item.item_code", read_only=True)
    selling_price = serializers.DecimalField(source="item.selling_price", max_digits=15, decimal_places=2, read_only=True)
    mrp = serializers.DecimalField(source="item.mrp", max_digits=15, decimal_places=2, read_only=True)
    label_size_details = serializers.SerializerMethodField()
    barcode_svg = serializers.SerializerMethodField()

    class Meta:
        model = BarcodeLabel
        fields = [
            "id", "item", "item_name", "item_code", "barcode_value",
            "label_size", "label_size_details", "copies", "price_source",
            "include_business_name", "include_item_name", "include_price",
            "include_mrp", "selling_price", "mrp", "barcode_svg",
            "created_at", "updated_at"
        ]
        read_only_fields = ["id", "barcode_svg", "created_at", "updated_at"]

    def get_label_size_details(self, obj):
        return BARCODE_LABEL_SIZES.get(obj.label_size, BARCODE_LABEL_SIZES["50x25"])

    def get_barcode_svg(self, obj):
        return generate_barcode_svg(obj.barcode_value)

    def validate_label_size(self, value):
        if value not in BARCODE_LABEL_SIZES:
            raise serializers.ValidationError("Choose a supported barcode label size.")
        return value

    def validate_copies(self, value):
        if value < 1 or value > 99:
            raise serializers.ValidationError("Copies must be between 1 and 99.")
        return value

    def validate_barcode_value(self, value):
        clean_value = (value or "").strip()
        if not clean_value:
            raise serializers.ValidationError("Barcode value is required.")
        return clean_value
