"""Issues & Clustering persistence (T4.1).

An Issue is the authority-facing unit of work: one real-world problem represented by one or more
citizen Reports. Report processing state stays on Report; severity, municipal workflow and
assignment live here and nowhere else.

T4.1 establishes the aggregate and its relationships only. Spatial matching, clustering locks,
severity recomputation and lifecycle transitions belong to T4.2-T5 and are deliberately absent.

[doc: data-model section 3; Arch sections 4.2-4.4; plan T4.1]
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.contrib.gis.db import models as gis_models
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from urbenmend.reporting.models import SeveritySignal


class IssueStatus(models.TextChoices):
    """The authoritative municipal workflow for one real-world problem."""

    SUBMITTED = "submitted", _("Submitted")
    TRIAGED = "triaged", _("Triaged")
    ACKNOWLEDGED = "acknowledged", _("Acknowledged")
    IN_PROGRESS = "in_progress", _("In progress")
    RESOLVED = "resolved", _("Resolved")
    CLOSED = "closed", _("Closed")
    REJECTED = "rejected", _("Rejected")
    DUPLICATE = "duplicate", _("Duplicate")
    INSUFFICIENT_INFO = "insufficient_info", _("Insufficient information")
    HIDDEN = "hidden", _("Hidden by moderation")
    REMOVED = "removed", _("Removed by moderation")


class Issue(models.Model):
    """A cluster of Reports describing one real-world civic problem (FR-18)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    primary_category = models.ForeignKey(
        "classification.Category",
        on_delete=models.PROTECT,
        related_name="issues",
    )
    representative_location = gis_models.PointField(
        geography=True,
        srid=4326,
        spatial_index=False,
    )
    computed_severity = models.CharField(
        max_length=16,
        choices=SeveritySignal.choices,
        db_index=True,
    )
    computed_severity_rationale = models.TextField()
    # NULL means "no authority override". An empty string would be an undeclared fifth band and
    # could not distinguish an absent override from malformed data.
    overridden_severity = models.CharField(  # noqa: DJ001
        max_length=16,
        choices=SeveritySignal.choices,
        null=True,
        blank=True,
    )
    severity_override_reason = models.TextField(blank=True)
    severity_overridden_by = models.ForeignKey(
        "identity.User",
        on_delete=models.PROTECT,
        related_name="severity_overridden_issues",
        null=True,
        blank=True,
    )
    severity_overridden_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=24,
        choices=IssueStatus.choices,
        default=IssueStatus.SUBMITTED,
        db_index=True,
    )
    assignee = models.ForeignKey(
        "identity.User",
        on_delete=models.PROTECT,
        related_name="assigned_issues",
        null=True,
        blank=True,
    )
    duplicate_of = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="duplicates",
        null=True,
        blank=True,
    )
    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "issues_issue"
        verbose_name = _("issue")
        verbose_name_plural = _("issues")
        ordering = ["opened_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["primary_category", "status", "opened_at"],
                name="issues_issue_queue_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Issue {self.pk}"

    @property
    def current_severity(self) -> str:
        """The displayed band, never a numeric priority score."""
        return self.overridden_severity or self.computed_severity

    @property
    def report_count(self) -> int:
        """Member count is derived from Reports, never a mutable counter column."""
        return self.reports.count()
