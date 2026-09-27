# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

import base64
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from timesframesync.config import Config, SyncError
from timesframesync.providers import create_source
from timesframesync.providers.onedrive import OneDrive
from timesframesync.providers.yandex_disk import YandexDisk
from timesframesync.sources_config import SourceConfig, validate_url
from test_google_photos import video_response


BASE = OneDrive.api_origin + "/_api/v2.1/drives/drive/albums/album"


def metadata(count=2):
    return {"id": "album", "name": "Photos", "eTag": "revision-1", "lastModifiedDateTime": "2026-01-01",
            "folder": {"childCount": count}, "mediaAlbum": {"albumItemCount": count}}


def item(asset_id="a", *, video=False):
    result = {"id": asset_id, "name": asset_id + ".jpg", "image": {"width": 10, "height": 20},
              "file": {"mimeType": "image/jpeg"}, "cTag": "content-1",
              "lastModifiedDateTime": "2026-01-01", "size": 100}
    if video:
        result.update(name=asset_id + ".mov", video={"duration": 500, "width": 2160, "height": 3840},
                      file={"mimeType": "video/quicktime"}, size=11)
        result.pop("image")
        result["@content.downloadUrl"] = "https://my.microsoftpersonalcontent.com/download?temporary-secret"
    return result


def client(pages, *, count=2, after=None):
    source = OneDrive("https://1drv.ms/a/secret")
    source.resolve = Mock(return_value=("drive", "album"))
    source.api = Mock(side_effect=[metadata(count), *pages, after if after is not None else metadata(count)])
    return source


def response(payload, status=200):
    return SimpleNamespace(json=lambda: payload, status_code=status)


def test_anonymous_token_and_share_resolution():
    source = OneDrive("https://1drv.ms/a/secret")
    source.request = Mock(side_effect=[response({"authScheme": "badger", "token": "secret-token"}),
                                       response({"id": "album", "parentReference": {"driveId": "drive"}})])
    assert source.resolve() == ("drive", "album")
    first, second = source.request.call_args_list
    assert first.args == ("POST", "https://api-badgerp.svc.ms/v1.0/token")
    assert first.kwargs["json"] == {"appId": source.app_id}
    share = "u!" + base64.urlsafe_b64encode(source.url.encode()).decode().rstrip("=")
    assert second.args == ("POST", source.api_origin + "/_api/v2.0/shares/" + share + "/driveitem")
    assert second.kwargs["headers"] == {"Authorization": "badger secret-token", "Prefer": "autoredeem"}
    assert not second.kwargs["allow_redirects"]
    assert "Authorization" not in source.session.headers


def test_onedrive_pagination_and_video_count():
    next_url = BASE + "/children?$skiptoken=next"
    source = client([{"value": [item()], "@odata.nextLink": next_url},
                     {"value": [item("v", video=True)]}])
    album = source.album()
    assert album.id == "drive:album" and album.name == "Photos"
    assert [(p.id, p.kind) for p in album.photos] == [("a", "photo"), ("v", "video")]
    assert album.photo_count == album.video_count == 1 and album.skipped == 0
    assert source.videos == {"v": (item(video=True)["@content.downloadUrl"], 11)}
    assert source.api.call_args_list[1].kwargs["params"] == {"top": 200}
    assert source.api.call_args_list[2].args == ("GET", next_url)
    assert source.api.call_args_list[2].kwargs["params"] is None
    assert source.api.call_args_list[-1].args == ("GET", BASE)


@pytest.mark.parametrize("pages,count", [
    ([{"value": []}], 1),  # Missing items, not an empty album.
    ([{"value": [item(), item()]}], 2),
    ([{"value": [item()]}], 0),
    ([{"value": None}], 0),
    ([{"value": [], "@odata.nextLink": BASE + "/children?next"}], 1),
    ([{"value": [item()], "@odata.nextLink": ""}], 1),
    ([{"value": [item()], "@odata.nextLink": BASE + "/children"}], 2),
    ([{"value": [item()], "@odata.nextLink": BASE + "/other"}], 2),
    ([{"value": [{"id": "missing-fields"}]}], 1),
    ([{"value": [dict(item(), deleted={})]}], 1),
    ([{"value": [dict(item(), remoteItem={})]}], 1),
])
def test_onedrive_incomplete_snapshot_fails(pages, count):
    with pytest.raises(SyncError):
        client(pages, count=count).album()


@pytest.mark.parametrize("change", ["tag", "count", "id"])
def test_onedrive_album_changed_during_read_fails(change):
    after = metadata(1)
    if change == "tag":
        after["eTag"] = "revision-2"
    elif change == "id":
        after["id"] = "other"
    else:
        after["folder"]["childCount"] = after["mediaAlbum"]["albumItemCount"] = 0
    with pytest.raises(SyncError, match="incomplete"):
        client([{"value": [item()]}], count=1, after=after).album()


def test_onedrive_confirmed_empty_album():
    assert client([{"value": []}], count=0).album().photos == []


@pytest.mark.parametrize("video", [False, True])
def test_onedrive_content_change_updates_revision_but_signed_url_does_not(video):
    before = item(video=video)
    after = dict(before, cTag="content-2")
    original = client([{"value": [before]}], count=1).album().photos[0]
    assert original.revision != client([{"value": [after]}], count=1).album().photos[0].revision
    after = dict(before, **{"@content.downloadUrl": "https://example.com/new-signature"})
    assert original == client([{"value": [after]}], count=1).album().photos[0]


@pytest.mark.parametrize("patch", [
    {"@content.downloadUrl": None}, {"@content.downloadUrl": ""}, {"@content.downloadUrl": {}},
    {"size": 0}, {"size": -1}, {"size": True}, {"size": "11"}, {"cTag": None},
    {"lastModifiedDateTime": ""}, {"video": "processing"}, {"file": {"mimeType": "image/jpeg"}},
])
def test_onedrive_unready_or_malformed_video_fails_whole_album(patch):
    movie = dict(item("clip", video=True), **patch)
    source = client([{"value": [item(), movie]}])
    with pytest.raises(SyncError, match="incomplete"):
        source.album()
    assert source.videos == {} and source.thumbnail_endpoints == {}


def test_onedrive_download_url_alias_and_refresh_replaces_old_links():
    movie = item("clip", video=True)
    movie["@microsoft.graph.downloadUrl"] = movie.pop("@content.downloadUrl")
    source = client([{"value": [movie]}], count=1)
    before = source.album()
    assert source.videos["clip"] == (movie["@microsoft.graph.downloadUrl"], 11)
    movie["@microsoft.graph.downloadUrl"] = "https://files.1drv.com/new-signature"
    source.api.side_effect = [metadata(1), {"value": [movie]}, metadata(1)]
    assert source.album() == before
    assert source.videos["clip"][0] == movie["@microsoft.graph.downloadUrl"]
    source.api.side_effect = [metadata(0), {"value": []}, metadata(0)]
    assert source.album().photos == [] and source.videos == {}


def video_client(*responses):
    source = client([{"value": [item("clip", video=True)]}], count=1)
    source.album()
    source.authorization = {"Authorization": "badger private-token", "Prefer": "autoredeem"}
    source.session.request = Mock(side_effect=responses)
    return source


@pytest.mark.parametrize("content_type", ["video/quicktime", "video/mp4", "application/octet-stream"])
def test_onedrive_video_download_streams_without_api_token_even_on_api_host(tmp_path, content_type):
    movie = video_response(**{"Content-Length": "11", "Content-Type": content_type})
    source = video_client(movie)
    destination = tmp_path / "clip.download"
    source.download_video("clip", destination)
    assert destination.read_bytes() == b"video-bytes"
    assert movie._content is False
    movie.iter_content.assert_called_once_with(1024 * 1024)
    call = source.session.request.call_args
    assert call.kwargs["headers"] == {"Accept-Encoding": "identity"}
    assert call.kwargs["stream"] is True and call.kwargs["allow_redirects"] is False
    assert "Authorization" not in source.session.headers
    movie.close.assert_called_once()


def test_onedrive_video_redirects_to_microsoft_cdn_without_token(tmp_path):
    redirect = video_response(status=302, Location="https://files.1drv.com/movie")
    movie = video_response()
    source = video_client(redirect, movie)
    source.download_video("clip", tmp_path / "clip")
    assert source.session.request.call_args.args == ("GET", "https://files.1drv.com/movie")
    for call in source.session.request.call_args_list:
        assert call.kwargs["headers"] == {"Accept-Encoding": "identity"}
        assert call.kwargs["allow_redirects"] is False
    redirect.close.assert_called_once()
    movie.close.assert_called_once()


@pytest.mark.parametrize("target", ["http://files.1drv.com/clip", "https://1drv.com.evil.test/clip",
    "https://my.microsoftpersonalcontent.com.evil.test/clip", "https://127.0.0.1/clip",
    "https://files.1drv.com:9000/clip", "https://user:secret@files.1drv.com/clip",
    "https://login.live.com/", "https://cvws-h2.icloud-content.com/clip"])
def test_onedrive_rejects_foreign_video_host_and_redirect(tmp_path, target):
    source = video_client()
    source.videos["clip"] = (target, 11)
    with pytest.raises(SyncError, match="unsupported video host"):
        source.download_video("clip", tmp_path / "clip")
    source.session.request.assert_not_called()
    source = video_client(video_response(status=302, Location=target))
    with pytest.raises(SyncError, match="unsupported video host"):
        source.download_video("clip", tmp_path / "clip")
    assert source.session.request.call_count == 1


@pytest.mark.parametrize("status,headers,content", [
    (200, {"Content-Type": "text/html"}, b"login"), (200, {"Content-Type": "image/jpeg"}, b"preview"),
    (206, {}, b"partial"), (200, {}, b""), (200, {}, b"short"), (200, {}, b"too many bytes"),
    (200, {"Content-Length": "5"}, b"short"), (200, {"Content-Length": "11"}, b"short"),
    (403, {}, b"expired"), (302, {}, b"no-location"),
])
def test_onedrive_video_requires_complete_resource_without_url_leak(tmp_path, status, headers, content):
    movie = video_response(content, status=status, **headers)
    with pytest.raises(SyncError) as error:
        video_client(movie).download_video("clip", tmp_path / "clip")
    assert "temporary-secret" not in str(error.value) and "private-token" not in str(error.value)
    movie.close.assert_called_once()


def test_onedrive_video_stream_error_closes_response_and_hides_url(tmp_path):
    movie = video_response()
    def chunks(_):
        yield b"part"
        raise requests.exceptions.ChunkedEncodingError("private-video-url")
    movie.iter_content = chunks
    with pytest.raises(SyncError, match="interrupted") as error:
        video_client(movie).download_video("clip", tmp_path / "clip")
    assert "private-video-url" not in str(error.value)
    movie.close.assert_called_once()


def test_onedrive_requires_video_from_last_listing(tmp_path):
    source = video_client()
    with pytest.raises(SyncError, match="last album listing"):
        source.download_video("missing", tmp_path / "clip")
    source.session.request.assert_not_called()


def test_onedrive_preview_never_sends_authorization_to_cdn():
    source = OneDrive("https://1drv.ms/a/secret")
    source.authorization = {"Authorization": "badger secret-token"}
    endpoint = source.api_origin + "/_api/v2.1/drives/drive/items/a/thumbnails"
    source.thumbnail_endpoints = {"a": endpoint}
    url = "https://centralus1-mediap.svc.ms/transform/thumbnail?private-signature"
    source.request = Mock(side_effect=[response({"value": [{"c2048x2048": {"url": url}}]}),
                                       SimpleNamespace(headers={"Content-Type": "image/jpeg"}, content=b"jpeg")])
    assert source.preview("a") == b"jpeg"
    first, second = source.request.call_args_list
    assert first.args == ("GET", endpoint) and first.kwargs["params"] == {"select": "c2048x2048"}
    assert first.kwargs["headers"] == source.authorization
    assert second.args == ("GET", url) and "headers" not in second.kwargs


@pytest.mark.parametrize("url", [
    "https://evil.example/_api/v2.1/children", "http://my.microsoftpersonalcontent.com/_api/v2.1/children",
    "https://user@my.microsoftpersonalcontent.com/_api/v2.1/children",
    "https://my.microsoftpersonalcontent.com.evil.example/_api/v2.1/children",
])
def test_onedrive_foreign_api_url_never_receives_token(url):
    source = OneDrive("https://1drv.ms/a/secret")
    source.request = Mock()
    with pytest.raises(SyncError):
        source.api("GET", url)
    source.request.assert_not_called()


@pytest.mark.parametrize("token", [{}, {"authScheme": "Bearer", "token": "secret"},
                                   {"authScheme": "badger", "token": ""}])
def test_onedrive_missing_anonymous_access_fails(token):
    source = OneDrive("https://1drv.ms/a/secret")
    source.request = Mock(return_value=response(token))
    with pytest.raises(SyncError):
        source.album()


def test_onedrive_network_failure_hides_link_and_token():
    source = OneDrive("https://1drv.ms/a/secret")
    source.session.request = Mock(side_effect=requests.ConnectionError("secret-token " + source.url))
    with pytest.raises(SyncError) as error:
        source.album()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("url", ["https://1drv.ms/a/secret", "https://1drv.ms/a/c/drive/share-token",
                                "https://1drv.ms/a/s!share-token"])
def test_onedrive_public_url_validation(url):
    validate_url("onedrive", url)


@pytest.mark.parametrize("url", ["https://1drv.ms/f/folder", "https://tenant.sharepoint.com/album", "http://1drv.ms/a/secret",
                                "https://1drv.ms/a/", "https://onedrive.live.com/?id=album"])
def test_onedrive_other_links_rejected(url):
    with pytest.raises(SyncError):
        validate_url("onedrive", url)


def test_new_provider_factory():
    for provider, url, expected in [("onedrive", "https://1drv.ms/a/secret", OneDrive),
                                    ("yandex_disk", "https://disk.yandex.ru/a/secret", YandexDisk)]:
        assert isinstance(create_source(SourceConfig("test", provider, url, "Frame"), Config()), expected)
