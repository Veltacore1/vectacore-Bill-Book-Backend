from decimal import Decimal

import django.db.models.deletion
import uuid
from django.db import migrations, models
from django.db.models import Sum


def seed_item_godown_stock(apps, schema_editor):
    Business = apps.get_model("accounts", "Business")
    Godown = apps.get_model("items", "Godown")
    Item = apps.get_model("items", "Item")
    ItemGodownStock = apps.get_model("items", "ItemGodownStock")
    StockMovement = apps.get_model("items", "StockMovement")

    for business in Business.objects.all():
        default_godown = Godown.objects.filter(business=business, is_default=True).order_by("name").first()
        if not default_godown:
            default_godown = Godown.objects.filter(business=business).order_by("name").first()
        if not default_godown:
            default_godown = Godown.objects.create(business=business, name="Main Godown", is_default=True)
        elif not default_godown.is_default:
            default_godown.is_default = True
            default_godown.save(update_fields=["is_default"])

        for item in Item.objects.filter(business=business):
            godown = item.godown or default_godown
            if not item.godown_id:
                item.godown = godown
                item.save(update_fields=["godown"])

            stock, created = ItemGodownStock.objects.get_or_create(
                business=business,
                item=item,
                godown=godown,
                defaults={
                    "opening_stock": item.opening_stock or Decimal("0.000"),
                    "current_stock": item.current_stock or Decimal("0.000"),
                },
            )
            if not created:
                stock.opening_stock = item.opening_stock or Decimal("0.000")
                stock.current_stock = item.current_stock or Decimal("0.000")
                stock.save(update_fields=["opening_stock", "current_stock", "updated_at"])

        StockMovement.objects.filter(business=business, godown__isnull=True).update(godown=default_godown)

        for item in Item.objects.filter(business=business):
            total = ItemGodownStock.objects.filter(business=business, item=item).aggregate(
                total=Sum("current_stock")
            )["total"] or Decimal("0.000")
            if item.current_stock != total:
                item.current_stock = total
                item.save(update_fields=["current_stock"])


def unseed_item_godown_stock(apps, schema_editor):
    ItemGodownStock = apps.get_model("items", "ItemGodownStock")
    ItemGodownStock.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("items", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ItemGodownStock",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("opening_stock", models.DecimalField(decimal_places=3, default=0.0, max_digits=15)),
                ("current_stock", models.DecimalField(decimal_places=3, default=0.0, max_digits=15)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="item_godown_stocks", to="accounts.business")),
                ("godown", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="item_stocks", to="items.godown")),
                ("item", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="godown_stocks", to="items.item")),
            ],
            options={
                "db_table": "item_godown_stocks",
                "unique_together": {("business", "item", "godown")},
            },
        ),
        migrations.AddIndex(
            model_name="itemgodownstock",
            index=models.Index(fields=["business", "godown"], name="item_godown_busines_9adb5b_idx"),
        ),
        migrations.AddIndex(
            model_name="itemgodownstock",
            index=models.Index(fields=["business", "item"], name="item_godown_busines_ef24a1_idx"),
        ),
        migrations.RunPython(seed_item_godown_stock, unseed_item_godown_stock),
    ]
