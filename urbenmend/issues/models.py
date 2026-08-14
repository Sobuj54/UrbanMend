"""Issues, clustering and confirmation persistence (T4.1-T4.7).

An Issue is the authority-facing unit of work: one real-world problem represented by one or more
citizen Reports. Report processing state stays on Report; severity, municipal workflow and
assignment live here and nowhere else.

T4.1 establishes the aggregate and its relationships. T4.2-T4.7 add the spatial index, clustering
rules, concurrency-safe attachment, member-derived severity and citizen confirmations; lifecycle
transitions remain T5 work.

[doc: data-model section 3; Arch sections 4.2-4.4; plan T4.1]
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.contrib.gis.db import models as gis_models
from django.contrib.postgres.indexes import GistIndex
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from urbenmend.identity.models import User, UserStatus
from urbenmend.reporting.models import SeveritySignal


class ClusteringRuleStatus(models.TextChoices):
    """Lifecycle for tunable per-category clustering configuration."""

    ACTIVE = "active", _("Active")
    RETIRED = "retired", _("Retired")


class ClusteringRule(models.Model):
    """Per-category proximity and age limits used by future clustering decisions (T4.3)."""

    category = models.ForeignKey(
        "classification.Category",
        on_delete=models.PROTECT,
        related_name="clustering_rules",
    )
    radius_m = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text=_("Maximum distance in metres for joining an existing Issue."),
    )
    time_window_hours = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text=_("Maximum Issue age in hours for accepting another Report."),
    )
    status = models.CharField(
        max_length=16,
        choices=ClusteringRuleStatus.choices,
        default=ClusteringRuleStatus.ACTIVE,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "issues_clustering_rule"
        verbose_name = _("clustering rule")
        verbose_name_plural = _("clustering rules")
        ordering = ["category_id", "-created_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(radius_m__gt=0),
                name="issues_rule_radius_positive",
            ),
            models.CheckConstraint(
                condition=Q(time_window_hours__gt=0),
                name="issues_rule_window_positive",
            ),
            models.UniqueConstraint(
                fields=["category"],
                condition=Q(status=ClusteringRuleStatus.ACTIVE),
                name="issues_rule_one_active_category",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.category}: {self.radius_m} m / {self.time_window_hours} h ({self.status})"


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
            # T4.2: serves `ST_DWithin`, KNN `<->` and later bbox/map queries. The field disables
            # its implicit index so this stable, reviewable name is the only spatial index.
            GistIndex(
                fields=["representative_location"],
                name="issues_issue_location_gist",
            ),
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

    @property
    def corroboration_count(self) -> int:
        """Distinct active people who reported or confirmed this Issue (FR-16, BR-22)."""
        # Verification/age weighting is deliberately absent: the trust inputs exist, but the
        # product defines no threshold or weight. Inventing one would turn this display-only count
        # into the undeclared numeric scoring system FR-21 removed.
        active_statuses = (UserStatus.REGISTERED, UserStatus.VERIFIED, UserStatus.ACTIVE)
        return (
            User.objects.filter(
                Q(reports__issue=self) | Q(confirmations__issue=self),
                status__in=active_statuses,
            )
            .distinct()
            .count()
        )


class Confirmation(models.Model):
    """One revocable citizen assertion that an Issue affects them too (FR-16, BR-23)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    issue = models.ForeignKey(
        Issue,
        on_delete=models.PROTECT,
        related_name="confirmations",
    )
    citizen = models.ForeignKey(
        "identity.User",
        on_delete=models.PROTECT,
        related_name="confirmations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "issues_confirmation"
        verbose_name = _("confirmation")
        verbose_name_plural = _("confirmations")
        ordering = ["created_at", "id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["issue", "citizen"],
                name="issues_confirmation_one_per_citizen",
            )
        ]

    def __str__(self) -> str:
        return f"{self.citizen} confirmed {self.issue}"
