import logging

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from urbenmend.api.exceptions import UnprocessableEntity
from urbenmend.identity.tests.factories import UserFactory
from urbenmend.media import imaging
from urbenmend.media.services import upload_media

pytestmark = pytest.mark.django_db


def test_undecodable_upload_does_not_log_decoder_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "PII-SENTINEL-01700000000"

    def reject(_raw: bytes) -> str:
        raise imaging.UnreadableImage(secret)

    monkeypatch.setattr(imaging, "detect_format", reject)
    upload = SimpleUploadedFile("private-name.jpg", b"not-an-image", content_type="image/jpeg")

    with caplog.at_level(logging.WARNING), pytest.raises(UnprocessableEntity):
        upload_media(owner=UserFactory.create(), upload=upload)

    assert secret not in caplog.text
    assert "error_type" in caplog.text
