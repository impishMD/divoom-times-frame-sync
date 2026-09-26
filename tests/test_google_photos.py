# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

from io import BytesIO
from unittest.mock import Mock

import pytest
import requests

from timesframesync.config import SyncError
from timesframesync.providers.google_photos import GooglePhotos
from test_sources import google_client, google_data, google_item


def video_response(content=b"video-bytes", *, status=200, **headers):
    response = requests.Response()
    response.status_code = status
    response.headers.update({"Content-Type": "video/mp4", **headers})
    response.raw = BytesIO(content)
    response.iter_content = Mock(wraps=response.iter_content)
    response.close = Mock(wraps=response.close)
    return response


def video_client(*responses):
    client = GooglePhotos("https://photos.app.goo.gl/example")
    client.videos = {"clip": "https://lh3.googleusercontent.com/temporary-secret=dv"}
    client.session.request = Mock(side_effect=responses)
    return client


def test_google_streams_video_through_cdn_redirect(tmp_path):
    redirect = video_response(b"", status=302, Location="https://video-downloads.googleusercontent.com/clip")
    movie = video_response(**{"Content-Length": "11"})
    client = video_client(redirect, movie)
    destination = tmp_path / "clip.download"
    client.download_video("clip", destination)
    assert destination.read_bytes() == b"video-bytes"
    assert movie._content is False  # requests.content would buffer the whole response.
    movie.iter_content.assert_called_once_with(1024 * 1024)
    for call in client.session.request.call_args_list:
        assert call.kwargs["stream"] is True
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["headers"] == {"Accept-Encoding": "identity"}
    redirect.close.assert_called_once()
    movie.close.assert_called_once()


@pytest.mark.parametrize("target", ["http://video-downloads.googleusercontent.com/clip",
                                    "https://googleusercontent.com.example.org/clip",
                                    "https://127.0.0.1/clip", "https://accounts.google.com/login",
                                    "https://user:password@lh3.googleusercontent.com/clip"])
def test_google_rejects_untrusted_video_url_and_redirect(tmp_path, target):
    client = video_client()
    client.videos["clip"] = target
    with pytest.raises(SyncError, match="unsupported video host"):
        client.download_video("clip", tmp_path / "clip")
    client.session.request.assert_not_called()
    response = video_response(status=302, Location=target)
    client = video_client(response)
    with pytest.raises(SyncError, match="unsupported video host"):
        client.download_video("clip", tmp_path / "clip")
    assert client.session.request.call_count == 1
    response.close.assert_called_once()


@pytest.mark.parametrize("status,headers,content", [
    (200, {"Content-Type": "text/html"}, b"login"),
    (200, {"Content-Type": "image/jpeg"}, b"thumbnail"),
    (206, {}, b"partial"),
    (200, {}, b""),
    (200, {"Content-Length": "100"}, b"truncated"),
    (200, {"Content-Length": "100", "Content-Encoding": "identity"}, b"truncated"),
    (200, {"Content-Length": "invalid"}, b"invalid-length"),
    (200, {"Content-Length": "0"}, b"empty-length"),
    (302, {}, b"no-location"),
    (404, {}, b"processing"),
])
def test_google_rejects_nonvideo_or_incomplete_download(tmp_path, status, headers, content):
    response = video_response(content, status=status, **headers)
    client = video_client(response)
    with pytest.raises(SyncError) as error:
        client.download_video("clip", tmp_path / "clip")
    assert "temporary-secret" not in str(error.value)
    response.close.assert_called_once()


def test_google_interrupted_stream_closes_response_without_leaking_url(tmp_path):
    response = video_response()
    def chunks(_):
        yield b"partial"
        raise requests.exceptions.ChunkedEncodingError("https://cdn.example/private-token")
    response.iter_content = chunks
    with pytest.raises(SyncError, match="interrupted") as error:
        video_client(response).download_video("clip", tmp_path / "clip")
    assert "private-token" not in str(error.value)
    response.close.assert_called_once()


def test_google_redirect_loop_is_bounded(tmp_path):
    responses = [video_response(status=302, Location="/same") for _ in range(6)]
    client = video_client(*responses)
    with pytest.raises(SyncError, match="too many"):
        client.download_video("clip", tmp_path / "clip")
    assert client.session.request.call_count == 6
    for response in responses:
        response.close.assert_called_once()


def test_google_requires_a_video_from_last_listing(tmp_path):
    client = video_client()
    with pytest.raises(SyncError, match="last album listing"):
        client.download_video("missing", tmp_path / "clip")
    client.session.request.assert_not_called()


def test_google_video_revision_survives_cdn_url_rotation():
    item = google_item("clip", video=True)
    client = google_client(google_data([item], 1))
    before = client.album()
    item[1][0] = "https://lh3.googleusercontent.com/new-signed-url"
    client.request = google_client(google_data([item], 1)).request
    after = client.album()
    assert before == after
    assert client.videos["clip"] == item[1][0] + "=dv"
    client.request = google_client(google_data([], 0)).request
    client.album()
    assert client.videos == {}


def test_google_video_with_missing_media_data_blocks_album():
    item = google_item("clip", video=True)
    item[1] = None  # Google may still be processing a newly uploaded video.
    with pytest.raises(SyncError, match="incomplete"):
        google_client(google_data([item], 1)).album()
