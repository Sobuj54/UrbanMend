from django.apps import AppConfig


class MediaConfig(AppConfig):
    """Media [doc: Arch §3, FR-7, P3]."""

    default_auto_field = "django.db.models.BigAutoField"
    # Full dotted path: apps are imported as urbenmend.media, not media.
    name = "urbenmend.media"
    label = "media"
    verbose_name = "Media"
