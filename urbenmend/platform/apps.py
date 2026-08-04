from django.apps import AppConfig


class PlatformConfig(AppConfig):
    """Platform (cross-cutting) [doc: Arch §3, NFR-5, NFR-9, NFR-13]."""

    default_auto_field = "django.db.models.BigAutoField"
    # Full dotted path: apps are imported as urbenmend.platform, not platform.
    name = "urbenmend.platform"
    label = "platform"
    verbose_name = "Platform (cross-cutting)"
