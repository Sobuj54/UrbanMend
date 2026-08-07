"""
Classification — persistence (T0.10, Q1 resolved).

Category taxonomy + LLM adapter + keyword fallback + caching + cost/rate control.

[doc: Arch §3 (FR-10, FR-12, FR-13, FR-13a, NFR-13); data-model §5]
"""

from __future__ import annotations

from django.db import models


class CategoryStatus(models.TextChoices):
    """Active categories accept new classification; retired ones keep historical refs."""

    ACTIVE = "active", "Active"
    RETIRED = "retired", "Retired"


class Category(models.Model):
    """A node in the controlled classification taxonomy (PRD §6.2, Q1 resolved).

    ⚠️ **Flat, not hierarchical.** Seven peer nodes with no nesting. A nested taxonomy would
    need a `parent` FK; there is none here, and adding one later would be a breaking change to
    BR-26 authority scoping and every category filter.

    **Bilingual labels** (Bangla/English) per NFR-8. The `name_en` is the canonical key — it
    appears in URLs, enums, and LLM prompts. `name_bn` is display-only.

    **Data, not code** (NFR-11/FR-30). The taxonomy is admin-editable config, seeded via
    migration. Do not hard-code a Python enum — that would make adding a node require a code
    deploy rather than a data migration.

    **Lifecycle is `Active → Retired`, never deleted.** Retired nodes keep historical
    references but accept no new classification (data-model §5). Reports/Issues already
    classified under a retired node stay readable; new ones land in `Other`.

    **`Other / Uncategorized` must exist** — PRD §331 edge case: when the LLM returns a
    category outside the allowed set, coerce to `Other`. It is a required sink, not filler.

    [doc: PRD §6.2 "Proposed Category Taxonomy", data-model §5, Arch §2.4/§3]
    """

    # ⚠️ **`slug` is the machine key; the labels are display-only.** API §6.2 emits scope as
    # `"categoryScope": ["roads","water_drainage"]` and §6.10 addresses nodes as
    # `PATCH /categories/{key}` — both are this value, not `name_en`. Authority-scope rows
    # (BR-26), clustering rules, severity keywords and LLM prompts all key on it, so editing an
    # English label can never break them. Immutable in practice: changing a slug silently
    # invalidates every stored reference and every client filter written against it.
    slug = models.SlugField(max_length=50, unique=True)

    # Unique across all statuses — retiring a category does not free its name for reuse, because
    # historical Issues would then point at a semantically different node.
    name_en = models.CharField(max_length=100, unique=True, db_index=True)
    name_bn = models.CharField(max_length=100)  # Bangla display label.

    status = models.CharField(
        max_length=20,
        choices=CategoryStatus.choices,
        default=CategoryStatus.ACTIVE,
        db_index=True,
    )

    # Audit timestamps (no `updated_at` — category edits are rare enough that the admin log
    # suffices; adding one now would set an inconsistent precedent for every other entity).
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "classification_category"
        verbose_name = "Category"
        verbose_name_plural = "Categories"
        ordering = ["name_en"]

    def __str__(self) -> str:
        return self.name_en
