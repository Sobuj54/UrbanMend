"""`factory_boy` factories for `classification` (T2.1).

[doc: testing.md "factory_boy"; data-model §5; C-2]
"""

from __future__ import annotations

import factory

from urbenmend.classification.models import Category, CategoryStatus


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
