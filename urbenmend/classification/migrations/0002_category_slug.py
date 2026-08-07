"""
Category machine key (T1.5 prerequisite).

`0001` shipped `name_en` as the only identifier, but API §6.2 expresses authority scope as
`"categoryScope": ["roads","water_drainage"]` and §6.10 addresses nodes as
`PATCH /categories/{key}`. Neither is an English label, and the spec is authoritative over the
implementation [doc: CLAUDE.md, API §"Principles"]. T1.5's BR-26 scope rows are the first thing
to reference a category by key, so the column lands here.

⚠️ **A new migration, not an edit to `0001`.** `0001` has been applied
[doc: database.md "never edit a migration already applied"].

⚠️ **Three operations, not one `AddField(unique=True)`.** Add nullable → backfill → tighten is
the backward-compatible shape [doc: database.md "backward-compatible migrations only"]: a single
non-null unique add fails against the seven rows already in the table, and code from the previous
deploy — which does not know the column exists — keeps working after each step.
"""

from __future__ import annotations

from typing import Any

from django.db import migrations, models

# ⚠️ Keys, not labels — renaming a label must never move a scope row.
#
# `roads`, `water_drainage` and `electrical` are the tokens API §6.2/§6.10 uses verbatim; the
# remaining four follow the same lower_snake_case convention. They are machine keys derived from
# the Q1-confirmed taxonomy, not an answer to any open question.
SLUGS: tuple[tuple[str, str], ...] = (
    ("Roads & Transport", "roads"),
    ("Street Lighting", "street_lighting"),
    ("Water & Drainage", "water_drainage"),
    ("Sanitation & Waste", "sanitation_waste"),
    ("Electrical Hazards", "electrical"),
    ("Public Structures", "public_structures"),
    ("Other / Uncategorized", "other"),
)


def backfill_slugs(apps: Any, schema_editor: Any) -> None:
    """Key the seven seeded nodes.

    ⚠️ Matched on `name_en` because that is the only identifier `0001` created — this migration
    exists precisely because there was nothing better to match on. `apps.get_model`, and no
    import from application code [doc: database.md].
    """
    Category = apps.get_model("classification", "Category")
    for name_en, slug in SLUGS:
        Category.objects.filter(name_en=name_en).update(slug=slug)


# ⚠️ **`noop` is the correct reverse, and it is deliberate rather than lazy.** `RunPython` is not
# reversible without an explicit reverse callable and CI gates every app in both directions
# [doc: database.md, testing.md], so something has to be passed here — but the only thing this
# migration wrote is a column that the `AddField` reversal drops moments later, so there is no
# state to restore.
#
# ⚠️ An earlier version cleared the values first (`update(slug="")`) and **failed the down
# migration**: seven rows assigned the same `""` violate the UNIQUE index the `AlterField` above
# put on the column. `None` would have worked — reversing `AlterField` restores `null=True`
# first — but it would still be writing to a doomed column. Do not "improve" this back into a
# data-clearing pass.
unseed_slugs = migrations.RunPython.noop


class Migration(migrations.Migration):
    dependencies = [
        ("classification", "0001_initial"),
    ]

    operations = [
        # Step 1 — nullable, so the seven existing rows are not rejected.
        migrations.AddField(
            model_name="category",
            name="slug",
            field=models.SlugField(max_length=50, null=True, unique=True),
        ),
        # Step 2 — backfill before the constraint tightens, or step 3 fails on NULLs.
        migrations.RunPython(backfill_slugs, unseed_slugs),
        # Step 3 — match the model. Any row this migration did not key would fail here, which is
        # the intended direction: a category with no machine key cannot be scoped or filtered.
        migrations.AlterField(
            model_name="category",
            name="slug",
            field=models.SlugField(max_length=50, unique=True),
        ),
    ]
