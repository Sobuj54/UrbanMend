"""
Classification — Category taxonomy (T0.10, ❓Q1 RESOLVED 2026-08-07).

⚠️ The seed migration *is* the taxonomy — PRD §6.2's seven nodes, confirmed as Q1's answer.
Restating that list here and asserting equality is deliberately the one tautology worth keeping:
it makes any future edit to the applied migration, or a stray node added by a later one, fail
loudly. The remaining tests check properties the list itself cannot express.

[doc: PRD §6.2, data-model §5, FR-30/NFR-11]
"""

from __future__ import annotations

from django.db import IntegrityError
from django.db.models import F
from django.test import TestCase

from urbenmend.classification.models import Category, CategoryStatus

# The 7 nodes confirmed as Q1's answer (PRD §6.2). English names are the canonical identifiers;
# Bangla labels are display data (NFR-8) and are checked for presence, not for content — this
# suite cannot judge a translation.
CONFIRMED_TAXONOMY = {
    "Roads & Transport",
    "Street Lighting",
    "Water & Drainage",
    "Sanitation & Waste",
    "Electrical Hazards",
    "Public Structures",
    "Other / Uncategorized",
}

# The machine keys `0002` backfilled. ⚠️ `roads`, `water_drainage` and `electrical` are quoted
# verbatim in API §6.2/§6.10, so those three are contract, not convention — a client filter
# written against the spec breaks if they change.
CONFIRMED_SLUGS = {
    "roads",
    "street_lighting",
    "water_drainage",
    "sanitation_waste",
    "electrical",
    "public_structures",
    "other",
}

# ⚠️ Load-bearing, not filler: PRD §331 requires an out-of-set LLM category to be coerced here,
# and FR-13a's keyword fallback needs a terminal bucket. Never retire this node.
FALLBACK_SINK = "Other / Uncategorized"


class TestSeedTaxonomy(TestCase):
    """The migration's `RunPython` seed produced the confirmed set.

    No `setUp` — the rows come from the migration, which `pytest-django` has already applied to
    the test database. That is the point: these assertions run against the same seed a fresh
    deploy gets, not against fixtures a test invented.
    """

    def test_seeded_taxonomy_matches_q1_answer(self) -> None:
        names = set(Category.objects.values_list("name_en", flat=True))

        assert names == CONFIRMED_TAXONOMY

    def test_every_node_has_the_machine_key_the_api_emits(self) -> None:
        """⚠️ `0002` adds `slug` nullable, backfills, then tightens to NOT NULL. A node the
        backfill missed would fail the third step — but only if a row existed to miss, so this
        asserts the outcome rather than trusting the migration ran in the intended order."""
        assert set(Category.objects.values_list("slug", flat=True)) == CONFIRMED_SLUGS

    def test_api_documented_slugs_map_to_the_expected_nodes(self) -> None:
        """API §6.2 quotes `["roads","water_drainage"]` and §6.10 `PATCH /categories/{key}`.
        Those keys are a published contract, so which node each one points at is not free to
        drift — a swap would silently re-scope every Authority provisioned against it."""
        by_slug = dict(Category.objects.values_list("slug", "name_en"))

        assert by_slug["roads"] == "Roads & Transport"
        assert by_slug["water_drainage"] == "Water & Drainage"
        assert by_slug["electrical"] == "Electrical Hazards"

    def test_fallback_sink_exists_and_is_active(self) -> None:
        """⚠️ Without an active `Other`, an out-of-set LLM response has nowhere to land and
        FR-13a's fallback cannot satisfy "no issue is left untriaged"."""
        sink = Category.objects.get(name_en=FALLBACK_SINK)

        assert sink.status == CategoryStatus.ACTIVE

    def test_seeded_categories_are_all_active(self) -> None:
        assert not Category.objects.exclude(status=CategoryStatus.ACTIVE).exists()

    def test_seeded_categories_have_bangla_labels(self) -> None:
        """NFR-8: every node is bilingual. A blank `name_bn` renders the Bangla UI empty, and
        nothing else in the stack would notice."""
        assert all(category.name_bn.strip() for category in Category.objects.all())

    def test_bangla_labels_are_not_copies_of_the_english_ones(self) -> None:
        """A placeholder seed that duplicated `name_en` into `name_bn` would pass the presence
        check above while leaving the Bangla UI in English."""
        assert not Category.objects.filter(name_bn__exact=F("name_en")).exists()


class TestCategoryModel(TestCase):
    """Node properties T0.10 binds, independent of what the seed happens to contain."""

    def test_name_en_is_unique(self) -> None:
        """⚠️ Unique across *all* statuses, retired included. Freeing a retired node's name for
        reuse would let a new category inherit the historical Issues of a semantically
        different one."""
        with self.assertRaises(IntegrityError):
            Category.objects.create(name_en="Roads & Transport", name_bn="সড়ক ও পরিবহন")

    def test_str_is_the_canonical_english_name(self) -> None:
        """Admin lists and log lines render this; the Bangla label would be the wrong key."""
        assert str(Category.objects.get(name_en="Street Lighting")) == "Street Lighting"

    def test_slug_is_unique(self) -> None:
        """⚠️ Two nodes sharing a key would make `categoryScope: ["roads"]` ambiguous and an
        authority-scope grant non-deterministic about which node it actually covered."""
        with self.assertRaises(IntegrityError):
            Category.objects.create(slug="roads", name_en="Roadworks", name_bn="সড়ক কাজ")
