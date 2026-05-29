import uuid
from django.db import models
from apps.accounts.models import Business, User
from apps.parties.models import Party
from apps.items.models import Item

class SalesInvoice(models.Model):
    STATUS_CHOICES = (
        ("paid", "Paid"),
        ("unpaid", "Unpaid"),
        ("partial", "Partial"),
        ("cancelled", "Cancelled"),
    )
    EINVOICE_STATUS_CHOICES = (
        ("pending", "Pending"),
        ("generated", "Generated"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="sales_invoices")
    invoice_number = models.CharField(max_length=50)  # Format: CSM/26-27/1629
    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="sales_invoices")
    
    # Dates
    invoice_date = models.DateField(auto_now_add=True)
    due_date = models.DateField(blank=True, null=True)
    
    # Logistics / Transport (Textile custom)
    place_of_supply = models.CharField(max_length=100, blank=True, null=True)
    vehicle_no = models.CharField(max_length=50, blank=True, null=True)
    transport_name = models.CharField(max_length=100, blank=True, null=True)
    lr_no = models.CharField(max_length=100, blank=True, null=True)
    eway_bill_no = models.CharField(max_length=50, blank=True, null=True)
    grn_no = models.CharField(max_length=100, blank=True, null=True)
    grn_date = models.DateField(blank=True, null=True)
    cin_no = models.CharField(max_length=100, blank=True, null=True)
    cin_date = models.DateField(blank=True, null=True)
    
    # Financial summary
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    discount_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    taxable_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    cgst_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    sgst_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    igst_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    cess_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    additional_charges = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    additional_charges_label = models.CharField(max_length=100, blank=True, null=True)
    round_off = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    
    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="unpaid")
    cancellation_reason = models.TextField(blank=True, null=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)
    cancelled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="cancelled_sales")
    
    # E-Invoicing integration
    irn = models.CharField(max_length=100, blank=True, null=True)
    ack_number = models.CharField(max_length=100, blank=True, null=True)
    ack_date = models.DateTimeField(blank=True, null=True)
    qr_code_data = models.TextField(blank=True, null=True)
    einvoice_status = models.CharField(max_length=20, choices=EINVOICE_STATUS_CHOICES, default="pending")
    einvoice_provider = models.CharField(max_length=50, default="local_stub")
    einvoice_retry_count = models.PositiveIntegerField(default=0)
    einvoice_last_error = models.TextField(blank=True, null=True)
    einvoice_cancel_reason = models.TextField(blank=True, null=True)
    einvoice_cancelled_at = models.DateTimeField(blank=True, null=True)
    
    # Notes & Config
    notes = models.TextField(blank=True, null=True)
    terms = models.TextField(blank=True, null=True)
    is_pos = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sales_invoices"
        unique_together = ("business", "invoice_number")

    def __str__(self):
        return self.invoice_number

class EInvoiceLog(models.Model):
    EVENT_CHOICES = (
        ("generate", "Generate"),
        ("retry", "Retry"),
        ("cancel", "Cancel"),
        ("status_sync", "Status Sync"),
    )
    STATUS_CHOICES = (
        ("success", "Success"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="einvoice_logs")
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name="einvoice_logs")
    event = models.CharField(max_length=30, choices=EVENT_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    provider = models.CharField(max_length=50, default="local_stub")
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    message = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="einvoice_logs")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "einvoice_logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["business", "created_at"], name="einvoice_lo_busines_e624ea_idx"),
            models.Index(fields=["invoice", "created_at"], name="einvoice_lo_invoice_0162c8_idx"),
        ]

class SalesInvoiceItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name="line_items")
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True)
    
    # Snapshot fields at invoice date
    item_name = models.CharField(max_length=255)
    item_code = models.CharField(max_length=100, blank=True, null=True)
    hsn_code = models.CharField(max_length=20, blank=True, null=True)
    unit = models.CharField(max_length=20, default="PCS")
    
    # Saree custom fields
    color = models.CharField(max_length=100, blank=True, null=True)
    cin_no = models.CharField(max_length=100, blank=True, null=True)
    cin_date = models.DateField(blank=True, null=True)
    grn_no = models.CharField(max_length=100, blank=True, null=True)
    grn_date = models.DateField(blank=True, null=True)
    
    # Quantities
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    free_quantity = models.DecimalField(max_digits=15, decimal_places=3, default=0.000)
    
    # Pricing
    mrp = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True)
    rate = models.DecimalField(max_digits=15, decimal_places=2)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    discount_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    
    # Tax breakdown
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    taxable_amount = models.DecimalField(max_digits=15, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "sales_invoice_items"

class Quotation(models.Model):
    STATUS_CHOICES = (
        ("open", "Open"),
        ("converted", "Converted"),
        ("cancelled", "Cancelled"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE)
    quotation_number = models.CharField(max_length=50)  # simple sequential: 38, 36...
    party = models.ForeignKey(Party, on_delete=models.PROTECT)
    quotation_date = models.DateField(auto_now_add=True)
    valid_till = models.DateField(blank=True, null=True)
    
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    converted_invoice = models.ForeignKey(SalesInvoice, on_delete=models.SET_NULL, null=True, blank=True)
    
    notes = models.TextField(blank=True, null=True)
    terms = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "quotations"

class QuotationItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="line_items")
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True)
    item_name = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    rate = models.DecimalField(max_digits=15, decimal_places=2)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "quotation_items"

class DeliveryChallan(models.Model):
    STATUS_CHOICES = (
        ("open", "Open"),
        ("converted", "Converted"),
        ("cancelled", "Cancelled"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE)
    challan_number = models.CharField(max_length=50)
    party = models.ForeignKey(Party, on_delete=models.PROTECT)
    challan_date = models.DateField(auto_now_add=True)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    converted_invoice = models.ForeignKey(SalesInvoice, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "delivery_challans"

class DeliveryChallanItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challan = models.ForeignKey(DeliveryChallan, on_delete=models.CASCADE, related_name="line_items")
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True)
    item_name = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    rate = models.DecimalField(max_digits=15, decimal_places=2)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "delivery_challan_items"

class ProformaInvoice(models.Model):
    STATUS_CHOICES = (
        ("open", "Open"),
        ("converted", "Converted"),
        ("cancelled", "Cancelled"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE)
    proforma_number = models.CharField(max_length=50)
    party = models.ForeignKey(Party, on_delete=models.PROTECT)
    proforma_date = models.DateField(auto_now_add=True)
    valid_till = models.DateField(blank=True, null=True)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    converted_invoice = models.ForeignKey(SalesInvoice, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "proforma_invoices"

class ProformaInvoiceItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proforma = models.ForeignKey(ProformaInvoice, on_delete=models.CASCADE, related_name="line_items")
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True)
    item_name = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    rate = models.DecimalField(max_digits=15, decimal_places=2)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "proforma_invoice_items"

class SalesReturn(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE)
    return_number = models.CharField(max_length=50)
    party = models.ForeignKey(Party, on_delete=models.PROTECT)
    original_invoice = models.ForeignKey(SalesInvoice, on_delete=models.SET_NULL, null=True, blank=True)
    return_date = models.DateField(auto_now_add=True)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20, default="unpaid")
    reason = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sales_returns"

class SalesReturnItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sales_return = models.ForeignKey(SalesReturn, on_delete=models.CASCADE, related_name="line_items")
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True)
    item_name = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    rate = models.DecimalField(max_digits=15, decimal_places=2)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "sales_return_items"

class CreditNote(models.Model):
    STATUS_CHOICES = (
        ("unpaid", "Unpaid"),
        ("credited", "Credited"),
        ("cancelled", "Cancelled"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE)
    credit_note_number = models.CharField(max_length=50)
    party = models.ForeignKey(Party, on_delete=models.PROTECT)
    original_invoice = models.ForeignKey(SalesInvoice, on_delete=models.SET_NULL, null=True, blank=True)
    note_date = models.DateField(auto_now_add=True)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="unpaid")
    reason = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "credit_notes"
