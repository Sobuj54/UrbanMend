from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid

IMMUTABLE_TRIGGER = "audit_event_immutable"
IMMUTABLE_FUNCTION = "audit_event_immutable_fn"


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("contenttypes", "__latest__"),
    ]
    operations = [
        migrations.CreateModel(
            name="AuditEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("action", models.CharField(db_index=True, max_length=96)),
                ("target_object_id", models.CharField(max_length=128)),
                ("before", models.JSONField(blank=True, null=True)),
                ("after", models.JSONField(blank=True, null=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="audit_events", to=settings.AUTH_USER_MODEL)),
                ("target_content_type", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="contenttypes.contenttype")),
            ],
            options={"db_table": "audit_event", "ordering": ["created_at", "id"]},
        ),
        migrations.AddIndex(model_name="auditevent", index=models.Index(fields=["actor", "created_at"], name="audit_actor_created_idx")),
        migrations.AddIndex(model_name="auditevent", index=models.Index(fields=["target_content_type", "target_object_id"], name="audit_target_idx")),
        migrations.RunSQL(
            sql=f"""
                CREATE FUNCTION {IMMUTABLE_FUNCTION}() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'Audit events are immutable';
                END;
                $$;
                CREATE TRIGGER {IMMUTABLE_TRIGGER}
                BEFORE UPDATE OR DELETE ON audit_event
                FOR EACH ROW EXECUTE FUNCTION {IMMUTABLE_FUNCTION}();
            """,
            reverse_sql=f"""
                DROP TRIGGER IF EXISTS {IMMUTABLE_TRIGGER} ON audit_event;
                DROP FUNCTION IF EXISTS {IMMUTABLE_FUNCTION}();
            """,
        ),
    ]
