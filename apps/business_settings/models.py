import uuid
from django.db import models
from apps.accounts.models import Business
from apps.parties.models import Party

class InvoiceSettings(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="invoice_settings")
    
    # Theme configuration
    theme = models.CharField(max_length=30, default="advanced_gst")  # advanced_gst, luxury, stylish, modern, simple, custom
    theme_color = models.CharField(max_length=20, default="#5B48F5")
    theme_style = models.CharField(max_length=50, blank=True, null=True)  # Uttar Pradesh, Maharashtra, Electronics, Gujarat
    
    # Toggle columns in print A4
    show_mrp = models.BooleanField(default=True)
    show_hsn = models.BooleanField(default=True)
    show_discount = models.BooleanField(default=True)
    show_color = models.BooleanField(default=True)  # textile/saree custom
    show_cin_date = models.BooleanField(default=True)  # textile/saree custom
    show_grn_date = models.BooleanField(default=True)  # textile/saree custom
    show_free_qty = models.BooleanField(default=False)
    
    # Additional print options
    show_party_balance = models.BooleanField(default=False)
    show_item_description = models.BooleanField(default=False)
    show_time_on_invoice = models.BooleanField(default=True)
    show_discount_on_mrp = models.BooleanField(default=False)
    
    # A4 Paper Settings
    paper_size = models.CharField(max_length=10, default="A4")  # A4, A5
    
    # Thermal printer settings
    thermal_paper_size = models.CharField(max_length=10, default="2inch")  # 2inch, 3inch
    thermal_theme = models.CharField(max_length=20, default="compact")  # compact, advanced
    
    # Business graphics
    logo_url = models.TextField(blank=True, null=True)
    signature_url = models.TextField(blank=True, null=True)
    
    # Up to 5 custom fields (stored as list of {label, type})
    custom_fields = models.JSONField(default=list)
    
    # Series reset preferences
    invoice_prefix = models.CharField(max_length=20, default="INV")
    reset_each_year = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "invoice_settings"

    def __str__(self):
        return f"Invoice Settings: {self.business.name}"

class BusinessPreference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="business_preferences")
    business_category = models.CharField(max_length=120, blank=True, default="")
    show_in_online_store = models.BooleanField(default=False)
    enable_gst_billing = models.BooleanField(default=False)
    show_logo_on_invoice = models.BooleanField(default=True)
    branch_billing = models.BooleanField(default=False)
    show_upi_on_invoice = models.BooleanField(default=False)
    print_preview = models.BooleanField(default=True)
    hide_zero_stock_barcodes = models.BooleanField(default=False)
    print_original_duplicate = models.BooleanField(default=True)
    auto_print_after_sale = models.BooleanField(default=False)
    ca_reports_enabled = models.BooleanField(default=False)
    ca_name = models.CharField(max_length=120, blank=True, default="")
    ca_email = models.EmailField(blank=True, default="")
    ca_mobile = models.CharField(max_length=20, blank=True, default="")
    plan_name = models.CharField(max_length=80, blank=True, default="Free Plan")
    plan_valid_till = models.DateField(blank=True, null=True)
    referral_code = models.CharField(max_length=40, blank=True, default="")
    support_email = models.EmailField(blank=True, default="")
    support_phone = models.CharField(max_length=20, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "business_preferences"

    def __str__(self):
        return f"Business Preferences: {self.business.name}"

class ReminderPreference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="reminder_preferences")
    payment_due = models.BooleanField(default=True)
    sale_invoice = models.BooleanField(default=True)
    low_stock = models.BooleanField(default=False)
    customer_occasions = models.BooleanField(default=False)
    daily_summary = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "reminder_preferences"

    def __str__(self):
        return f"Reminder Preferences: {self.business.name}"

class Reminder(models.Model):
    CHANNEL_CHOICES = (
        ("sms", "SMS Alert"),
        ("whatsapp", "WhatsApp Alert"),
        ("email", "Email Notification"),
    )

    STATUS_CHOICES = (
        ("pending", "Pending Scheduled"),
        ("sent", "Successfully Sent"),
        ("failed", "Delivery Failed"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="reminders")
    party = models.ForeignKey(Party, on_delete=models.CASCADE, blank=True, null=True)
    voucher_type = models.CharField(max_length=50, blank=True, null=True)  # sales_invoice, etc.
    voucher_id = models.UUIDField(blank=True, null=True)
    message = models.TextField()
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, default="sms")
    scheduled_at = models.DateTimeField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    attempt_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(blank=True, null=True)
    delivery_provider = models.CharField(max_length=50, blank=True, default="")
    provider_message_id = models.CharField(max_length=120, blank=True, default="")
    provider_response = models.JSONField(default=dict, blank=True)
    delivery_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "reminders"
        indexes = [
            models.Index(fields=["business", "status", "scheduled_at"], name="reminders_business_status_idx"),
        ]


class BusinessNotification(models.Model):
    STATUS_CHOICES = (
        ("unread", "Unread"),
        ("read", "Read"),
        ("dismissed", "Dismissed"),
    )
    PRIORITY_CHOICES = (
        ("high", "High"),
        ("medium", "Medium"),
        ("low", "Low"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="notifications")
    source_type = models.CharField(max_length=50)
    source_id = models.UUIDField(blank=True, null=True)
    title = models.CharField(max_length=160)
    message = models.TextField()
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default="medium")
    target = models.CharField(max_length=80, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="unread")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    read_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "business_notifications"
        constraints = [
            models.UniqueConstraint(
                fields=["business", "source_type", "source_id"],
                name="uniq_notification_source_per_business",
            ),
        ]
        indexes = [
            models.Index(fields=["business", "status", "-created_at"], name="notif_business_status_idx"),
            models.Index(fields=["business", "source_type", "source_id"], name="notif_business_source_idx"),
        ]
