from django.apps import AppConfig


class IdentityConfig(AppConfig):
    """Identity & Access [doc: Arch §3, FR-1, FR-2, FR-3, FR-4]."""

    default_auto_field = "django.db.models.BigAutoField"
    # Full dotted path: apps are imported as urbenmend.identity, not identity.
    name = "urbenmend.identity"
    label = "identity"
    verbose_name = "Identity & Access"
