import uuid
from django.db import models
from apps.accounts.models import Business

class Staff(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="staff_members")
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    designation = models.CharField(max_length=150, blank=True, null=True)  # Saree Weaver, Accountant, Sales Executive, Loom Operator
    monthly_salary = models.DecimalField(max_digits=15, decimal_places=2)
    joining_date = models.DateField(auto_now_add=True)
    bank_details = models.JSONField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "staff"

    def __str__(self):
        return self.name

class Attendance(models.Model):
    STATUS_CHOICES = (
        ("present", "Present"),
        ("absent", "Absent"),
        ("half_day", "Half Day"),
        ("holiday", "Paid Holiday"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE)
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="attendance_records")
    date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="present")
    check_in_time = models.TimeField(blank=True, null=True)
    check_out_time = models.TimeField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "attendance"
        unique_together = ("staff", "date")

class Payroll(models.Model):
    STATUS_CHOICES = (
        ("unpaid", "Unpaid"),
        ("paid", "Paid"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE)
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="payroll_records")
    month = models.IntegerField()  # 1-12
    year = models.IntegerField()
    basic_salary = models.DecimalField(max_digits=15, decimal_places=2)
    deductions = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    allowances = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    net_salary = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="unpaid")
    payment_date = models.DateField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payroll"
        unique_together = ("staff", "month", "year")
