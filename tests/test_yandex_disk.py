# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import json
from urllib.parse import unquote

import pytest
import requests

from timesframesync.config import SyncError
from timesframesync.providers.yandex_disk import YandexDisk
from timesframesync.sources_config import validate_url
from test_google_photos import video_response


def item(name="a", *, video=False):
    return {"id": name, "name": name + (".mp4" if video else ".jpg"), "type": "file", "modified": 1, "albumItemId": "cursor-" + name,
            "virus": False, "meta": {"mediatype": "video" if video else "image", "drweb": 1,
                                     "size": 11 if video else 100, "file_id": "file-" + name,
                                     "mimetype": "video/mp4" if video else "image/jpeg",
                                     "xxxlPreview": "https://downloader.disk.yandex.ru/preview/private-" + name}}


def bootstrap():
    root = {"id": "album", "name": "Test", "modified": 42, "hash": "secret-hash", "type": "album"}
    return {"rootResourceId": "album", "resources": {"album": root}, "environment": {"sk": "secret-sk"}}, root


def client(pages):
    source = YandexDisk("https://disk.yandex.ru/a/secret")
    source.bootstrap = Mock(side_effect=[bootstrap(), bootstrap()])
    source.page = Mock(side_effect=pages)
    return source


def test_yandex_pagination_and_video_count():
    source = client([{"completed": False, "resources": [item("a")]},
                     {"completed": True, "resources": [item("b"), item("v", video=True)]}])
    album = source.album()
    assert [(a.id, a.kind) for a in album.photos] == [("a", "photo"), ("b", "photo"), ("v", "video")]
    assert album.photo_count == 2 and album.video_count == 1 and album.skipped == 0
    assert source.videos == {"v": ("cursor-v", 11)}
    assert source.page.call_args_list[0].args == ("secret-hash", "secret-sk", None)
    assert source.page.call_args_list[1].args == ("secret-hash", "secret-sk", "cursor-a")
    assert set(source.images) == {"a", "b"}


@pytest.mark.parametrize("field,value", [("albumItemId", None), ("albumItemId", ""), ("size", 0),
    ("size", True), ("size", "11"), ("mimetype", "image/jpeg"), ("mimetype", None), ("drweb", 2)])
def test_yandex_malformed_or_unsafe_video_blocks_whole_album(field, value):
    movie = item("clip", video=True)
    (movie if field == "albumItemId" else movie["meta"])[field] = value
    source = client([{"completed": True, "resources": [item(), movie]}])
    with pytest.raises(SyncError):
        source.album()
    assert source.videos == {} and source.images == {} and source.download_context is None


def test_yandex_video_needs_no_preview_and_refresh_clears_old_items():
    movie = item("clip", video=True)
    movie["meta"].pop("xxxlPreview")
    source = client([{"completed": True, "resources": [movie]}])
    assert source.album().video_count == 1
    source.bootstrap.side_effect = [bootstrap(), bootstrap()]
    source.page.side_effect = [{"completed": True, "resources": []}]
    assert source.album().photos == [] and source.videos == {}


def api_response(payload=None):
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(payload if payload is not None else {
        "error": False, "statusCode": 200,
        "data": {"url": "https://downloader.disk.yandex.ru/disk/temporary-secret"}}).encode()
    return response


def video_client(*responses):
    source = client([{"completed": True, "resources": [item("clip", video=True)]}])
    source.album()
    source.session.request = Mock(side_effect=responses)
    return source


def test_yandex_resolves_single_video_then_streams_cdn_redirect(tmp_path):
    redirect = video_response(b"", status=302, Location="https://s1.storage.yandex.net/movie")
    movie = video_response(**{"Content-Length": "11"})
    source = video_client(api_response(), redirect, movie)
    destination = tmp_path / "clip.download"
    source.download_video("clip", destination)
    assert destination.read_bytes() == b"video-bytes"
    assert movie._content is False
    movie.iter_content.assert_called_once_with(1024 * 1024)
    first, *downloads = source.session.request.call_args_list
    assert first.args == ("POST", "https://disk.yandex.ru/public/api/album-download-url")
    assert json.loads(unquote(first.kwargs["data"])) == {"hash": "secret-hash", "sk": "secret-sk", "itemId": "cursor-clip"}
    assert first.kwargs["headers"]["X-Retpath-Y"] == source.url
    assert first.kwargs["allow_redirects"] is False
    for call in downloads:
        assert call.kwargs["headers"] == {"Accept-Encoding": "identity"}
        assert call.kwargs["allow_redirects"] is False and call.kwargs["stream"] is True
        assert "secret-sk" not in str(call) and "secret-hash" not in str(call)
    redirect.close.assert_called_once()
    movie.close.assert_called_once()


@pytest.mark.parametrize("payload", [{"error": True}, {"type": "captcha"}, {},
    {"error": False, "statusCode": 403, "data": {"read_only": True}},
    {"error": False, "statusCode": 200, "data": {}},
    {"error": False, "statusCode": 200, "data": {"url": ""}},
    {"error": False, "statusCode": 200, "data": {"url": []}}])
def test_yandex_unavailable_video_url_does_not_request_media(tmp_path, payload):
    source = video_client(api_response(payload))
    with pytest.raises(SyncError) as error:
        source.download_video("clip", tmp_path / "clip")
    assert source.session.request.call_count == 1
    assert "secret" not in str(error.value)
    assert not (tmp_path / "clip").exists()


@pytest.mark.parametrize("url", ["http://s1.storage.yandex.net/movie", "https://yandex.net.evil.test/movie",
    "https://127.0.0.1/movie", "https://s1.storage.yandex.net:9000/movie",
    "https://user:secret@s1.storage.yandex.net/movie", "https://files.1drv.com/movie"])
def test_yandex_validates_signed_video_url_and_each_redirect(tmp_path, url):
    source = video_client(api_response({"error": False, "statusCode": 200, "data": {"url": url}}))
    with pytest.raises(SyncError, match="unsupported video host"):
        source.download_video("clip", tmp_path / "clip")
    assert source.session.request.call_count == 1
    source = video_client(api_response(), video_response(status=302, Location=url))
    with pytest.raises(SyncError, match="unsupported video host"):
        source.download_video("clip", tmp_path / "clip")
    assert source.session.request.call_count == 2


@pytest.mark.parametrize("status,headers,content", [
    (200, {"Content-Type": "application/zip"}, b"album-archive"),
    (200, {"Content-Type": "image/jpeg"}, b"preview"), (200, {"Content-Type": "text/html"}, b"captcha"),
    (206, {}, b"partial"), (200, {}, b"short"), (200, {}, b"too many bytes"),
    (200, {"Content-Length": "5"}, b"short"), (200, {"Content-Length": "11"}, b"short"),
    (403, {}, b"expired")])
def test_yandex_video_rejects_archives_and_incomplete_files(tmp_path, status, headers, content):
    response = video_response(content, status=status, **headers)
    with pytest.raises(SyncError):
        video_client(api_response(), response).download_video("clip", tmp_path / "clip")
    response.close.assert_called_once()


def test_yandex_video_download_failure_does_not_expose_session(tmp_path):
    source = video_client(requests.ConnectionError("secret-sk temporary-secret"))
    with pytest.raises(SyncError) as error:
        source.download_video("clip", tmp_path / "clip")
    assert "secret" not in str(error.value)


def test_yandex_video_requires_item_from_last_complete_listing(tmp_path):
    source = video_client()
    with pytest.raises(SyncError, match="last album listing"):
        source.download_video("missing", tmp_path / "clip")
    source.session.request.assert_not_called()


@pytest.mark.parametrize("pages", [
    [{"completed": False, "resources": []}],
    [{"completed": False, "resources": [item()]}, {"completed": True, "resources": [item()]}],
    [{"completed": True, "resources": [{"id": "a"}]}],
])
def test_yandex_partial_listing_fails(pages):
    with pytest.raises(SyncError, match="incomplete"):
        client(pages).album()


def test_yandex_album_change_during_pagination_fails():
    source = client([{"completed": True, "resources": [item()]}])
    changed = deepcopy(bootstrap())
    changed[1]["modified"] = 43
    source.bootstrap.side_effect = [bootstrap(), changed]
    with pytest.raises(SyncError, match="incomplete"):
        source.album()


def test_yandex_empty_album_is_success_only_after_complete_listing():
    assert client([{"completed": True, "resources": []}]).album().photos == []


def test_yandex_bootstrap_json_is_parsed_without_javascript():
    source = YandexDisk("https://disk.yandex.ru/a/secret")
    state, root = bootstrap()
    source.request = Mock(return_value=SimpleNamespace(text=
        '<script type="application/json" id="store-prefetch">' + json.dumps(state) + '</script>'))
    assert source.bootstrap() == (state, root)
    source.request.return_value.text = '<html>captcha_smart</html>'
    with pytest.raises(SyncError, match="CAPTCHA"):
        source.bootstrap()


def test_yandex_public_page_api_encodes_json_and_rejects_error():
    source = YandexDisk("https://disk.yandex.ru/a/secret")
    response = Mock()
    response.status_code = 200
    response.json.return_value = {"completed": True, "resources": []}
    source.request = Mock(return_value=response)
    source.page("hash", "sk", "cursor")
    call = source.request.call_args
    assert call.args == ("POST", "https://disk.yandex.ru/public/api/fetch-album-list")
    assert json.loads(unquote(call.kwargs["data"])) == {"hash": "hash", "sk": "sk", "lastItemId": "cursor"}
    for payload in [{"error": True}, {"type": "captcha"}, {"completed": True}, {"resources": [], "completed": "true"}]:
        response.json.return_value = payload
        with pytest.raises((SyncError, ValueError)):
            source.page("hash", "sk", None)


def test_yandex_repeated_cursor_fails_even_if_ids_differ():
    a, b = item("a"), item("b")
    b["albumItemId"] = a["albumItemId"]
    source = client([{"completed": False, "resources": [a]}, {"completed": False, "resources": [b]}])
    with pytest.raises(SyncError, match="incomplete"):
        source.album()


@pytest.mark.parametrize("video", [False, True])
def test_yandex_signed_preview_url_is_not_cache_revision(video):
    first, second = item(video=video), item(video=video)
    second["meta"]["xxxlPreview"] += "?new-signature=secret"
    a = client([{"completed": True, "resources": [first]}]).album()
    b = client([{"completed": True, "resources": [second]}]).album()
    assert a.photos == b.photos
    second["meta"]["file_id"] = "changed-file"
    assert a.photos != client([{"completed": True, "resources": [second]}]).album().photos


def test_yandex_network_error_does_not_expose_link():
    source = YandexDisk("https://disk.yandex.ru/a/secret")
    source.session.request = Mock(side_effect=requests.ConnectionError(source.url))
    with pytest.raises(SyncError) as error:
        source.album()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("url", ["https://disk.yandex.ru/a/abc", "https://disk.yandex.com/a/abc/"])
def test_yandex_album_url_validation(url):
    validate_url("yandex_disk", url)


@pytest.mark.parametrize("url", ["https://disk.yandex.ru/d/file", "https://disk.yandex.ru/a/", "https://example.com/a/abc"])
def test_yandex_wrong_url_rejected(url):
    with pytest.raises(SyncError):
        validate_url("yandex_disk", url)
