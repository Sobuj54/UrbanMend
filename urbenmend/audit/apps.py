from django.apps import AppConfig


class AuditConfig(AppConfig):
    """Audit & Integrity [doc: Arch §3, FR-32]."""

    default_auto_field = "django.db.models.BigAutoField"
    # Full dotted path: apps are imported as urbenmend.audit, not audit.
    name = "urbenmend.audit"
    label = "audit"
    verbose_name = "Audit & Integrity"
