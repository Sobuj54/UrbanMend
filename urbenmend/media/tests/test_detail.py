"""`GET /media/{id}` and `DELETE /media/{id}` — the read and the removal (T2.4, API §6.4).

Two rules carry most of this module: the read is **public** (Q7 resolved), and removal is a **state
change** rather than a row delete — which is the only reason `410 GONE` is expressible afterwards.

[doc: API §6.4, §4.2; FR-31, Q7; database.md "No hard deletes"]
"""

from __future__ import annotations

import uuid

import pytest
from django.test import Client
from django.urls import reverse

from urbenmend.identity.models import Role, User
from urbenmend.identity.tests.factories import UserFactory
from urbenmend.media.models import Media, MediaState
from urbenmend.media.tests.factories import MediaFactory, ReadyMediaFactory
from urbenmend.reporting.models import ReportStatus
from urbenmend.reporting.tests.factories import ReportFactory

pytestmark = pytest.mark.django_db


def _url(media_id: object) -> str:
    return reverse("api:media-detail", kwargs={"media_id": str(media_id)})


def _signed_in(role: str = Role.CITIZEN, **kwargs: object) -> tuple[Client, User]:
    user = UserFactory.create(role=role, **kwargs)
    client = Client()
    client.force_login(user)
    return client, user


# --------------------------------------------------------------------------------------
# GET — public read
# --------------------------------------------------------------------------------------


def test_an_anonymous_caller_may_read_a_ready_photo() -> None:
    """Q7 RESOLVED: media visibility follows the owning report's, and reports are public.

    ⚠️ This is why `MediaDetailView` uses `AllowAny` rather than `IsAuthenticated` — and why the
    role rule for `DELETE` had to move into the service instead of onto the view.
    """
    media = ReadyMediaFactory.create()

    response = Client().get(_url(media.pk))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(media.pk)
    assert body["state"] == MediaState.READY
    assert body["url"]
    assert body["thumbnailUrl"]


def test_a_still_processing_photo_is_returned_with_a_null_thumbnail() -> None:
    """⚠️ Not a `404`. §6.4's response carries `state` precisely so a client can render a
    placeholder; turning a missing thumbnail into "not found" would make an ordinary few-seconds-old
    upload look deleted."""
    media = MediaFactory.create()

    response = Client().get(_url(media.pk))

    assert response.status_code == 200
    assert response.json()["state"] == MediaState.PROCESSING
    assert response.json()["thumbnailUrl"] is None


def test_a_failed_photo_is_returned_rather_than_hidden() -> None:
    """`FAILED` is a state a client renders, not an absence. Same reasoning as `PROCESSING`."""
    media = MediaFactory.create(state=MediaState.FAILED, failure_reason="OSError: boom")

    response = Client().get(_url(media.pk))

    assert response.status_code == 200
    assert response.json()["state"] == MediaState.FAILED
    # ⚠️ The operator-facing reason never crosses the wire — it can quote file contents (NFR-12).
    assert "boom" not in response.content.decode()


def test_an_unknown_id_is_404() -> None:
    response = Client().get(_url(uuid.uuid4()))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_a_non_uuid_id_is_404_from_the_router() -> None:
    """⚠️ `<uuid:media_id>` refuses it at the routing layer, so a scan for `/media/1` never reaches
    a view — and no selector has to defend against a `ValueError` from the ORM to avoid a `500`."""
    response = Client().get("/api/v1/media/1")

    assert response.status_code == 404


def test_a_moderated_photo_is_410_not_404() -> None:
    """⚠️ The distinction is a disclosure decision, not a nicety (FR-31, §4.2).

    `404` for a moderated photo leaves a client retrying forever and erases the fact that
    moderation acted; `410` for an id that never existed confirms to a scanner that the id had once
    been valid. Only a surviving row can answer `410`, which is why removal is a state change.
    """
    media = MediaFactory.create(state=MediaState.REMOVED)

    response = Client().get(_url(media.pk))

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "GONE"


def test_the_gone_body_carries_no_url() -> None:
    """Belt and braces: the serializer also returns `null` for a removed row, so a future caller
    that renders one without going through the selector still cannot mint a presigned URL for it."""
    from urbenmend.media.serializers import MediaResponseSerializer

    media = MediaFactory.create(state=MediaState.REMOVED)

    rendered = MediaResponseSerializer(media).data

    assert rendered["url"] is None
    assert rendered["thumbnailUrl"] is None


# --------------------------------------------------------------------------------------
# DELETE — author pre-triage, or Admin moderation
# --------------------------------------------------------------------------------------


def test_the_author_may_remove_a_photo_from_a_pre_triage_report() -> None:
    report = ReportFactory.create(status=ReportStatus.SUBMITTED)
    media = MediaFactory.create(owner=report.author, report=report)
    client = Client()
    client.force_login(report.author)

    response = client.delete(_url(media.pk))

    assert response.status_code == 204
    media.refresh_from_db()
    assert media.state == MediaState.REMOVED


def test_removal_is_a_state_change_and_the_row_survives() -> None:
    """database.md "No hard deletes" — and `410` is only expressible while the row exists."""
    media = MediaFactory.create()
    client = Client()
    client.force_login(media.owner)

    client.delete(_url(media.pk))

    assert Media.objects.filter(pk=media.pk).exists()


def test_the_stored_object_stays_in_the_bucket() -> None:
    """⚠️ FR-31 is moderation, not erasure.

    A removal may be reviewed or reversed; deleting the `FieldFile` here would make that
    irreversible, and it would run outside the transaction — so a rollback would leave a live row
    pointing at nothing.
    """
    media = MediaFactory.create()
    stored_name = media.file.name
    assert stored_name  # a fixture with no stored object would make the assertions below vacuous
    client = Client()
    client.force_login(media.owner)

    client.delete(_url(media.pk))

    media.refresh_from_db()
    assert media.file.name == stored_name
    assert media.file.storage.exists(stored_name)


def test_an_unattached_photo_can_be_removed_by_its_uploader() -> None:
    """The pre-triage lock reads `media.report`, which is `None` in the §6.4→§6.3 window.

    ⚠️ A lock written as `not media.report.is_editable` would raise `AttributeError` — a `500` — on
    the most ordinary case there is: a citizen who uploaded a photo and changed their mind before
    submitting.
    """
    media = MediaFactory.create()
    client = Client()
    client.force_login(media.owner)

    response = client.delete(_url(media.pk))

    assert response.status_code == 204


def test_the_author_may_not_remove_once_the_report_is_triaged() -> None:
    """`409 NOT_EDITABLE` — the same lock T2.8 applies to the report itself.

    A photo an Authority has already been dispatched on must not vanish from underneath them.
    """
    report = ReportFactory.create(status=ReportStatus.TRIAGED)
    media = MediaFactory.create(owner=report.author, report=report)
    client = Client()
    client.force_login(report.author)

    response = client.delete(_url(media.pk))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NOT_EDITABLE"
    media.refresh_from_db()
    assert media.state != MediaState.REMOVED


def test_an_admin_moderating_is_not_bound_by_the_edit_lock() -> None:
    """⚠️ **Deliberately exempt, and this is the whole of FR-31.**

    Acting on content *after* it has been seen is what moderation is for. An Admin held to the
    author's pre-triage lock could not remove anything that had been triaged — which is precisely
    the content a takedown request is about.
    """
    report = ReportFactory.create(status=ReportStatus.TRIAGED)
    media = MediaFactory.create(owner=report.author, report=report)
    client, _ = _signed_in(Role.ADMIN)

    response = client.delete(_url(media.pk))

    assert response.status_code == 204
    media.refresh_from_db()
    assert media.state == MediaState.REMOVED


def test_another_citizen_may_not_remove_someone_elses_photo() -> None:
    media = MediaFactory.create()
    client, _ = _signed_in()

    response = client.delete(_url(media.pk))

    assert response.status_code == 403
    media.refresh_from_db()
    assert media.state != MediaState.REMOVED


def test_an_authority_may_not_remove_a_photo() -> None:
    """⚠️ Removal is author-or-Admin. An Authority acts on Issues; FR-31 moderation is Admin's.

    Reading `role != CITIZEN` as "privileged, so allowed" is the mistake this catches.
    """
    media = MediaFactory.create()
    client, _ = _signed_in(Role.AUTHORITY)

    response = client.delete(_url(media.pk))

    assert response.status_code == 403
    media.refresh_from_db()
    assert media.state != MediaState.REMOVED


def test_the_denial_names_neither_the_role_nor_the_owner() -> None:
    """T1.5: "Only the author or an admin may do this" tells an attacker which account to compromise."""
    media = MediaFactory.create()
    client, _ = _signed_in()

    response = client.delete(_url(media.pk))

    message = response.json()["error"]["message"].lower()
    assert "author" not in message
    assert "admin" not in message


def test_an_anonymous_delete_is_401_not_403() -> None:
    """⚠️ `AllowAny` lets `request.user` be `AnonymousUser`, so the method checks authentication
    itself — otherwise `remove_media()` would read `.role` off a model that has none, and a `500`
    would stand in for the `401` §4.2 requires."""
    media = MediaFactory.create()

    response = Client().delete(_url(media.pk))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    media.refresh_from_db()
    assert media.state != MediaState.REMOVED


def test_a_delete_against_an_already_moderated_photo_is_410() -> None:
    """The selector runs first, so "gone" is the honest answer rather than a second `204`.

    ⚠️ This is the boundary of `remove_media()`'s idempotent short-circuit: that branch covers a
    *service* caller (a management command, a future bulk moderation), while over HTTP the read
    rule wins. Being told `204` about content an Admin took down would imply the caller's own
    removal succeeded.
    """
    media = MediaFactory.create(state=MediaState.REMOVED)
    client = Client()
    client.force_login(media.owner)

    response = client.delete(_url(media.pk))

    assert response.status_code == 410


def test_a_delete_of_an_unknown_id_is_404() -> None:
    client, _ = _signed_in()

    response = client.delete(_url(uuid.uuid4()))

    assert response.status_code == 404


def test_an_authenticated_delete_without_a_csrf_token_is_refused() -> None:
    """⚠️ The T1.3 trap: `authentication_classes = []` is what silently disables CSRF, and this view
    has a mutating method on it. `AllowAny` skips the *permission* check while `SessionAuthentication`
    still runs, so this must be a `403`."""
    media = MediaFactory.create()
    client = Client(enforce_csrf_checks=True)
    client.force_login(media.owner)

    response = client.delete(_url(media.pk))

    assert response.status_code == 403
    media.refresh_from_db()
    assert media.state != MediaState.REMOVED


def test_an_anonymous_get_needs_no_csrf_token() -> None:
    """The other half of the same decision: the public read must stay reachable without a session."""
    media = ReadyMediaFactory.create()

    response = Client(enforce_csrf_checks=True).get(_url(media.pk))

    assert response.status_code == 200
