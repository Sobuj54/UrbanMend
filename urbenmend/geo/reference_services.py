from django.contrib.gis.geos import Point
from django.db import transaction
from django.http import Http404

from urbenmend.audit.services import record_event
from urbenmend.geo.models import POI, CityBoundary
from urbenmend.identity.models import Role, User
from urbenmend.identity.services import require_role


@transaction.atomic
def create_poi(*, actor: User, name: str, type: str, location: dict, source: str) -> POI:
    require_role(actor, Role.ADMIN)
    poi = POI.objects.create(
        name=name.strip(),
        poi_type=type,
        location=Point(location["lng"], location["lat"], srid=4326),
        source=source.strip(),
    )
    record_event(
        actor=actor,
        action="reference.poi_created",
        target=poi,
        after={"name": poi.name, "type": poi.poi_type, "active": True},
    )
    return poi


@transaction.atomic
def update_poi(*, actor: User, poi_id, **changes) -> POI:
    require_role(actor, Role.ADMIN)
    try:
        poi = POI.objects.select_for_update().get(pk=poi_id)
    except POI.DoesNotExist as exc:
        raise Http404("POI not found.") from exc
    before = {"name": poi.name, "type": poi.poi_type, "source": poi.source, "active": poi.is_active}
    mapping = {"type": "poi_type", "active": "is_active"}
    for key, value in changes.items():
        setattr(poi, mapping.get(key, key), value.strip() if isinstance(value, str) else value)
    poi.save(update_fields=[mapping.get(key, key) for key in changes] + ["updated_at"])
    after = {"name": poi.name, "type": poi.poi_type, "source": poi.source, "active": poi.is_active}
    record_event(
        actor=actor, action="reference.poi_updated", target=poi, before=before, after=after
    )
    return poi


@transaction.atomic
def replace_city_boundary(*, actor: User, name: str, geometry) -> CityBoundary:
    require_role(actor, Role.ADMIN)
    if CityBoundary.objects.filter(name=name.strip()).exists():
        from urbenmend.api.exceptions import Conflict

        raise Conflict("A city boundary with this name already exists.", code="DUPLICATE_NAME")
    active = list(CityBoundary.objects.select_for_update().filter(is_active=True))
    if len(active) != 1:
        from urbenmend.api.exceptions import Conflict

        raise Conflict(
            "Exactly one active city boundary is required before replacement.",
            code="BOUNDARY_STATE_INVALID",
        )
    previous = active[0]
    previous.is_active = False
    previous.save(update_fields=["is_active"])
    boundary = CityBoundary.objects.create(name=name.strip(), area=geometry, is_active=True)
    record_event(
        actor=actor,
        action="reference.city_boundary_replaced",
        target=boundary,
        before={"id": str(previous.pk), "name": previous.name},
        after={"id": str(boundary.pk), "name": boundary.name},
    )
    return boundary
