import uuid
from django.db import models
from apps.accounts.models import Business, User
from apps.parties.models import Party
from apps.items.models import Item

class PurchaseInvoice(models.Model):
    STATUS_CHOICES = (
        ("paid", "Paid"),
        ("unpaid", "Unpaid"),
        ("partial", "Partial"),
        ("cancelled", "Cancelled"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="purchase_invoices")
    invoice_number = models.CharField(max_length=50)  # Format: PUR/26-27/0001
    supplier_invoice_number = models.CharField(max_length=100, blank=True, null=True)  # entered manually from supplier invoice
    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="purchase_invoices")
    
    invoice_date = models.DateField(auto_now_add=True)
    due_date = models.DateField(blank=True, null=True)
    
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    discount_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    taxable_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    cgst_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    sgst_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    igst_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    cess_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="unpaid")
    notes = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "purchase_invoices"
        unique_together = ("business", "invoice_number")

    def __str__(self):
        return self.invoice_number

class PurchaseInvoiceItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name="line_items")
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True)
    item_name = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    rate = models.DecimalField(max_digits=15, decimal_places=2)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    taxable_amount = models.DecimalField(max_digits=15, decimal_places=2)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "purchase_invoice_items"

class PurchaseOrder(models.Model):
    STATUS_CHOICES = (
        ("open", "Open"),
        ("converted", "Converted"),
        ("cancelled", "Cancelled"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE)
    order_number = models.CharField(max_length=50)
    party = models.ForeignKey(Party, on_delete=models.PROTECT)
    order_date = models.DateField(auto_now_add=True)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    converted_invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "purchase_orders"

class PurchaseOrderItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="line_items")
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True)
    item_name = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    rate = models.DecimalField(max_digits=15, decimal_places=2)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "purchase_order_items"


class PurchaseReturn(models.Model):
    STATUS_CHOICES = (
        ("adjusted", "Adjusted"),
        ("pending_adjustment", "Pending Adjustment"),
        ("cancelled", "Cancelled"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="purchase_returns")
    return_number = models.CharField(max_length=50)
    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="purchase_returns")
    original_invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.SET_NULL, null=True, blank=True, related_name="purchase_returns")
    reference_number = models.CharField(max_length=100, blank=True, null=True)
    return_date = models.DateField(auto_now_add=True)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="adjusted")
    reason = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "purchase_returns"
        unique_together = ("business", "return_number")
        indexes = [
            models.Index(fields=["business", "return_date"], name="purchase_re_busines_3f1a6b_idx"),
            models.Index(fields=["business", "status"], name="purchase_re_busines_983f84_idx"),
        ]

    def __str__(self):
        return self.return_number


class PurchaseReturnItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    purchase_return = models.ForeignKey(PurchaseReturn, on_delete=models.CASCADE, related_name="line_items")
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True)
    item_name = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    rate = models.DecimalField(max_digits=15, decimal_places=2)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    taxable_amount = models.DecimalField(max_digits=15, decimal_places=2)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "purchase_return_items"


class DebitNote(models.Model):
    STATUS_CHOICES = (
        ("unpaid", "Unpaid"),
        ("credited", "Credited"),
        ("cancelled", "Cancelled"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE)
    debit_note_number = models.CharField(max_length=50)
    party = models.ForeignKey(Party, on_delete=models.PROTECT)
    original_invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.SET_NULL, null=True, blank=True)
    note_date = models.DateField(auto_now_add=True)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="unpaid")
    reason = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "debit_notes"
