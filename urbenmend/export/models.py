"""Durable asynchronous export jobs (API section 6.15)."""

import uuid

from django.db import models


class ExportState(models.TextChoices):
    PROCESSING = "processing", "Processing"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class Export(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    requester = models.ForeignKey("identity.User", on_delete=models.PROTECT, related_name="exports")
    resource = models.CharField(
        max_length=16, choices=[("issues", "Issues"), ("reports", "Reports")]
    )
    format = models.CharField(max_length=16, choices=[("csv", "CSV"), ("geojson", "GeoJSON")])
    filters = models.JSONField(default=dict)
    state = models.CharField(
        max_length=16, choices=ExportState.choices, default=ExportState.PROCESSING
    )
    object_key = models.CharField(max_length=255, blank=True)
    failure_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"Export {self.pk} ({self.state})"
