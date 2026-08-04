from django.apps import AppConfig


class IssuesConfig(AppConfig):
    """Issues & Clustering [doc: Arch §3, FR-14, FR-15, FR-18, FR-19, FR-20, FR-24, FR-25]."""

    default_auto_field = "django.db.models.BigAutoField"
    # Full dotted path: apps are imported as urbenmend.issues, not issues.
    name = "urbenmend.issues"
    label = "issues"
    verbose_name = "Issues & Clustering"
