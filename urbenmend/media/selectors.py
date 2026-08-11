"""
Media — read operations (T2.4).

Query functions for this module. Kept separate from services.py so reads never acquire write-path
side effects, and so the modules that consume this one have a single documented surface to call
[doc: Arch §3.1].

Rules for this file:
  - No writes, no `transaction.atomic`, no task enqueue.
  - Apply the caller's visibility rules here — a selector that returns rows the actor may
    not see is an authorization bug even though it wrote nothing [doc: Arch §3.1, FR-3].
  - Return querysets or domain objects, never DRF serializers or HTTP responses.

[doc: Arch §3 (FR-7, P3); API §6.4]
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Prefetch
from django.http import Http404

from urbenmend.api.exceptions import Gone
from urbenmend.media.models import Media, MediaState

if TYPE_CHECKING:
    from uuid import UUID

    from django.db.models import QuerySet

# ⚠️ **The attribute `visible_media_prefetch()` populates, named once.** A serializer reading
# `report.visible_media` and a selector writing `to_attr="media"` would not fail — Django would
# happily shadow the related manager and the serializer would read a *different*, unfiltered list.
# Both sides referencing this constant is what makes that impossible.
VISIBLE_MEDIA_ATTR = "visible_media"


def get_media_for_read(*, media_id: UUID | str) -> Media:
    """`GET /media/{id}` — the public read (API §6.4, Q7 RESOLVED: public).

    ⚠️ **No actor parameter, and that is the contract rather than an omission.** §6.4 makes media
    visibility "as owning report's visibility", and Q7 resolved report visibility as public. Adding
    an actor here would imply a per-caller rule that does not exist and would drift the moment
    someone assumed it was being applied.

    ⚠️ **`404` for absent, `410` for moderated — the distinction is deliberate and it is a
    disclosure decision, not a nicety.** api-conventions.md reserves `404` for "absent **or hidden
    from this caller**"; `410` is the deliberate admission that something *was* here (FR-31, §6.4).
    Answering `404` for a moderated photo would leave a client retrying forever; answering `410` for
    an id that never existed would confirm to a scanner that the id had once been valid.

    ⚠️ **A `FAILED` or still-`PROCESSING` photo is returned, not hidden.** §6.4's response carries
    `state` precisely so a client can render a placeholder; turning a missing thumbnail into a
    `404` would make an ordinary few-seconds-old upload look deleted.

    `Http404` rather than DRF's `NotFound` so this module stays DRF-free; the handler renders both
    into the same §4.1 envelope.
    """
    try:
        media = Media.objects.select_related("report").get(pk=media_id)
    except (Media.DoesNotExist, ValueError, TypeError) as exc:
        # `ValueError`/`TypeError` cover a malformed UUID reaching a service called outside the URL
        # converter (a management command, a test). A bad id is "not found", never a `500`.
        raise Http404("Media not found.") from exc

    if media.state == MediaState.REMOVED:
        raise Gone

    return media


def _visible_media() -> QuerySet[Media]:
    """The one definition of "photos a client may see", in upload order.

    ⚠️ **Both `media_for_report()` and `visible_media_prefetch()` build on this, and that is the
    point.** The rule is a moderation rule (FR-31): a `REMOVED` row must not reappear inside a
    report payload. Written twice — once for the single read and once for the list's prefetch — the
    two copies are free to drift, and the failure mode is that moderated content comes back on
    exactly one of the two endpoints.
    """
    return Media.objects.exclude(state=MediaState.REMOVED).order_by("created_at")


def media_for_report(*, report_id: UUID | str) -> list[Media]:
    """The visible photos of one Report, in upload order (API §6.3 `media[]`).

    ⚠️ **Moderated rows are excluded rather than rendered as `410`.** A `410` is the answer when a
    client asked for *that* photo by id; inside a report's `media[]` array there is nothing to
    answer about, and emitting a removed entry would put moderated content back in the payload
    FR-31 removed it from.

    Returns a list, not a queryset: this is consumed by a serializer that iterates once, and a lazy
    queryset there invites an N+1 when the caller is itself in a loop (T2.7's list endpoint). ⚠️ The
    list alone does **not** solve that N+1 — it only stops the same rows being re-fetched. The list
    endpoint uses `visible_media_prefetch()` instead, which is one query for the whole page.
    """
    return list(_visible_media().filter(report_id=report_id))


def visible_media_count(*, report_id: UUID | str) -> int:
    """How many photos a Report still has as evidence — BR-3's input on the edit path (T2.8).

    ⚠️ **Moderated rows are excluded, and that is the whole reason this is not
    `report.media.count()`.** BR-3 accepts a report with *either* a photo or an adequate
    description; a report whose only photo an Admin removed under FR-31 no longer has the photo, so
    an author blanking its description must be refused. Counting the removed row would let the
    report end up with no evidence at all — which `POST /reports` cannot produce and an edit
    therefore must not either.

    ⚠️ **Shares `_visible_media()` with the two read paths on purpose.** "Visible" is one rule; a
    count that answered a different question from the array `media_for_report()` renders would make
    BR-3 disagree with the payload the client can see.

    A count, not `len(media_for_report(...))`: this is called on a write path that never renders the
    rows, and fetching them to discard them costs a query's worth of bytes for a single integer.
    """
    return _visible_media().filter(report_id=report_id).count()


def visible_media_prefetch() -> Prefetch[str, QuerySet[Media], str]:
    """The `media[]` of a whole page of Reports in one query (API §6.3's list, T2.7).

    ⚠️ **A `Prefetch` rather than a plain `prefetch_related("media")`, because the plain form
    prefetches *every* row including moderated ones** — and the filtering would then have to happen
    in the serializer, in Python, where it is invisible to anyone reading the query. Here the
    exclusion is part of the query the database runs.

    ⚠️ **`to_attr` means `report.media` is left alone**, so a caller that ignores the prefetch gets
    the ordinary unfiltered manager rather than a silently-filtered one. That is the safe direction:
    the surprising result is a *missing* attribute (caught immediately) rather than a related manager
    that quietly means something different depending on how the row was fetched.
    """
    return Prefetch("media", queryset=_visible_media(), to_attr=VISIBLE_MEDIA_ATTR)
