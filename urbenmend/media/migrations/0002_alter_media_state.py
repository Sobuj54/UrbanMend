from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("media", "0001_initial")]
    operations = [migrations.AlterField(model_name="media", name="state", field=models.CharField(
        choices=[("uploaded", "Uploaded (sanitized, awaiting derivatives)"),
                 ("processing", "Processing (compression and thumbnail)"),
                 ("ready", "Ready"), ("failed", "Processing failed"),
                 ("hidden", "Hidden by moderation"), ("removed", "Removed by moderation")],
        db_index=True, default="uploaded", max_length=16))]
