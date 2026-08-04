from django.apps import AppConfig


class ExportConfig(AppConfig):
    """Export [doc: Arch §3, NFR-12]."""

    default_auto_field = "django.db.models.BigAutoField"
    # Full dotted path: apps are imported as urbenmend.export, not export.
    name = "urbenmend.export"
    label = "export"
    verbose_name = "Export"
