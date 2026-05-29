# Generated for MyBillBook-style item metadata and party-wise prices.

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("items", "0003_barcodelabel"),
        ("parties", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="item",
            name="bill_no",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="item",
            name="cin_date",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="item",
            name="color",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="item",
            name="grn_date",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="item",
            name="secondary_unit",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="item",
            name="serialisation_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="item",
            name="show_online_store",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="ItemPartyPrice",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("sales_price", models.DecimalField(decimal_places=2, max_digits=15)),
                ("tax_inclusive", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="item_party_prices", to="accounts.business")),
                ("item", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="party_prices", to="items.item")),
                ("party", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="item_prices", to="parties.party")),
            ],
            options={
                "db_table": "item_party_prices",
                "unique_together": {("business", "item", "party")},
                "indexes": [
                    models.Index(fields=["business", "item"], name="item_party__busines_b5377b_idx"),
                    models.Index(fields=["business", "party"], name="item_party__busines_39810f_idx"),
                ],
            },
        ),
    ]
