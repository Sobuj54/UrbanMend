from django.apps import AppConfig


class ReportingConfig(AppConfig):
    """Reporting [doc: Arch §3, FR-5, FR-8, FR-9, FR-11]."""

    default_auto_field = "django.db.models.BigAutoField"
    # Full dotted path: apps are imported as urbenmend.reporting, not reporting.
    name = "urbenmend.reporting"
    label = "reporting"
    verbose_name = "Reporting"
