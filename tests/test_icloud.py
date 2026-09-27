# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

from copy import deepcopy
from unittest.mock import Mock

import pytest
import requests

from timesframesync.config import SyncError
from test_google_photos import video_response
from test_sources import ck_count, ck_photo, icloud_client, resolved


def listing(records):
    count = sum(r["recordType"] == "CPLAsset" for r in records)
    return [resolved(), ck_count(count), *([{"records": records}] if count else []), ck_count(count)]


def video_client(*responses):
    client = icloud_client(listing(ck_photo("clip", video=True)))
    client.album()
    client.session.request = Mock(side_effect=responses)
    return client


@pytest.mark.parametrize("name", ["resVidFullRes", "resVidLargeRes", "resVidMedRes", "resVidSmallRes", "resOriginalRes"])
def test_icloud_selects_video_renditions_and_original_fallback(name):
    records = ck_photo("clip", video=True)
    resource = records[0]["fields"].pop("resVidLargeRes")
    records[0]["fields"][name] = resource
    records[0]["fields"].pop("resJPEGMedRes")  # A cover is generated from the actual video.
    client = icloud_client(listing(records))
    album = client.album()
    assert album.video_count == 1 and album.photo_count == album.skipped == 0
    assert album.photos[0].revision == "video-checksum-clip:123"
    assert client.videos["clip"][1] == 11


def test_icloud_prefers_rendered_asset_then_prepared_master_video():
    records = ck_photo("clip", video=True)
    original = deepcopy(records[0]["fields"]["resVidLargeRes"])
    original["value"]["fileChecksum"] = "original"
    records[0]["fields"]["resOriginalRes"] = original
    client = icloud_client(listing(records))
    assert client.album().photos[0].revision == "video-checksum-clip:123"
    edited = deepcopy(original)
    edited["value"]["fileChecksum"] = "edited"
    records[1]["fields"]["resVidMedRes"] = edited
    client.json.side_effect = listing(records)
    assert client.album().photos[0].revision == "edited:123"


def test_icloud_revision_ignores_url_rotation_but_tracks_video_changes():
    records = ck_photo("clip", video=True)
    client = icloud_client(listing(records))
    before = client.album()
    resource = records[0]["fields"]["resVidLargeRes"]["value"]
    resource["downloadURL"] = "https://cvws-h2.icloud-content.com/new-signature"
    records[0]["fields"]["resJPEGMedRes"]["value"]["fileChecksum"] = "new-preview"
    client.json.side_effect = listing(records)
    assert client.album() == before
    assert client.videos["clip"][0] == resource["downloadURL"]
    resource["fileChecksum"] = "new-video"
    client.json.side_effect = listing(records)
    assert client.album().photos[0].revision != before.photos[0].revision
    client.json.side_effect = listing([])
    client.album()
    assert client.videos == {} and client.images == {}


@pytest.mark.parametrize("resource", [None, {}, "pending", {"size": 11},
    {"fileChecksum": "checksum", "downloadURL": "https://cvws-h2.icloud-content.com/clip", "size": True},
    {"fileChecksum": "checksum", "downloadURL": "", "size": 11},
    {"fileChecksum": "", "downloadURL": "https://cvws-h2.icloud-content.com/clip", "size": 11},
    {"fileChecksum": "checksum", "downloadURL": "https://cvws-h2.icloud-content.com/clip", "size": 0}])
def test_icloud_unready_or_malformed_video_fails_entire_album(resource):
    records = ck_photo("photo") + ck_photo("clip", video=True)
    records[2]["fields"]["resVidLargeRes"]["value"] = resource
    with pytest.raises(SyncError):
        icloud_client(listing(records)).album()


def test_icloud_live_photo_remains_a_photo():
    records = ck_photo("live")
    records[0]["fields"]["resOriginalVidComplRes"] = ck_photo("clip", video=True)[0]["fields"]["resVidLargeRes"]
    client = icloud_client(listing(records))
    album = client.album()
    assert album.photo_count == 1 and album.video_count == 0 and client.videos == {}


@pytest.mark.parametrize("content_type", ["video/mp4", "video/quicktime", "application/octet-stream"])
def test_icloud_streams_full_video_through_cdn_redirect(tmp_path, content_type):
    redirect = video_response(b"", status=302, Location="https://cvws-h3.icloud-content.com/movie")
    movie = video_response(**{"Content-Length": "11", "Content-Type": content_type})
    client = video_client(redirect, movie)
    destination = tmp_path / "clip.download"
    client.download_video("clip", destination)
    assert destination.read_bytes() == b"video-bytes"
    assert movie._content is False
    movie.iter_content.assert_called_once_with(1024 * 1024)
    for call in client.session.request.call_args_list:
        assert call.kwargs["stream"] is True and call.kwargs["allow_redirects"] is False
        assert call.kwargs["headers"] == {"Accept-Encoding": "identity"}
    redirect.close.assert_called_once()
    movie.close.assert_called_once()


@pytest.mark.parametrize("target", ["http://cvws-h2.icloud-content.com/clip", "https://icloud-content.com.evil.test/clip",
    "https://127.0.0.1/clip", "https://user:secret@cvws-h2.icloud-content.com/clip",
    "https://cvws-h2.icloud-content.com:9000/clip"])
def test_icloud_rejects_video_host_before_request_and_on_redirect(tmp_path, target):
    client = video_client()
    client.videos["clip"] = (target, 11)
    with pytest.raises(SyncError, match="unsupported video host"):
        client.download_video("clip", tmp_path / "clip")
    client.session.request.assert_not_called()
    client = video_client(video_response(status=302, Location=target))
    with pytest.raises(SyncError, match="unsupported video host"):
        client.download_video("clip", tmp_path / "clip")
    assert client.session.request.call_count == 1


@pytest.mark.parametrize("status,headers,content", [
    (200, {"Content-Type": "text/html"}, b"login"), (200, {"Content-Type": "image/jpeg"}, b"thumbnail"),
    (206, {}, b"partial"), (200, {}, b""), (200, {}, b"truncated"), (200, {}, b"too many bytes"),
    (200, {"Content-Length": "9"}, b"truncated"), (200, {"Content-Length": "11"}, b"truncated"),
    (200, {"Content-Length": "invalid"}, b"video-bytes"), (200, {"Content-Encoding": "gzip"}, b"video-bytes"),
    (302, {}, b"no-location"), (404, {}, b"expired")])
def test_icloud_rejects_nonvideo_or_wrong_resource_size(tmp_path, status, headers, content):
    response = video_response(content, status=status, **headers)
    with pytest.raises(SyncError) as error:
        video_client(response).download_video("clip", tmp_path / "clip")
    assert "secret=video" not in str(error.value)
    response.close.assert_called_once()


def test_icloud_stream_error_and_redirect_loop_are_bounded(tmp_path):
    response = video_response()
    def chunks(_):
        yield b"partial"
        raise requests.exceptions.ChunkedEncodingError("private-video-url")
    response.iter_content = chunks
    with pytest.raises(SyncError, match="interrupted") as error:
        video_client(response).download_video("clip", tmp_path / "clip")
    assert "private-video-url" not in str(error.value)
    response.close.assert_called_once()
    responses = [video_response(status=302, Location="/loop") for _ in range(6)]
    client = video_client(*responses)
    with pytest.raises(SyncError, match="too many"):
        client.download_video("clip", tmp_path / "clip")
    assert client.session.request.call_count == 6
    for response in responses:
        response.close.assert_called_once()


def test_icloud_requires_video_from_last_listing(tmp_path):
    client = video_client()
    with pytest.raises(SyncError, match="last album listing"):
        client.download_video("missing", tmp_path / "clip")
    client.session.request.assert_not_called()
