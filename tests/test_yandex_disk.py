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


def item(name="a", *, video=False):
    return {"id": name, "name": name + ".jpg", "type": "file", "modified": 1, "albumItemId": "cursor-" + name,
            "virus": False, "meta": {"mediatype": "video" if video else "image", "drweb": 1,
                                     "size": 100, "file_id": "file-" + name, "mimetype": "image/jpeg",
                                     "xxxlPreview": "https://downloader.disk.yandex.ru/preview/private-" + name}}


def bootstrap():
    root = {"id": "album", "name": "Test", "modified": 42, "hash": "secret-hash", "type": "album"}
    return {"rootResourceId": "album", "resources": {"album": root}, "environment": {"sk": "secret-sk"}}, root


def client(pages):
    source = YandexDisk("https://disk.yandex.ru/a/secret")
    source.bootstrap = Mock(side_effect=[bootstrap(), bootstrap()])
    source.page = Mock(side_effect=pages)
    return source


def test_yandex_pagination_and_video_skip():
    source = client([{"completed": False, "resources": [item("a")]},
                     {"completed": True, "resources": [item("b"), item("v", video=True)]}])
    album = source.album()
    assert [a.id for a in album.photos] == ["a", "b"] and album.skipped == 1
    assert source.page.call_args_list[0].args == ("secret-hash", "secret-sk", None)
    assert source.page.call_args_list[1].args == ("secret-hash", "secret-sk", "cursor-a")
    assert set(source.images) == {"a", "b"}


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


def test_yandex_signed_preview_url_is_not_cache_revision():
    first, second = item(), item()
    second["meta"]["xxxlPreview"] += "?new-signature=secret"
    a = client([{"completed": True, "resources": [first]}]).album()
    b = client([{"completed": True, "resources": [second]}]).album()
    assert a.photos == b.photos


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
