from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL), ("contenttypes", "__latest__")]
    operations = [migrations.CreateModel(name="ModerationAction", fields=[
        ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ("action", models.CharField(choices=[("hide", "Hide"), ("remove", "Remove")], max_length=16)),
        ("reason", models.TextField()), ("target_object_id", models.CharField(max_length=128)),
        ("created_at", models.DateTimeField(auto_now_add=True)),
        ("actor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="moderation_actions", to=settings.AUTH_USER_MODEL)),
        ("target_content_type", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="contenttypes.contenttype")),
    ], options={"db_table": "moderation_action", "ordering": ["-created_at", "-id"]})]
