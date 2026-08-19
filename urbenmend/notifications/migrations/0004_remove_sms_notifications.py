from django.db import migrations, models


def remove_sms_rows(apps, schema_editor):
    Notification = apps.get_model("notifications", "Notification")
    Notification.objects.filter(channel="sms").delete()


class Migration(migrations.Migration):
    dependencies = [("notifications", "0003_notificationpreference")]
    operations = [
        migrations.RunPython(remove_sms_rows, migrations.RunPython.noop),
        migrations.RemoveField(model_name="notificationpreference", name="sms"),
        migrations.AlterField(
            model_name="notification",
            name="channel",
            field=models.CharField(
                choices=[("in_app", "In-app"), ("email", "Email")], max_length=16
            ),
        ),
    ]
