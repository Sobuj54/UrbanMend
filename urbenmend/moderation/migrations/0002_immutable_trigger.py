from django.db import migrations

IMMUTABLE_TRIGGER = "moderation_action_immutable"
IMMUTABLE_FUNCTION = "moderation_action_immutable_fn"

class Migration(migrations.Migration):
    dependencies = [("moderation", "0001_initial")]
    operations = [migrations.RunSQL(sql=f"""
        CREATE FUNCTION {IMMUTABLE_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Moderation actions are immutable'; END; $$;
        CREATE TRIGGER {IMMUTABLE_TRIGGER} BEFORE UPDATE OR DELETE ON moderation_action
        FOR EACH ROW EXECUTE FUNCTION {IMMUTABLE_FUNCTION}();
    """, reverse_sql=f"""DROP TRIGGER IF EXISTS {IMMUTABLE_TRIGGER} ON moderation_action;
        DROP FUNCTION IF EXISTS {IMMUTABLE_FUNCTION}();""")]
