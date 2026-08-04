from django.apps import AppConfig


class ModerationConfig(AppConfig):
    """Administration & Moderation [doc: Arch §3, FR-30, FR-31]."""

    default_auto_field = "django.db.models.BigAutoField"
    # Full dotted path: apps are imported as urbenmend.moderation, not moderation.
    name = "urbenmend.moderation"
    label = "moderation"
    verbose_name = "Administration & Moderation"
