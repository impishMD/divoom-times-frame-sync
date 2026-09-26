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


BASE = OneDrive.api_origin + "/_api/v2.1/drives/drive/albums/album"


def metadata(count=2):
    return {"id": "album", "name": "Photos", "eTag": "revision-1", "lastModifiedDateTime": "2026-01-01",
            "folder": {"childCount": count}, "mediaAlbum": {"albumItemCount": count}}


def item(asset_id="a", *, video=False):
    result = {"id": asset_id, "name": asset_id + ".jpg", "image": {"width": 10, "height": 20},
              "file": {"mimeType": "image/jpeg"}, "cTag": "content-1",
              "lastModifiedDateTime": "2026-01-01", "size": 100}
    if video:
        result["video"] = {}
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


def test_onedrive_pagination_and_video_skip():
    next_url = BASE + "/children?$skiptoken=next"
    source = client([{"value": [item()], "@odata.nextLink": next_url},
                     {"value": [item("v", video=True)]}])
    album = source.album()
    assert album.id == "drive:album" and album.name == "Photos"
    assert [p.id for p in album.photos] == ["a"] and album.skipped == 1
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


def test_onedrive_content_change_updates_revision_but_signed_url_does_not():
    before = item()
    after = dict(before, cTag="content-2")
    original = client([{"value": [before]}], count=1).album().photos[0]
    assert original.revision != client([{"value": [after]}], count=1).album().photos[0].revision
    after = dict(before, **{"@content.downloadUrl": "https://example.com/new-signature"})
    assert original == client([{"value": [after]}], count=1).album().photos[0]


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
