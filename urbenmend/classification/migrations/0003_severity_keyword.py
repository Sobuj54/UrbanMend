"""
Severity keyword table + seed (T3.3, FR-13a/FR-14/FR-30).

The `CreateModel` below is Django-generated; the `RunPython` seed and everything above it were
added by hand — a generated migration is a draft [doc: DevOps §7].

⚠️ **The seed is a starting list, not a specification.** Unlike `0001`'s taxonomy (which *is* PRD
§6.2 and must not drift), these phrases are an editable starting point: FR-30 makes them
admin-managed and NFR-11 makes them data, so an operator tuning fallback accuracy adds rows in the
admin rather than shipping a migration. What is doc-derived is the *shape*: FR-14 names the
indicators ("danger", "accident", "flood", "gas leak", "live wire", "collapse") and requires them
"in **both Bangla and English**", and Q2 reserves Critical for life-safety.

⚠️ **Every term here is stored pre-normalized**, because `bulk_create` bypasses
`SeverityKeyword.save()` and therefore bypasses `keywords.normalize_term()`. A term with a capital
letter or a double space would sit in the table looking correct and never match a single report.
`tests/test_severity_keyword.py::test_every_seeded_term_is_already_normalized` is the guard.

⚠️ **Matching is prefix-from-word-start** (`keywords._compile`), which is why this list carries
stems rather than every inflection: `collapse` covers "collapsed", `flood` covers
"flooding"/"flooded", and the Bangla stems cover their suffixed forms. It is also why the list
avoids short generic stems — a two-letter term would fire on half the dictionary.

⚠️ **A stem ending in a silent `e` does NOT cover its `-ing` form**, because English drops the `e`:
"collapsing" shares only `collaps` with `collapse`, so the prefix match misses it. This is the one
inflection rule the matcher cannot absorb, and it under-triages *silently* — the report still
classifies, one band too low, with a rationale that looks reasonable. The fix here is a second
explicit row rather than the shorter stem `collaps`, which would also fire on "collapsible".
`tests/test_keywords.py::test_a_term_matches_its_english_inflections` is the guard, and the reason
this note exists is that the first version of this seed made exactly that mistake in the Critical
band.

⚠️ **'hospital' and 'school' are deliberately absent.** API §6.5's rationale example quotes
`'hospital'`, but C-10 forbids POI/proximity data from affecting severity or ordering — it is
display-only. A keyword on a facility name is the same input arriving through a different door, and
seeding it would make the fallback the one classifier that quietly breaks C-10. `children` *is*
seeded because FR-14 names it as an indicator of who is at risk, not as a place.

[doc: data-model §14; PRD FR-13a, FR-14, FR-15, FR-30, NFR-4, NFR-8, NFR-11; Arch §6; Q2 RESOLVED]
"""

from typing import Any

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models

# (term, language, severity, category slug or None).
#
# A `None` category is a real modelling choice, not a gap (data-model §14 — "many → 0..1"):
# `injured` and `explosion` raise the band without saying whose problem it is, and filing them
# under an arbitrary node would mis-route unrelated reports.
#
# The Low band is mostly category-bearing on purpose. FR-13a requires the fallback to assign a
# category *and* a severity, so the engine needs ordinary nouns ("drain", "বাতি") to have somewhere
# to point — without them a routine report matches nothing and lands in the `other` sink.
#
# ⚠️ The Bangla column is a first pass by a non-native writer and is flagged for native review; it
# is admin-editable precisely so that review needs no deploy (FR-30).
SEVERITY_KEYWORDS: tuple[tuple[str, str, str, str | None], ...] = (
    # --- Critical — life-safety only (FR-14, Q2: collapse, live wire, gas leak, severe flooding).
    # ⚠️ Nothing routine belongs in this band, and no *default* may ever reach it: the fallback's
    # unmatched case is Medium (`keywords.DEFAULT_SEVERITY`) for exactly this reason.
    ("live wire", "en", "critical", "electrical"),
    ("exposed wire", "en", "critical", "electrical"),
    ("electrocution", "en", "critical", "electrical"),
    ("gas leak", "en", "critical", None),
    ("collapse", "en", "critical", "public_structures"),
    # ⚠️ A second row, not a shorter `collaps` stem: the silent `e` means "collapsing" shares no
    # prefix with "collapse" (see the module docstring), and a report of a wall *in the act of*
    # collapsing is the most urgent form of this indicator, not a lesser one.
    ("collapsing", "en", "critical", "public_structures"),
    ("explosion", "en", "critical", None),
    ("landslide", "en", "critical", None),
    ("sinkhole", "en", "critical", "roads"),
    # "on fire" rather than "fire": the bare noun fires on "fire hydrant" and "fire station",
    # which would put routine reports in the band reserved for life-safety.
    ("on fire", "en", "critical", None),
    ("severe flooding", "en", "critical", "water_drainage"),
    ("বিদ্যুৎস্পৃষ্ট", "bn", "critical", "electrical"),  # electrocuted
    ("খোলা তার", "bn", "critical", "electrical"),  # exposed wire — ⚠️ never seed bare "তার": it is
    # also the everyday pronoun "his/her", so it would match a large share of all Bangla text.
    ("গ্যাস লিক", "bn", "critical", None),  # gas leak
    ("ধস", "bn", "critical", "public_structures"),  # collapse
    ("বিস্ফোরণ", "bn", "critical", None),  # explosion
    ("আগুন", "bn", "critical", None),  # fire
    # --- High — hazardous, not immediately life-threatening.
    ("danger", "en", "high", None),  # covers "dangerous"
    ("accident", "en", "high", None),
    ("injured", "en", "high", None),
    ("children", "en", "high", None),  # FR-14's own acceptance example
    ("open manhole", "en", "high", "water_drainage"),
    ("short circuit", "en", "high", "electrical"),
    ("sparking", "en", "high", "electrical"),
    ("power line", "en", "high", "electrical"),
    ("flood", "en", "high", "water_drainage"),  # covers "flooding"/"flooded"
    ("sewage overflow", "en", "high", "sanitation_waste"),
    ("বিপদ", "bn", "high", None),  # danger
    ("দুর্ঘটনা", "bn", "high", None),  # accident
    ("আহত", "bn", "high", None),  # injured
    ("শিশু", "bn", "high", None),  # child
    ("বন্যা", "bn", "high", "water_drainage"),  # flood
    ("খোলা ম্যানহোল", "bn", "high", "water_drainage"),  # open manhole
    ("শর্ট সার্কিট", "bn", "high", "electrical"),  # short circuit
    # --- Medium — a real defect, no immediate hazard.
    ("pothole", "en", "medium", "roads"),
    ("broken road", "en", "medium", "roads"),
    ("damaged road", "en", "medium", "roads"),
    ("blocked drain", "en", "medium", "water_drainage"),
    ("drain blocked", "en", "medium", "water_drainage"),
    ("water leak", "en", "medium", "water_drainage"),
    ("manhole", "en", "medium", "water_drainage"),
    ("waterlogging", "en", "medium", "water_drainage"),
    ("transformer", "en", "medium", "electrical"),
    ("overflowing bin", "en", "medium", "sanitation_waste"),
    ("broken footpath", "en", "medium", "public_structures"),
    ("গর্ত", "bn", "medium", "roads"),  # hole / pothole
    ("খানাখন্দ", "bn", "medium", "roads"),  # potholed, broken up
    ("রাস্তা ভাঙা", "bn", "medium", "roads"),  # broken road
    ("জলাবদ্ধতা", "bn", "medium", "water_drainage"),  # waterlogging
    ("পানি জমে", "bn", "medium", "water_drainage"),  # standing water
    ("ম্যানহোল", "bn", "medium", "water_drainage"),  # manhole
    ("ট্রান্সফরমার", "bn", "medium", "electrical"),  # transformer
    # --- Low — mostly here to carry a *category*, see the note above.
    ("road", "en", "low", "roads"),
    ("street light", "en", "low", "street_lighting"),
    ("streetlight", "en", "low", "street_lighting"),
    ("lamp post", "en", "low", "street_lighting"),
    ("drain", "en", "low", "water_drainage"),
    ("sewer", "en", "low", "water_drainage"),
    ("water supply", "en", "low", "water_drainage"),
    ("garbage", "en", "low", "sanitation_waste"),
    ("litter", "en", "low", "sanitation_waste"),
    ("bad smell", "en", "low", "sanitation_waste"),
    ("electric", "en", "low", "electrical"),  # covers "electrical"/"electricity"
    ("footpath", "en", "low", "public_structures"),
    ("bridge", "en", "low", "public_structures"),
    ("রাস্তা", "bn", "low", "roads"),  # road
    ("সড়ক", "bn", "low", "roads"),  # road
    ("সড়ক বাতি", "bn", "low", "street_lighting"),  # street light
    ("বাতি", "bn", "low", "street_lighting"),  # light
    ("ল্যাম্পপোস্ট", "bn", "low", "street_lighting"),  # lamp post
    ("ড্রেন", "bn", "low", "water_drainage"),  # drain
    ("নর্দমা", "bn", "low", "water_drainage"),  # sewer
    ("ময়লা", "bn", "low", "sanitation_waste"),  # dirt / refuse
    ("আবর্জনা", "bn", "low", "sanitation_waste"),  # garbage
    ("দুর্গন্ধ", "bn", "low", "sanitation_waste"),  # bad smell
    ("বিদ্যুৎ", "bn", "low", "electrical"),  # electricity
    ("ফুটপাত", "bn", "low", "public_structures"),  # footpath
    ("সেতু", "bn", "low", "public_structures"),  # bridge
)


def seed_keywords(apps: Any, schema_editor: Any) -> None:
    """Insert the starting keyword set.

    ⚠️ `apps.get_model` rather than a direct import, and no call into `services.py` — a data
    migration runs against the historical model state, not today's code
    [doc: database.md "must use apps.get_model(...) and must not import from application code"].
    That also means `SeverityKeyword.save()` is *not* the historical model's save, so the
    normalization it performs does not happen here; see the module docstring.
    """
    Category = apps.get_model("classification", "Category")
    SeverityKeyword = apps.get_model("classification", "SeverityKeyword")

    category_ids = dict(Category.objects.values_list("slug", "id"))

    rows = []
    for term, language, severity, slug in SEVERITY_KEYWORDS:
        if slug is not None and slug not in category_ids:
            # ⚠️ Raise rather than fall back to a NULL category. A typo'd slug that silently
            # becomes "no category" leaves a keyword that raises severity but can never categorise,
            # and nothing would ever surface it — whereas categories are seeded by `0001` and never
            # deleted (data-model §5), so a miss here really is a mistake in this file.
            raise ValueError(
                f"Severity keyword {term!r} names category {slug!r}, which is not in the taxonomy."
            )
        rows.append(
            SeverityKeyword(
                term=term,
                language=language,
                severity=severity,
                category_id=category_ids[slug] if slug is not None else None,
                status="active",
            )
        )

    # `ignore_conflicts` so re-running against a partially seeded database is harmless; `term` is
    # UNIQUE, which is what makes the conflict detectable.
    SeverityKeyword.objects.bulk_create(rows, ignore_conflicts=True)


def unseed_keywords(apps: Any, schema_editor: Any) -> None:
    """Remove only the rows this migration inserted, so `migrate classification zero` runs clean.

    ⚠️ Matched on `term`, never `.all()` — a blanket delete would take every keyword an admin added
    (FR-30) with it, so a down-then-up cycle would silently discard operator tuning
    [doc: database.md, CLAUDE.md "a real reverse deleting only what it seeded"].

    ⚠️ A real reverse rather than `RunPython.noop` even though the `CreateModel` reversal drops the
    whole table moments later — unlike `0002`, where the reversed `AddField` made the data
    restoration meaningless. Here the seed function is the kind of thing a later migration will
    want to reuse (re-seeding after a taxonomy change), and a reverse that only works because of
    its neighbours is a trap for whoever does that.
    """
    SeverityKeyword = apps.get_model("classification", "SeverityKeyword")
    seeded = [term for term, _lang, _sev, _slug in SEVERITY_KEYWORDS]
    SeverityKeyword.objects.filter(term__in=seeded).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("classification", "0002_category_slug"),
    ]

    operations = [
        migrations.CreateModel(
            name="SeverityKeyword",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "term",
                    models.CharField(
                        help_text=(
                            "Indicator phrase. Matched from the start of a word, so 'collapse' "
                            "also matches 'collapsed'. Stored lower-cased with whitespace "
                            "collapsed."
                        ),
                        max_length=100,
                        unique=True,
                        validators=[django.core.validators.MinLengthValidator(2)],
                    ),
                ),
                (
                    "language",
                    models.CharField(
                        choices=[("en", "English"), ("bn", "Bangla")],
                        db_index=True,
                        default="en",
                        max_length=8,
                    ),
                ),
                (
                    "severity",
                    models.CharField(
                        choices=[
                            ("critical", "Critical"),
                            ("high", "High"),
                            ("medium", "Medium"),
                            ("low", "Low"),
                        ],
                        help_text=(
                            "Band this indicator implies. Critical is reserved for life-safety "
                            "(FR-14)."
                        ),
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "Active"), ("retired", "Retired")],
                        db_index=True,
                        default="active",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "category",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="severity_keywords",
                        to="classification.category",
                    ),
                ),
            ],
            options={
                "verbose_name": "severity keyword",
                "verbose_name_plural": "severity keywords",
                "db_table": "classification_severity_keyword",
                "ordering": ["term"],
                "indexes": [models.Index(fields=["status"], name="classification_kw_status_idx")],
            },
        ),
        migrations.RunPython(seed_keywords, unseed_keywords),
    ]
