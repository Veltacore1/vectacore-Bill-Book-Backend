# Generated for CSM SILKS online orders on 2026-05-23

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("accounts", "0001_initial"),
        ("items", "0001_initial"),
        ("parties", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="OnlineOrder",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("order_number", models.CharField(max_length=60)),
                ("customer_name", models.CharField(max_length=255)),
                ("customer_mobile", models.CharField(blank=True, max_length=20, null=True)),
                ("delivery_address", models.TextField(blank=True, null=True)),
                ("quantity", models.DecimalField(decimal_places=3, max_digits=15)),
                ("unit_price", models.DecimalField(decimal_places=2, max_digits=15)),
                ("taxable_amount", models.DecimalField(decimal_places=2, max_digits=15)),
                ("tax_amount", models.DecimalField(decimal_places=2, default=0.0, max_digits=15)),
                ("total_amount", models.DecimalField(decimal_places=2, max_digits=15)),
                ("payment_status", models.CharField(choices=[("pending", "Pending"), ("paid", "Paid"), ("cod", "Cash On Delivery"), ("refunded", "Refunded")], default="pending", max_length=20)),
                ("dispatch_status", models.CharField(choices=[("new", "New"), ("packed", "Packed"), ("shipped", "Shipped"), ("delivered", "Delivered"), ("cancelled", "Cancelled")], default="new", max_length=20)),
                ("source", models.CharField(choices=[("online_store", "Online Store"), ("whatsapp", "WhatsApp"), ("manual", "Manual Entry")], default="online_store", max_length=30)),
                ("stock_deducted", models.BooleanField(default=False)),
                ("notes", models.TextField(blank=True, null=True)),
                ("order_date", models.DateField(auto_now_add=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="online_orders", to="accounts.business")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="accounts.user")),
                ("item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="online_orders", to="items.item")),
                ("party", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="online_orders", to="parties.party")),
            ],
            options={
                "db_table": "online_orders",
                "unique_together": {("business", "order_number")},
            },
        ),
        migrations.AddIndex(
            model_name="onlineorder",
            index=models.Index(fields=["business", "dispatch_status", "order_date"], name="online_orde_busines_4ca9bc_idx"),
        ),
        migrations.AddIndex(
            model_name="onlineorder",
            index=models.Index(fields=["business", "payment_status"], name="online_orde_busines_e70e24_idx"),
        ),
    ]
