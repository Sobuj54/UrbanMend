from django.apps import AppConfig


class ClassificationConfig(AppConfig):
    """Classification [doc: Arch §3, FR-10, FR-12, FR-13, FR-13a, NFR-13]."""

    default_auto_field = "django.db.models.BigAutoField"
    # Full dotted path: apps are imported as urbenmend.classification, not classification.
    name = "urbenmend.classification"
    label = "classification"
    verbose_name = "Classification"
