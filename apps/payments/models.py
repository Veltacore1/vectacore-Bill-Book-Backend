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
