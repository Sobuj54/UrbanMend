from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reporting", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="report",
            name="classification_needs_review",
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]
