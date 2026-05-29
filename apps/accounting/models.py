import uuid
from django.db import models
from apps.accounts.models import Business, User


def report_share_token():
    return uuid.uuid4().hex

class BankAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="bank_accounts")
    account_name = models.CharField(max_length=150)  # SBI Primary, HDFC Cash, etc.
    account_number = models.CharField(max_length=50)
    ifsc_code = models.CharField(max_length=20)
    bank_name = models.CharField(max_length=100)
    branch = models.CharField(max_length=100, blank=True, null=True)
    opening_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    current_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bank_accounts"

    def __str__(self):
        return f"{self.account_name} - {self.bank_name}"

class BankTransaction(models.Model):
    TRANSACTION_TYPE_CHOICES = (
        ("deposit", "Deposit / Inward"),
        ("withdrawal", "Withdrawal / Outward"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE)
    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name="transactions")
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPE_CHOICES)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    transaction_date = models.DateField(auto_now_add=True)
    reference_number = models.CharField(max_length=100, blank=True, null=True)  # cheque / NEFT / IMPS ref
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bank_transactions"

class Expense(models.Model):
    PAYMENT_MODE_CHOICES = (
        ("cash", "Cash"),
        ("bank", "Bank Transfer"),
        ("upi", "UPI"),
        ("cheque", "Cheque"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="expenses")
    expense_category = models.CharField(max_length=100)  # Rent, Fuel, Tea/Snacks, Packing Saree boxes, etc.
    expense_number = models.CharField(max_length=50)  # EXP-0001
    expense_date = models.DateField(auto_now_add=True)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    payment_mode = models.CharField(max_length=20, choices=PAYMENT_MODE_CHOICES, default="cash")
    reference_number = models.CharField(max_length=100, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "expenses"

    def __str__(self):
        return f"{self.expense_category} - {self.total_amount}"

class AutomatedBill(models.Model):
    FREQUENCY_CHOICES = (
        ("monthly", "Monthly"),
        ("quarterly", "Quarterly"),
        ("yearly", "Yearly"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE)
    bill_name = models.CharField(max_length=150)  # Saree Loom rent, Electricity, Internet
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    frequency = models.CharField(max_length=20, choices=FREQUENCY_CHOICES, default="monthly")
    next_due_date = models.DateField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "automated_bills"


class ReportShare(models.Model):
    STATUS_CHOICES = (
        ("prepared", "Prepared"),
        ("sent", "Sent"),
        ("failed", "Failed"),
        ("revoked", "Revoked"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="report_shares")
    report_id = models.CharField(max_length=80)
    report_name = models.CharField(max_length=180)
    recipient = models.CharField(max_length=180)
    date_range = models.CharField(max_length=80, blank=True, default="")
    filters = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="prepared")
    share_token = models.CharField(max_length=64, unique=True, default=report_share_token)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "report_shares"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["business", "report_id", "created_at"]),
            models.Index(fields=["business", "recipient", "created_at"]),
        ]

    def __str__(self):
        return f"{self.report_name} -> {self.recipient}"
