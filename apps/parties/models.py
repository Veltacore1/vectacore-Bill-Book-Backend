import uuid
from django.db import models
from apps.accounts.models import Business

class PartyCategory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="party_categories")
    name = models.CharField(max_length=100)

    class Meta:
        db_table = "party_categories"
        unique_together = ("business", "name")

    def __str__(self):
        return self.name

class Party(models.Model):
    PARTY_TYPE_CHOICES = (
        ("customer", "Customer"),
        ("supplier", "Supplier"),
        ("both", "Both"),
    )
    
    BALANCE_TYPE_CHOICES = (
        ("debit", "Debit (To Receive)"),
        ("credit", "Credit (To Pay)"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="parties")
    name = models.CharField(max_length=255)
    mobile = models.CharField(max_length=20, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    gstin = models.CharField(max_length=20, blank=True, null=True)
    pan = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    pincode = models.CharField(max_length=10, blank=True, null=True)
    party_type = models.CharField(max_length=20, choices=PARTY_TYPE_CHOICES, default="customer")
    category = models.ForeignKey(PartyCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="parties")
    opening_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    opening_balance_type = models.CharField(max_length=10, choices=BALANCE_TYPE_CHOICES, default="debit")
    credit_limit = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True)
    credit_days = models.IntegerField(blank=True, null=True)
    shared_ledger_token = models.CharField(max_length=100, unique=True, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "parties"

    def __str__(self):
        return f"{self.name} ({self.party_type})"
