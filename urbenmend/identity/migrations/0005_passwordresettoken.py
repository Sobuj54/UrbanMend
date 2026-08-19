from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid
import django.utils.timezone

class Migration(migrations.Migration):
    dependencies = [("identity", "0004_authority_two_factor")]
    operations = [migrations.CreateModel(name="PasswordResetToken", fields=[
        ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ("token_hash", models.CharField(max_length=128)),
        ("expires_at", models.DateTimeField()),
        ("consumed_at", models.DateTimeField(blank=True, null=True)),
        ("attempts", models.PositiveSmallIntegerField(default=0)),
        ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
        ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reset_tokens", to=settings.AUTH_USER_MODEL)),
    ], options={"db_table": "identity_password_reset_token"}),
    migrations.AddIndex(model_name="passwordresettoken", index=models.Index(fields=["user", "-created_at"], name="identity_reset_lookup_idx"))]
