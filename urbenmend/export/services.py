"""Export job write operations."""

from django.db import transaction

from urbenmend.export.models import Export
from urbenmend.export.tasks import generate_export
from urbenmend.identity.models import Role, User


def create_export(
    *, actor: User, resource: str, file_format: str, filters: dict[str, object]
) -> Export:
    if actor.role not in {Role.AUTHORITY, Role.ADMIN}:
        raise PermissionError("Authority or Admin role required.")
    with transaction.atomic():
        export = Export.objects.create(
            requester=actor, resource=resource, format=file_format, filters=filters
        )
        transaction.on_commit(lambda: generate_export.delay(str(export.pk)))
    return export
