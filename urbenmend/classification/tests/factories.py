"""`factory_boy` factories for `classification` (T2.1, T3.3).

[doc: testing.md "factory_boy"; data-model §5, §14; C-2]
"""

from __future__ import annotations

import factory

from urbenmend.classification.models import (
    Category,
    CategoryStatus,
    SeverityKeyword,
    SeverityKeywordStatus,
)
from urbenmend.reporting.models import SeveritySignal


class CategoryFactory(factory.django.DjangoModelFactory[Category]):
    """A taxonomy node.

    ⚠️ **Most tests should NOT use this** — `classification/0001` seeds the seven real categories
    and the test database is migrated, so `Category.objects.get(slug="roads")` returns the node
    the product actually ships. C-2 makes the taxonomy controlled; a test that invents
    `test-category` asserts against a node no report will ever carry. This factory exists for the
    cases where the *shape* is the subject: a Retired node (T1.6's `422`), or a slug that is
    deliberately absent from the seeded set.

    ⚠️ **`django_get_or_create = ("slug",)`** — `slug` is UNIQUE and seven values are already
    taken. Without it, `CategoryFactory(slug="roads")` raises `IntegrityError` instead of handing
    back the seeded row, and the error appears in whichever test happened to reach for a real slug.
    """

    class Meta:
        model = Category
        django_get_or_create = ("slug",)

    slug = factory.Sequence(lambda n: f"test-category-{n}")
    name_en = factory.LazyAttribute(lambda o: f"Test category {o.slug}")
    # ⚠️ Not a copy of `name_en`. `Category` requires both labels (NFR-8) and T0.10's suite
    # asserts the seeded rows' Bangla is not English — a factory that copies would teach the
    # opposite habit. Bangla for "test".
    name_bn = factory.LazyAttribute(lambda o: f"পরীক্ষা {o.slug}")
    status = CategoryStatus.ACTIVE


class RetiredCategoryFactory(CategoryFactory):
    """A retired node — matches no Issue, so a scope grant or a report hint on it is refused."""

    slug = factory.Sequence(lambda n: f"retired-category-{n}")
    status = CategoryStatus.RETIRED


class SeverityKeywordFactory(factory.django.DjangoModelFactory[SeverityKeyword]):
    """One fallback rule (data-model §14).

    ⚠️ **The sequence prefix keeps these out of the seeded set's way.** `0003` seeds ~78 real terms
    and `term` is UNIQUE, so a factory defaulting to a plausible word ("pothole") would collide with
    the migration on the first call and the `IntegrityError` would surface in whichever test happened
    to run first.

    ⚠️ **Deliberately no `django_get_or_create = ("term",)`**, unlike `CategoryFactory`. It would
    look like the same convenience and be a trap: `save()` normalizes the term, so a lookup on the
    *raw* value would miss a row that is already stored in normalized form, then insert a duplicate
    and fail on the UNIQUE constraint. Tests that want a seeded rule should read it —
    `SeverityKeyword.objects.get(term="live wire")` — rather than re-create it.

    ⚠️ **`category` defaults to `None`, which is a real state, not an omission**
    (`SeverityKeyword.category`: a phrase may raise severity without naming a department). Tests that
    need one pass a seeded node, so nothing here invents a taxonomy row.
    """

    class Meta:
        model = SeverityKeyword

    # Two characters minimum (`MinLengthValidator`), and `zz` keeps it clear of real vocabulary in
    # both scripts — a factory rule must never accidentally match a report another test wrote.
    term = factory.Sequence(lambda n: f"zzkeyword{n}")
    language = "en"
    severity = SeveritySignal.MEDIUM
    category = None
    status = SeverityKeywordStatus.ACTIVE
