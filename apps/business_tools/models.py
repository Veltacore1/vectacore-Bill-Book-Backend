import uuid
from django.db import models
from apps.accounts.models import Business, User
from apps.items.models import Item
from apps.parties.models import Party


class OnlineOrder(models.Model):
    PAYMENT_STATUS_CHOICES = (
        ("pending", "Pending"),
        ("paid", "Paid"),
        ("cod", "Cash On Delivery"),
        ("refunded", "Refunded"),
    )
    DISPATCH_STATUS_CHOICES = (
        ("new", "New"),
        ("packed", "Packed"),
        ("shipped", "Shipped"),
        ("delivered", "Delivered"),
        ("cancelled", "Cancelled"),
    )
    SOURCE_CHOICES = (
        ("online_store", "Online Store"),
        ("whatsapp", "WhatsApp"),
        ("manual", "Manual Entry"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="online_orders")
    order_number = models.CharField(max_length=60)
    party = models.ForeignKey(Party, on_delete=models.SET_NULL, null=True, blank=True, related_name="online_orders")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="online_orders")
    customer_name = models.CharField(max_length=255)
    customer_mobile = models.CharField(max_length=20, blank=True, null=True)
    delivery_address = models.TextField(blank=True, null=True)
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    unit_price = models.DecimalField(max_digits=15, decimal_places=2)
    taxable_amount = models.DecimalField(max_digits=15, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default="pending")
    dispatch_status = models.CharField(max_length=20, choices=DISPATCH_STATUS_CHOICES, default="new")
    source = models.CharField(max_length=30, choices=SOURCE_CHOICES, default="online_store")
    stock_deducted = models.BooleanField(default=False)
    notes = models.TextField(blank=True, null=True)
    order_date = models.DateField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "online_orders"
        unique_together = ("business", "order_number")
        indexes = [
            models.Index(fields=["business", "dispatch_status", "order_date"], name="online_orde_busines_4ca9bc_idx"),
            models.Index(fields=["business", "payment_status"], name="online_orde_busines_e70e24_idx"),
        ]

    def __str__(self):
        return f"{self.order_number} - {self.customer_name}"


class SMSTemplate(models.Model):
    CATEGORY_CHOICES = (
        ("offer", "Offer"),
        ("new_arrival", "New Arrival"),
        ("payment", "Payment"),
        ("store", "Store Update"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="sms_templates")
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default="offer")
    message = models.TextField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sms_templates"
        unique_together = ("business", "name")
        indexes = [
            models.Index(fields=["business", "category"], name="sms_templa_busines_5f7d1d_idx"),
        ]

    def __str__(self):
        return self.name


class SMSCampaign(models.Model):
    AUDIENCE_CHOICES = (
        ("all_customers", "All Customers"),
        ("manual", "Selected Customers"),
    )
    STATUS_CHOICES = (
        ("draft", "Draft"),
        ("queued", "Queued"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="sms_campaigns")
    campaign_number = models.CharField(max_length=60)
    name = models.CharField(max_length=160)
    template = models.ForeignKey(SMSTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name="campaigns")
    audience = models.CharField(max_length=30, choices=AUDIENCE_CHOICES, default="all_customers")
    message = models.TextField()
    recipient_count = models.PositiveIntegerField(default=0)
    delivered_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    credit_cost = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    queued_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sms_campaigns"
        unique_together = ("business", "campaign_number")
        indexes = [
            models.Index(fields=["business", "status", "created_at"], name="sms_campai_busines_83df21_idx"),
        ]

    def __str__(self):
        return f"{self.campaign_number} - {self.name}"


class SMSRecipient(models.Model):
    STATUS_CHOICES = (
        ("queued", "Queued"),
        ("delivered", "Delivered"),
        ("failed", "Failed"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="sms_recipients")
    campaign = models.ForeignKey(SMSCampaign, on_delete=models.CASCADE, related_name="recipients")
    party = models.ForeignKey(Party, on_delete=models.SET_NULL, null=True, blank=True, related_name="sms_recipients")
    party_name = models.CharField(max_length=255)
    mobile = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="queued")
    delivered_at = models.DateTimeField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sms_recipients"
        unique_together = ("campaign", "mobile")
        indexes = [
            models.Index(fields=["business", "status"], name="sms_recipi_busines_7dfb31_idx"),
        ]


class SMSCreditLedger(models.Model):
    ENTRY_TYPE_CHOICES = (
        ("credit", "Credit"),
        ("debit", "Debit"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="sms_credit_ledger")
    entry_type = models.CharField(max_length=10, choices=ENTRY_TYPE_CHOICES)
    credits = models.PositiveIntegerField()
    reference = models.CharField(max_length=120, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sms_credit_ledger"
        indexes = [
            models.Index(fields=["business", "created_at"], name="sms_credit_busines_a80572_idx"),
        ]
