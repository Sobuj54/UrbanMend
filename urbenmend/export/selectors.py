"""Export job read operations."""

from urbenmend.export.models import Export
from urbenmend.identity.models import User


def visible_export(*, actor: User, export_id: str) -> Export | None:
    queryset = Export.objects.filter(pk=export_id)
    if getattr(actor, "role", None) != "admin":
        queryset = queryset.filter(requester=actor)
    return queryset.first()
