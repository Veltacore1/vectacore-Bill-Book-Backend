from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0003_reportshare"),
    ]

    operations = [
        migrations.AlterField(
            model_name="reportshare",
            name="status",
            field=models.CharField(
                choices=[
                    ("prepared", "Prepared"),
                    ("sent", "Sent"),
                    ("failed", "Failed"),
                    ("revoked", "Revoked"),
                ],
                default="prepared",
                max_length=20,
            ),
        ),
    ]
