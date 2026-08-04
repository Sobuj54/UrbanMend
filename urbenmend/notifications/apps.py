from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    """Notifications [doc: Arch §3, FR-27, FR-28, FR-29]."""

    default_auto_field = "django.db.models.BigAutoField"
    # Full dotted path: apps are imported as urbenmend.notifications, not notifications.
    name = "urbenmend.notifications"
    label = "notifications"
    verbose_name = "Notifications"
