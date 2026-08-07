"""
Category taxonomy — baseline + seed (T0.10, ❓Q1 RESOLVED 2026-08-07).

The `CreateModel` below is Django-generated; the `RunPython` seed was added by hand — a
generated migration is a draft [doc: DevOps §7].

⚠️ **The seed is the taxonomy.** PRD §6.2's seven nodes, confirmed unchanged as Q1's answer.
Because the taxonomy is data rather than a Python enum (NFR-11/FR-30), this migration is the
only place the canonical list appears. Adding a node later is a new migration, never an edit to
this one — it has been applied [doc: database.md "never edit a migration already applied"].
"""

from __future__ import annotations

from typing import Any

from django.db import migrations, models

# PRD §6.2, confirmed as Q1's answer on 2026-08-07. English name is canonical; Bangla is the
# NFR-8 display label.
#
# ⚠️ `Other / Uncategorized` is load-bearing, not filler: PRD §331 requires an LLM category
# outside the allowed set to be coerced to it, and FR-13a's fallback needs a terminal bucket.
# Never retire this node.
TAXONOMY: tuple[tuple[str, str], ...] = (
    ("Roads & Transport", "সড়ক ও পরিবহন"),
    ("Street Lighting", "সড়ক বাতি"),
    ("Water & Drainage", "পানি ও নিষ্কাশন"),
    ("Sanitation & Waste", "পরিচ্ছন্নতা ও বর্জ্য"),
    ("Electrical Hazards", "বিদ্যুতিক বিপদ"),
    ("Public Structures", "সর্বজনীন কাঠামো"),
    ("Other / Uncategorized", "অন্যান্য"),
)


def seed_taxonomy(apps: Any, schema_editor: Any) -> None:
    """Insert the seven nodes.

    ⚠️ `apps.get_model` rather than a direct import, and no call into `services.py` — a data
    migration must run against the historical model state, not today's code
    [doc: database.md "must use apps.get_model(...) and must not import from application code"].
    """
    Category = apps.get_model("classification", "Category")
    # `bulk_create` with `ignore_conflicts` so re-running against a partially seeded database
    # is harmless. `name_en` is UNIQUE, which is what makes the conflict detectable.
    Category.objects.bulk_create(
        [Category(name_en=name_en, name_bn=name_bn, status="active") for name_en, name_bn in TAXONOMY],
        ignore_conflicts=True,
    )


def unseed_taxonomy(apps: Any, schema_editor: Any) -> None:
    """Remove the seeded nodes so `migrate classification zero` runs clean.

    ⚠️ Supplied because `RunPython` is **not reversible without an explicit reverse callable**,
    and CI gates reversibility for every app [doc: database.md, testing.md "migrate <app> zero"].

    Deletes only the seven rows this migration inserted, matched by name — not `.all()`. A
    blanket delete would take any category added by a later migration with it, so a
    down-then-up cycle would silently lose taxonomy nodes.
    """
    Category = apps.get_model("classification", "Category")
    Category.objects.filter(name_en__in=[name_en for name_en, _ in TAXONOMY]).delete()


class Migration(migrations.Migration):
    initial = True

    # No dependency on `identity`: Category stands alone here. The authority↔category scope
    # M2M (BR-26) lands in a later `identity` migration that names *this* app, keeping the
    # reference-data table free of a dependency on the user model.
    dependencies: list[tuple[str, str]] = []

    operations = [
        migrations.CreateModel(
            name="Category",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name_en", models.CharField(db_index=True, max_length=100, unique=True)),
                ("name_bn", models.CharField(max_length=100)),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "Active"), ("retired", "Retired")],
                        db_index=True,
                        default="active",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Category",
                "verbose_name_plural": "Categories",
                "db_table": "classification_category",
                "ordering": ["name_en"],
            },
        ),
        migrations.RunPython(seed_taxonomy, unseed_taxonomy),
    ]
