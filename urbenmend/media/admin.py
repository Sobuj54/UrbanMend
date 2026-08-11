"""
Media — Django admin registrations (T2.4).

Reference data and moderation tooling are surfaced through admin [doc: Arch §2.4, FR-30/31].

[doc: Arch §3 (FR-7, P3); API §6.4]
"""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.http import HttpRequest

from urbenmend.media.models import Media

# django-stubs types ModelAdmin as generic in the model, but the runtime class is not
# subscriptable — see the same alias in `identity/admin.py` for the full reasoning.
if TYPE_CHECKING:
    _MediaAdminBase = admin.ModelAdmin[Media]
else:
    _MediaAdminBase = admin.ModelAdmin


@admin.register(Media)
class MediaAdmin(_MediaAdminBase):
    """Read-only inspection of uploaded photos.

    ⚠️ **Fully read-only, and add is barred.** Every column here is either citizen-supplied bytes or
    a fact the pipeline derived from them; a hand-edited `state` would strand a row as `ready` with
    no thumbnail, or re-queue one already processed. `has_add_permission` is false because a Media
    row without an accompanying storage object is a broken read for every client that follows a
    presigned URL.

    ⚠️ **No delete** (database.md "No hard deletes"). Moderation is `state = removed`, which is what
    lets `GET /media/{id}` answer `410 GONE` (FR-31, API §4.2) — a deleted row answers `404` and
    erases the fact that moderation acted. Removing via the API is `DELETE /media/{id}`, which
    records the actor.

    ⚠️ **`failure_reason` is visible here and nowhere else.** It holds the decoder's own message,
    which can quote file contents (NFR-12), so it is deliberately absent from every serializer.
    """

    list_display = ["id", "owner", "report", "state", "image_format", "byte_size", "created_at"]
    list_filter = ["state", "image_format"]
    search_fields = ["id", "failure_reason"]
    # FKs to tables that grow without bound — the raw-id widget avoids a select box that loads
    # every user on every page render.
    raw_id_fields = ["owner", "report"]
    date_hierarchy = "created_at"

    def get_readonly_fields(self, request: HttpRequest, obj: Media | None = None) -> list[str]:
        """Every concrete field, derived rather than listed — a hand-written list stops covering a
        column the day one is added, and the columns still to come here are moderation state."""
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        """No add — a Media row without its storage object is a broken read."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Media | None = None) -> bool:
        """No delete — moderation sets `state = removed` (FR-31, database.md)."""
        return False
