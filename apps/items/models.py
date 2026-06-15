import io
import re
import uuid
from django.db import models
from django.db.models import Sum
from rest_framework.exceptions import ValidationError
from apps.accounts.models import Business, User
from apps.parties.models import Party
from decimal import Decimal
from barcode import Code128
from barcode.writer import SVGWriter

class ItemCategory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="item_categories")
    name = models.CharField(max_length=100)

    class Meta:
        db_table = "item_categories"
        unique_together = ("business", "name")

    def __str__(self):
        return self.name

class Godown(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="godowns")
    name = models.CharField(max_length=100)
    address = models.TextField(blank=True, null=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        db_table = "godowns"

    def __str__(self):
        return self.name

class ItemGodownStock(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="item_godown_stocks")
    item = models.ForeignKey("Item", on_delete=models.CASCADE, related_name="godown_stocks")
    godown = models.ForeignKey(Godown, on_delete=models.CASCADE, related_name="item_stocks")
    opening_stock = models.DecimalField(max_digits=15, decimal_places=3, default=0.000)
    current_stock = models.DecimalField(max_digits=15, decimal_places=3, default=0.000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "item_godown_stocks"
        unique_together = ("business", "item", "godown")
        indexes = [
            models.Index(fields=["business", "godown"]),
            models.Index(fields=["business", "item"]),
        ]

    def __str__(self):
        return f"{self.item} @ {self.godown}: {self.current_stock}"

class Item(models.Model):
    UNIT_CHOICES = (
        ("PCS", "Pieces (PCS)"),
        ("KG", "Kilogram (KG)"),
        ("MTR", "Meter (MTR)"),
        ("BOX", "Box (BOX)"),
        ("SET", "Set (SET)"),
        ("DOZ", "Dozen (DOZ)"),
        ("PAIR", "Pair (PAIR)"),
    )

    GST_RATE_CHOICES = (
        (0.00, "0%"),
        (5.00, "5%"),
        (12.00, "12%"),
        (18.00, "18%"),
        (28.00, "28%"),
    )

    DISCOUNT_TYPE_CHOICES = (
        ("percent", "Percentage (%)"),
        ("flat", "Flat Amount (₹)"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="items")
    name = models.CharField(max_length=255)
    item_code = models.CharField(max_length=100, blank=True, null=True)  # SKU / barcode
    barcode = models.CharField(max_length=100, blank=True, null=True)
    hsn_code = models.CharField(max_length=20, blank=True, null=True)
    category = models.ForeignKey(ItemCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="items")
    unit = models.CharField(max_length=20, choices=UNIT_CHOICES, default="PCS")
    
    # Pricing
    selling_price = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    purchase_price = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    mrp = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True)
    wholesale_price = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True)
    
    # Taxes
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, choices=GST_RATE_CHOICES, default=0.00)
    cess_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    tax_inclusive = models.BooleanField(default=False)
    
    # Inventory Stock
    opening_stock = models.DecimalField(max_digits=15, decimal_places=3, default=0.000)
    current_stock = models.DecimalField(max_digits=15, decimal_places=3, default=0.000)
    low_stock_qty = models.DecimalField(max_digits=15, decimal_places=3, blank=True, null=True)
    godown = models.ForeignKey(Godown, on_delete=models.SET_NULL, null=True, blank=True, related_name="items")
    secondary_unit = models.CharField(max_length=20, blank=True, null=True)
    serialisation_enabled = models.BooleanField(default=False)
    
    # Discount
    default_discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    default_discount_type = models.CharField(max_length=10, choices=DISCOUNT_TYPE_CHOICES, default="percent")
    
    # Meta
    show_online_store = models.BooleanField(default=False)
    color = models.CharField(max_length=100, blank=True, null=True)
    cin_date = models.CharField(max_length=100, blank=True, null=True)
    grn_date = models.CharField(max_length=100, blank=True, null=True)
    bill_no = models.CharField(max_length=100, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "items"

    def __str__(self):
        return self.name

class StockMovement(models.Model):
    MOVEMENT_TYPE_CHOICES = (
        ("sale", "Sale Stock Out"),
        ("purchase", "Purchase Stock In"),
        ("sales_return", "Sales Return In"),
        ("purchase_return", "Purchase Return Out"),
        ("adjustment_in", "Stock Adjustment In"),
        ("adjustment_out", "Stock Adjustment Out"),
        ("opening", "Opening Stock Entry"),
        ("transfer", "Godown Transfer"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="stock_movements")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="stock_movements")
    godown = models.ForeignKey(Godown, on_delete=models.SET_NULL, null=True, blank=True)
    movement_type = models.CharField(max_length=50, choices=MOVEMENT_TYPE_CHOICES)
    reference_type = models.CharField(max_length=50, blank=True, null=True)  # sales_invoice, purchase_invoice, etc.
    reference_id = models.UUIDField(blank=True, null=True)
    quantity = models.DecimalField(max_digits=15, decimal_places=3)  # Pos for IN, Neg for OUT
    rate = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True)
    balance_after = models.DecimalField(max_digits=15, decimal_places=3)
    movement_date = models.DateField(auto_now_add=True)
    notes = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "stock_movements"

class GodownTransfer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    from_godown = models.ForeignKey(Godown, on_delete=models.CASCADE, related_name="transfers_out")
    to_godown = models.ForeignKey(Godown, on_delete=models.CASCADE, related_name="transfers_in")
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    transfer_date = models.DateField(auto_now_add=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "godown_transfers"

class PriceHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    party = models.ForeignKey(Party, on_delete=models.CASCADE)
    voucher_type = models.CharField(max_length=50)  # sales_invoice, purchase_invoice
    rate = models.DecimalField(max_digits=15, decimal_places=2)
    transaction_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "price_history"


class ItemPartyPrice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="item_party_prices")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="party_prices")
    party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name="item_prices")
    sales_price = models.DecimalField(max_digits=15, decimal_places=2)
    tax_inclusive = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "item_party_prices"
        unique_together = ("business", "item", "party")
        indexes = [
            models.Index(fields=["business", "item"]),
            models.Index(fields=["business", "party"]),
        ]

    def __str__(self):
        return f"{self.party.name} - {self.item.name}: {self.sales_price}"


class ItemOffer(models.Model):
    DISCOUNT_TYPE_CHOICES = (
        ("percent", "Percentage"),
        ("flat", "Flat Amount"),
    )

    STATUS_CHOICES = (
        ("draft", "Draft"),
        ("active", "Active"),
        ("paused", "Paused"),
        ("expired", "Expired"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="item_offers")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="offers")
    title = models.CharField(max_length=160)
    discount_type = models.CharField(max_length=10, choices=DISCOUNT_TYPE_CHOICES, default="percent")
    discount_value = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    starts_on = models.DateField(blank=True, null=True)
    ends_on = models.DateField(blank=True, null=True)
    channel = models.CharField(max_length=60, default="billing")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    notes = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "item_offers"
        indexes = [
            models.Index(fields=["business", "status"]),
            models.Index(fields=["business", "item"]),
            models.Index(fields=["business", "starts_on", "ends_on"]),
        ]

    def __str__(self):
        return f"{self.title} - {self.item.name}"

    @property
    def offer_price(self):
        base_price = self.item.selling_price or Decimal("0.00")
        discount = Decimal(str(self.discount_value or 0))
        if self.discount_type == "flat":
            return max(Decimal("0.00"), base_price - discount)
        return max(Decimal("0.00"), base_price - (base_price * discount / Decimal("100")))


BARCODE_LABEL_SIZES = {
    "50x25": {
        "id": "50x25",
        "name": "50 x 25 mm",
        "width_mm": 50,
        "height_mm": 25,
        "columns": 3,
        "gap_mm": 3,
        "description": "Standard jewellery and textile SKU label",
    },
    "38x25": {
        "id": "38x25",
        "name": "38 x 25 mm",
        "width_mm": 38,
        "height_mm": 25,
        "columns": 4,
        "gap_mm": 2,
        "description": "Compact barcode label",
    },
    "65x35": {
        "id": "65x35",
        "name": "65 x 35 mm",
        "width_mm": 65,
        "height_mm": 35,
        "columns": 2,
        "gap_mm": 4,
        "description": "Large price and MRP label",
    },
    "100x50": {
        "id": "100x50",
        "name": "100 x 50 mm",
        "width_mm": 100,
        "height_mm": 50,
        "columns": 1,
        "gap_mm": 4,
        "description": "Wide carton or shelf label",
    },
    "thermal_58": {
        "id": "thermal_58",
        "name": "58 mm Thermal",
        "width_mm": 58,
        "height_mm": 32,
        "columns": 1,
        "gap_mm": 2,
        "description": "Single-column thermal printer roll",
    },
}


def get_barcode_label_size(label_size):
    return BARCODE_LABEL_SIZES.get(label_size) or BARCODE_LABEL_SIZES["50x25"]


def _tracking_token(value, fallback, length):
    clean_value = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    return (clean_value[:length] or fallback)[:length]


def make_item_tracking_code(business, item):
    business_token = _tracking_token(business.invoice_prefix or business.name, "ITM", 3)
    category_name = item.category.name if getattr(item, "category", None) else item.name
    category_token = _tracking_token(category_name, "GEN", 3)
    color_token = _tracking_token(getattr(item, "color", None), "STD", 3)
    suffix = str(item.id).replace("-", "").upper()[-6:]
    return f"{business_token}-{category_token}-{color_token}-{suffix}"


def make_item_barcode_value(business, item):
    return item.item_code or make_item_tracking_code(business, item)


def generate_barcode_svg(value):
    clean_value = (value or "").strip()
    if not clean_value:
        clean_value = "0"
    buffer = io.BytesIO()
    barcode = Code128(clean_value, writer=SVGWriter())
    barcode.write(
        buffer,
        options={
            "module_height": 8,
            "module_width": 0.28,
            "font_size": 6,
            "text_distance": 1.5,
            "quiet_zone": 1,
            "write_text": False,
        },
    )
    svg = buffer.getvalue().decode("utf-8")
    start = svg.find("<svg")
    return svg[start:] if start >= 0 else svg


class BarcodeLabel(models.Model):
    PRICE_SOURCE_CHOICES = (
        ("selling", "Selling Price"),
        ("mrp", "MRP"),
        ("none", "No Price"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="barcode_labels")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="barcode_labels")
    barcode_value = models.CharField(max_length=128)
    label_size = models.CharField(max_length=30, default="50x25")
    copies = models.PositiveIntegerField(default=1)
    price_source = models.CharField(max_length=20, choices=PRICE_SOURCE_CHOICES, default="selling")
    include_business_name = models.BooleanField(default=True)
    include_item_name = models.BooleanField(default=True)
    include_price = models.BooleanField(default=True)
    include_mrp = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "barcode_labels"
        indexes = [
            models.Index(fields=["business", "item"]),
            models.Index(fields=["business", "created_at"]),
        ]

    def __str__(self):
        return f"{self.barcode_value} - {self.item.name}"


def ensure_default_godown(business):
    godown = Godown.objects.filter(business=business, is_default=True).order_by("name").first()
    if godown:
        return godown
    godown = Godown.objects.filter(business=business).order_by("name").first()
    if godown:
        if not godown.is_default:
            godown.is_default = True
            godown.save(update_fields=["is_default"])
        return godown
    return Godown.objects.create(business=business, name="Main Godown", is_default=True)


def resolve_stock_godown(business, item, godown=None):
    if godown:
        return godown
    if item.godown_id:
        return item.godown
    godown = ensure_default_godown(business)
    item.godown = godown
    item.save(update_fields=["godown", "updated_at"])
    return godown


def sync_item_current_stock(item):
    total = ItemGodownStock.objects.filter(
        business=item.business,
        item=item,
    ).aggregate(total=Sum("current_stock"))["total"]
    item.current_stock = total or Decimal("0.000")
    item.save(update_fields=["current_stock", "updated_at"])
    return item.current_stock


def apply_stock_movement(
    *,
    business,
    item,
    quantity,
    movement_type,
    godown=None,
    reference_type=None,
    reference_id=None,
    rate=None,
    created_by=None,
    notes=None,
    allow_negative=True,
):
    quantity = Decimal(str(quantity))
    item = Item.objects.select_for_update().get(id=item.id, business=business)
    godown = resolve_stock_godown(business, item, godown)
    stock, _ = ItemGodownStock.objects.select_for_update().get_or_create(
        business=business,
        item=item,
        godown=godown,
        defaults={"opening_stock": Decimal("0.000"), "current_stock": Decimal("0.000")},
    )
    next_stock = stock.current_stock + quantity
    if not allow_negative and next_stock < 0:
        raise ValidationError(f"Only {stock.current_stock:g} {item.unit} available in {godown.name}.")
    stock.current_stock = next_stock
    stock.save(update_fields=["current_stock", "updated_at"])
    sync_item_current_stock(item)
    movement = StockMovement.objects.create(
        business=business,
        item=item,
        godown=godown,
        movement_type=movement_type,
        reference_type=reference_type,
        reference_id=reference_id,
        quantity=quantity,
        rate=rate,
        balance_after=stock.current_stock,
        created_by=created_by,
        notes=notes,
    )
    return stock, movement
