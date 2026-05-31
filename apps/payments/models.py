import uuid
from django.db import models
from apps.accounts.models import Business, User
from apps.parties.models import Party
from apps.sales.models import SalesInvoice
from apps.purchases.models import PurchaseInvoice

class PaymentIn(models.Model):
    STATUS_CHOICES = (
        ("active", "Active"),
        ("void", "Void"),
    )

    PAYMENT_MODE_CHOICES = (
        ("cash", "Cash"),
        ("bank", "Bank Account Transfer"),
        ("upi", "UPI (GPay/PhonePe/etc.)"),
        ("cheque", "Cheque"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="payments_in")
    payment_number = models.CharField(max_length=50)  # PMTIN-0001
    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="payments_in")
    payment_date = models.DateField(auto_now_add=True)
    amount_received = models.DecimalField(max_digits=15, decimal_places=2)
    payment_mode = models.CharField(max_length=20, choices=PAYMENT_MODE_CHOICES, default="cash")
    reference_number = models.CharField(max_length=100, blank=True, null=True)  # transaction ref / cheque no
    
    # Financial linkages
    bank_account_id = models.UUIDField(blank=True, null=True)  # link to accounting.BankAccount
    is_advance = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    cancellation_reason = models.TextField(blank=True, null=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)
    cancelled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="voided_payments_in")
    notes = models.TextField(blank=True, null=True)
    
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payments_in"
        constraints = [
            models.UniqueConstraint(fields=["business", "payment_number"], name="uniq_payment_in_number_per_business"),
        ]

    def __str__(self):
        return self.payment_number

class PaymentInSettlement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment_in = models.ForeignKey(PaymentIn, on_delete=models.CASCADE, related_name="settlements")
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name="settlements")
    settled_amount = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        db_table = "payment_in_settlements"

class PaymentOut(models.Model):
    STATUS_CHOICES = (
        ("active", "Active"),
        ("void", "Void"),
    )

    PAYMENT_MODE_CHOICES = (
        ("cash", "Cash"),
        ("bank", "Bank Account Transfer"),
        ("upi", "UPI"),
        ("cheque", "Cheque"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="payments_out")
    payment_number = models.CharField(max_length=50)  # PMTOUT-0001
    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="payments_out")
    payment_date = models.DateField(auto_now_add=True)
    amount_paid = models.DecimalField(max_digits=15, decimal_places=2)
    payment_mode = models.CharField(max_length=20, choices=PAYMENT_MODE_CHOICES, default="cash")
    reference_number = models.CharField(max_length=100, blank=True, null=True)
    
    bank_account_id = models.UUIDField(blank=True, null=True)
    is_advance = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    cancellation_reason = models.TextField(blank=True, null=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)
    cancelled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="voided_payments_out")
    notes = models.TextField(blank=True, null=True)
    
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payments_out"
        constraints = [
            models.UniqueConstraint(fields=["business", "payment_number"], name="uniq_payment_out_number_per_business"),
        ]

    def __str__(self):
        return self.payment_number

class PaymentOutSettlement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment_out = models.ForeignKey(PaymentOut, on_delete=models.CASCADE, related_name="settlements")
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name="settlements")
    settled_amount = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        db_table = "payment_out_settlements"


class PaymentGatewayOrder(models.Model):
    STATUS_CHOICES = (
        ("created", "Created"),
        ("attempted", "Attempted"),
        ("paid", "Paid"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="payment_gateway_orders")
    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="payment_gateway_orders")
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.SET_NULL, null=True, blank=True, related_name="payment_gateway_orders")
    payment_in = models.OneToOneField(PaymentIn, on_delete=models.SET_NULL, null=True, blank=True, related_name="gateway_order")
    provider = models.CharField(max_length=50, default="razorpay")
    provider_order_id = models.CharField(max_length=100, unique=True)
    provider_payment_id = models.CharField(max_length=100, blank=True, null=True)
    provider_status = models.CharField(max_length=50, blank=True, default="")
    receipt = models.CharField(max_length=40)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    amount_subunits = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="created")
    signature_verified = models.BooleanField(default=False)
    provider_payload = models.JSONField(default=dict, blank=True)
    notes = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_payment_gateway_orders")
    paid_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payment_gateway_orders"
        constraints = [
            models.UniqueConstraint(fields=["business", "receipt"], name="uniq_gateway_order_receipt_per_business"),
        ]
        indexes = [
            models.Index(fields=["business", "status"], name="pg_order_biz_status_idx"),
            models.Index(fields=["provider", "provider_order_id"], name="pg_order_provider_idx"),
        ]

    def __str__(self):
        return f"{self.provider}:{self.provider_order_id}"


class PaymentGatewayEvent(models.Model):
    STATUS_CHOICES = (
        ("processed", "Processed"),
        ("duplicate", "Duplicate"),
        ("failed", "Failed"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.SET_NULL, null=True, blank=True, related_name="payment_gateway_events")
    order = models.ForeignKey(PaymentGatewayOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="gateway_events")
    provider = models.CharField(max_length=50, default="razorpay")
    event_id = models.CharField(max_length=120)
    event_type = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="processed")
    signature_valid = models.BooleanField(default=False)
    message = models.TextField(blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    processed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payment_gateway_events"
        constraints = [
            models.UniqueConstraint(fields=["provider", "event_id"], name="uniq_gateway_event_per_provider"),
        ]
        indexes = [
            models.Index(fields=["business", "created_at"], name="pg_event_biz_created_idx"),
            models.Index(fields=["provider", "event_type"], name="pg_event_provider_type_idx"),
        ]

    def __str__(self):
        return f"{self.provider}:{self.event_id}"
