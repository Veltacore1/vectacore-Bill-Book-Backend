# Generated for durable local/provider-stub e-invoice lifecycle.

import uuid
from django.db import migrations, models
import django.db.models.deletion


def backfill_generated_einvoice_status(apps, schema_editor):
    SalesInvoice = apps.get_model("sales", "SalesInvoice")
    SalesInvoice.objects.filter(irn__isnull=False).exclude(irn="").update(
        einvoice_status="generated",
        einvoice_provider="local_stub",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
        ("sales", "0002_alter_salesinvoice_invoice_number_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesinvoice",
            name="einvoice_cancel_reason",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoice",
            name="einvoice_cancelled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoice",
            name="einvoice_last_error",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoice",
            name="einvoice_provider",
            field=models.CharField(default="local_stub", max_length=50),
        ),
        migrations.AddField(
            model_name="salesinvoice",
            name="einvoice_retry_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="salesinvoice",
            name="einvoice_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("generated", "Generated"),
                    ("failed", "Failed"),
                    ("cancelled", "Cancelled"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="EInvoiceLog",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("event", models.CharField(choices=[("generate", "Generate"), ("retry", "Retry"), ("cancel", "Cancel"), ("status_sync", "Status Sync")], max_length=30)),
                ("status", models.CharField(choices=[("success", "Success"), ("failed", "Failed"), ("cancelled", "Cancelled")], max_length=20)),
                ("provider", models.CharField(default="local_stub", max_length=50)),
                ("request_payload", models.JSONField(blank=True, default=dict)),
                ("response_payload", models.JSONField(blank=True, default=dict)),
                ("message", models.TextField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="einvoice_logs", to="accounts.business")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="einvoice_logs", to="accounts.user")),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="einvoice_logs", to="sales.salesinvoice")),
            ],
            options={
                "db_table": "einvoice_logs",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="einvoicelog",
            index=models.Index(fields=["business", "created_at"], name="einvoice_lo_busines_e624ea_idx"),
        ),
        migrations.AddIndex(
            model_name="einvoicelog",
            index=models.Index(fields=["invoice", "created_at"], name="einvoice_lo_invoice_0162c8_idx"),
        ),
        migrations.RunPython(backfill_generated_einvoice_status, migrations.RunPython.noop),
    ]
