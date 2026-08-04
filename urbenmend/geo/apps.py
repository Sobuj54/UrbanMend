from django.apps import AppConfig


class GeoConfig(AppConfig):
    """Geospatial [doc: Arch §3, FR-6, FR-16, FR-17, FR-23, NFR-1]."""

    default_auto_field = "django.db.models.BigAutoField"
    # Full dotted path: apps are imported as urbenmend.geo, not geo.
    name = "urbenmend.geo"
    label = "geo"
    verbose_name = "Geospatial"
