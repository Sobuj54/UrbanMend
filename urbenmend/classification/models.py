"""
Classification — persistence (T0.10 `Category`, ❓Q1 resolved; T3.3 `SeverityKeyword`).

Category taxonomy + LLM adapter + keyword fallback + caching + cost/rate control.

The two tables here are both *reference data* (NFR-11): the taxonomy every classification must
choose from, and the bilingual indicator phrases the deterministic fallback matches (FR-13a).
Neither holds report data. The classifiers themselves live outside this module and hold no Django
imports at all — `contracts.py` (the ABC), `keywords.py` (the fallback engine), `llm.py` (the
hosted adapter) — with `selectors.py` reading these tables into their plain value objects.

[doc: Arch §3, §6 (FR-10, FR-12, FR-13, FR-13a, NFR-13); data-model §5, §14]
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.core.validators import MinLengthValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from urbenmend.classification.keywords import normalize_term

# ⚠️ **Imported, not redeclared, and the direction cannot reverse.** `SeverityKeyword.severity` and
# `Report.severity_signal` must hold the same four bands: a keyword whose band cannot be written to
# a Report is a rule the fallback can match and then fail to persist. Importing means a fifth band
# added to `SeveritySignal` also alters this column's `choices`, which `makemigrations --check`
# catches as drift — a local redeclaration would let this table silently lag by a band.
#
# `reporting.models` reaches `classification` only through the string FK `"classification.Category"`,
# so this import closes no loop. It must stay that way: a Python-level import from
# `reporting.models` back into this module is the cycle, and Django surfaces it at startup as a
# partially-initialised module with an unrelated-looking `AttributeError`.
from urbenmend.reporting.models import SeveritySignal


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


class SeverityKeywordStatus(models.TextChoices):
    """Lifecycle for a keyword rule (data-model §14: `Active → Retired`, never deleted).

    ⚠️ **Not `CategoryStatus`, even though the two values are identical today.** `choices` are baked
    into every migration that touches a field, so a shared enum makes any future member a schema
    change on *both* tables at once — and the two lifecycles have no reason to move together. A
    retired keyword is a rule an admin decided was wrong (FR-30); a retired category is a
    taxonomy decision with authority-scope and clustering consequences.
    """

    ACTIVE = "active", _("Active")
    RETIRED = "retired", _("Retired")


class SeverityKeyword(models.Model):
    """A bilingual indicator phrase mapped to a severity band (data-model §14, FR-13a/FR-14/FR-30).

    The reference data behind the deterministic fallback (`keywords.KeywordFallbackClassifier`) and
    behind FR-15's severity rationale: FR-14 requires severity to reflect "high-risk indicators
    ('danger', 'accident', 'flood', 'gas leak', 'live wire', 'collapse') in **both Bangla and
    English**", and this table is where those indicators live.

    ⚠️ **Data, not code** (NFR-11/FR-30). Seeded by migration and then admin-editable — unlike
    `Category`, whose admin is deliberately read-only because every node change ripples into
    authority scope (BR-26) and clustering. Editing a keyword ripples nowhere: it changes what the
    *next* classification matches, which is exactly the tuning loop FR-30 asks for.

    ⚠️ **The stored form is the match form.** `save()` normalizes through
    `keywords.normalize_term()`, the same function the engine runs report text through, so what an
    admin sees in the list is literally what will be matched. Storing the raw input instead would
    let `Live  Wire` sit in the table looking correct and never fire.

    [doc: data-model §14; PRD FR-13a, FR-14, FR-15, FR-30, NFR-4, NFR-8, NFR-11; Arch §6]
    """

    # The indicator phrase, normalized. 100 characters is generous for a phrase — this is
    # `'live wire'`, not a sentence.
    #
    # ⚠️ **UNIQUE on `term` alone, deliberately not on `(term, language)`.** Matching ignores
    # `language` entirely (a code-mixed report carries English indicators — see
    # `keywords.KeywordRule.language`), so two rows sharing a term are indistinguishable to the
    # engine no matter what languages they claim. A `(term, language)` constraint would permit a
    # distinction the matcher cannot honour, and the second row's severity would apply to reports
    # in the other language too — silently.
    #
    # ⚠️ `MinLengthValidator(2)` because a one-character term is a rule that fires on nearly every
    # report. Validators run in `full_clean()`, which the admin form calls and `bulk_create` does
    # not — the right place for it: an admin edit is rejected while the seeded migration and any
    # future bulk load stay unblocked.
    term = models.CharField(
        max_length=100,
        unique=True,
        validators=[MinLengthValidator(2)],
        help_text=_(
            "Indicator phrase. Matched from the start of a word, so 'collapse' also matches "
            "'collapsed'. Stored lower-cased with whitespace collapsed."
        ),
    )

    # NFR-8's two languages. Descriptive only — see the UNIQUE note above and
    # `keywords.KeywordRule.language`: nothing in the matcher reads it.
    #
    # ⚠️ Not `identity.Language`: that enum means a *user's preferred* language (API §6.2
    # `preferredLanguage`). Reusing it here would tie a keyword's script to a notification
    # preference, and the first change to either would be wrong for the other.
    language = models.CharField(
        max_length=8,
        choices=[("en", _("English")), ("bn", _("Bangla"))],
        default="en",
        db_index=True,
    )

    severity = models.CharField(
        max_length=16,
        choices=SeveritySignal.choices,
        help_text=_("Band this indicator implies. Critical is reserved for life-safety (FR-14)."),
    )

    # data-model §14 — "Severity Keyword **many → 0..1** Category".
    #
    # ⚠️ **Nullable on purpose, and `None` is a useful answer.** `injured` or `explosion` raises
    # severity without saying which department owns the problem; forcing a category on such a
    # phrase would file unrelated reports under whichever node it was arbitrarily assigned.
    #
    # `PROTECT` because categories retire rather than delete (data-model §5) — a delete that would
    # strand keyword rules should fail loudly.
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="severity_keywords",
        null=True,
        blank=True,
    )

    status = models.CharField(
        max_length=16,
        choices=SeverityKeywordStatus.choices,
        default=SeverityKeywordStatus.ACTIVE,
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    # Unlike `Category` (which has no `updated_at` because nodes are migration-only), keywords are
    # edited from the admin as a routine tuning activity — "when did this rule change?" is a
    # question an operator will actually ask when fallback accuracy shifts (NFR-9).
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "classification_severity_keyword"
        verbose_name = _("severity keyword")
        verbose_name_plural = _("severity keywords")
        ordering = ["term"]
        indexes: ClassVar[list[models.Index]] = [
            # The fallback's only read: every active rule, in one query, on every classification
            # that degrades (T3.4). Small table, but the index keeps it off a sequential scan as
            # the retired set grows.
            models.Index(fields=["status"], name="classification_kw_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.term} → {self.severity}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Normalize the term before every write.

        ⚠️ **In `save()`, not in `clean()`.** DRF never calls `full_clean()`, and neither does a
        `queryset.update()` or a management command — the project settled this in T1.2 for email
        and phone normalization and the reason is unchanged. `clean()` would normalize only what
        the admin form touches, leaving every other path to store a term that cannot match.

        ⚠️ **`bulk_create` still bypasses this**, which is why the seed in
        `migrations/0003_severity_keyword.py` stores already-normalized literals and
        `tests/test_severity_keyword.py` asserts that they are.
        """
        self.term = normalize_term(self.term)
        super().save(*args, **kwargs)
