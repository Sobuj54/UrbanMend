"""Celery export generation tasks (T9.1)."""

import csv
import io
import json
from typing import Any

from celery import shared_task
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone

from urbenmend.export.models import Export, ExportState

EXPORT_TASK = "export.generate"


@shared_task(name=EXPORT_TASK)  # type: ignore[untyped-decorator]
def generate_export(export_id: str, **_options: Any) -> None:
    from urbenmend.issues.selectors import list_issues
    from urbenmend.reporting.selectors import list_reports

    export = Export.objects.select_related("requester").filter(pk=export_id).first()
    if export is None or export.state != ExportState.PROCESSING:
        return
    try:
        if export.resource == "issues":
            rows = list(
                list_issues(
                    actor=export.requester,
                    category_slugs=_category_filter(export.filters),
                )[:10000]
            )
            if export.format == "geojson":
                payload = {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "id": str(issue.pk),
                            "geometry": {
                                "type": "Point",
                                "coordinates": [
                                    issue.representative_location.x,
                                    issue.representative_location.y,
                                ],
                            },
                            "properties": {
                                "category": issue.primary_category.slug,
                                "severity": issue.current_severity,
                                "status": issue.status,
                            },
                        }
                        for issue in rows
                    ],
                }
                content = json.dumps(payload).encode()
                extension = "geojson"
            else:
                stream = io.StringIO()
                writer = csv.writer(stream)
                writer.writerow(["id", "category", "severity", "status", "longitude", "latitude"])
                for issue in rows:
                    writer.writerow(
                        [
                            issue.pk,
                            issue.primary_category.slug,
                            issue.current_severity,
                            issue.status,
                            issue.representative_location.x,
                            issue.representative_location.y,
                        ]
                    )
                content, extension = stream.getvalue().encode(), "csv"
        else:
            reports = list(
                list_reports(
                    actor=export.requester,
                    category_slugs=_category_filter(export.filters),
                )[:10000]
            )
            if export.format == "geojson":
                payload = {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "id": str(report.pk),
                            "geometry": {
                                "type": "Point",
                                "coordinates": [report.location.x, report.location.y],
                            },
                            "properties": {
                                "category": report.category.slug if report.category else None,
                                "severity": report.severity_signal,
                                "status": report.status,
                            },
                        }
                        for report in reports
                    ],
                }
                content = json.dumps(payload).encode()
                extension = "geojson"
            else:
                stream = io.StringIO()
                writer = csv.writer(stream)
                writer.writerow(["id", "category", "severity", "status", "longitude", "latitude"])
                for report in reports:
                    writer.writerow(
                        [
                            report.pk,
                            report.category.slug if report.category else "",
                            report.severity_signal or "",
                            report.status,
                            report.location.x,
                            report.location.y,
                        ]
                    )
                content, extension = stream.getvalue().encode(), "csv"
        key = f"exports/{export.pk}.{extension}"
        default_storage.save(key, ContentFile(content))
        export.object_key = key
        export.state = ExportState.READY
        export.completed_at = timezone.now()
        export.save(update_fields=["object_key", "state", "completed_at"])
    except Exception as exc:  # noqa: BLE001
        export.state = ExportState.FAILED
        export.failure_reason = f"{type(exc).__name__}: {exc}"
        export.completed_at = timezone.now()
        export.save(update_fields=["state", "failure_reason", "completed_at"])


def _category_filter(filters: dict[str, object]) -> tuple[str, ...]:
    value = filters.get("category")
    return (value,) if isinstance(value, str) and value else ()
