# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

from dataclasses import replace
from types import SimpleNamespace
import json
from unittest.mock import Mock

import pytest
import requests

from timesframesync.cache import Cache
from timesframesync.config import Config, SyncError
from timesframesync.multi_sync import MultiSynchronizer
from timesframesync.providers.google_photos import GooglePhotos
from timesframesync.providers.icloud import ICloud
from timesframesync.source import Album, Asset
from timesframesync.sources_config import load_sources
from timesframesync.sync import Synchronizer
from test_sync import FakeFrame, jpeg


def google_item(asset_id, *, video=False):
    return [asset_id, [f"https://lh3.googleusercontent.com/{asset_id}", 800, 1200],
            100, None, None, 200, None, None, None, {"76647426": []} if video else {}]


def google_data(items, count, token=""):
    info = [None] * 22
    info[0], info[1], info[19], info[21] = "remote-album", "Test", "private-key", count
    return [None, items, token, info]


def google_client(data):
    client = GooglePhotos("https://photos.app.goo.gl/private")
    client.request = Mock(return_value=SimpleNamespace(
        url="https://photos.google.com/share/private",
        text="AF_initDataCallback({key: 'ds:1', data:" + json.dumps(data) + "});"))
    return client


def test_google_paginates_counts_video_and_stable_ids():
    client = google_client(google_data([google_item("a")], 3, "page2"))
    client.next_page = Mock(return_value=google_data([google_item("b", video=True), google_item("c")], 3))
    album = client.album()
    assert [(p.id, p.kind) for p in album.photos] == [("a", "photo"), ("b", "video"), ("c", "photo")]
    assert album.photo_count == 2 and album.video_count == 1 and album.skipped == 0
    client.next_page.assert_called_once_with("remote-album", "private-key", "page2")
    assert set(client.images) == {"a", "b", "c"}
    assert client.videos == {"b": "https://lh3.googleusercontent.com/b=dv"}


@pytest.mark.parametrize("data", [google_data([], 1), google_data([google_item("a"), google_item("a")], 2),
                                  google_data(None, 1), [None], google_data([], -1)])
def test_google_rejects_partial_or_malformed_album(data):
    with pytest.raises(SyncError):
        google_client(data).album()


def test_google_rejects_repeated_pagination_token():
    client = google_client(google_data([google_item("a")], 3, "same"))
    client.next_page = Mock(return_value=google_data([google_item("b")], 3, "same"))
    with pytest.raises(SyncError, match="incomplete"):
        client.album()


def test_google_valid_empty_and_unshared_page():
    assert google_client(google_data(None, 0)).album().photos == []
    client = google_client(google_data([], 0))
    client.request.return_value.text = "<html>Sign in</html>"
    with pytest.raises(SyncError, match="missing"):
        client.album()


def test_google_batchexecute_decodes_framed_response():
    client = GooglePhotos("https://photos.app.goo.gl/private")
    data = google_data([google_item("a")], 1)
    client.request = Mock(return_value=SimpleNamespace(text=
        ")]}'\n\n1234\n" + json.dumps([["wrb.fr", "snAcKc", json.dumps(data), None]]) + "\n[[\"di\",42]]"))
    assert client.next_page("album", "secret", "page2") == data
    args = json.loads(client.request.call_args.kwargs["data"]["f.req"])
    assert json.loads(args[0][0][1]) == ["album", "page2", None, "secret"]


def ck_record(name, kind, **fields):
    return {"recordName": name, "recordType": kind, "fields": {k: {"value": v} for k, v in fields.items()}}


def resolved():
    return {"results": [{"databaseScope": "SHARED", "zoneID": {"zoneName": "zone", "ownerRecordName": "owner"},
                          "share": ck_record("share", "cloudkit.share", **{"cloudkit.title": "Test"}),
                          "anonymousPublicAccess": {"token": "private-token", "databasePartition": "https://p111-ckdatabasews.icloud.com"}}]}


def ck_count(count=2, token="v1"):
    return {"records": [ck_record("count", "IndexCountResult", itemCount=count)], "syncToken": token}


def ck_photo(name, *, video=False):
    return [ck_record("master-" + name, "CPLMaster", itemType="public.mpeg-4" if video else "public.heic",
                      resJPEGMedRes={"fileChecksum": "checksum-" + name,
                                     "downloadURL": "https://cvws-h2.icloud-content.com/${f}?secret=x"}),
            ck_record(name, "CPLAsset", masterRef={"recordName": "master-" + name}, recordModificationDate=123)]


def icloud_client(responses):
    client = ICloud("https://photos.icloud.com/shared/album/private")
    client.json = Mock(side_effect=responses)
    return client


def test_icloud_rank_pagination_and_video_skip():
    client = icloud_client([resolved(), ck_count(), {"records": ck_photo("a")},
                           {"records": ck_photo("b", video=True)}, ck_count()])
    album = client.album()
    assert album.name == "Test" and album.id == "zone:owner"
    assert [p.id for p in album.photos] == ["a"] and album.skipped == 1
    assert "${f}" not in client.images["a"]
    calls = client.json.call_args_list
    assert calls[2].kwargs["json"]["query"]["filterBy"][1]["fieldValue"]["value"] == 0
    assert calls[3].kwargs["json"]["query"]["filterBy"][1]["fieldValue"]["value"] == 1
    assert calls[2].kwargs["params"]["publicAccessAuthToken"] == "private-token"


@pytest.mark.parametrize("response", [ck_count(3), ck_count(2, "changed")])
def test_icloud_change_during_listing_fails(response):
    client = icloud_client([resolved(), ck_count(), {"records": ck_photo("a") + ck_photo("b")}, response])
    with pytest.raises(SyncError, match="incomplete"):
        client.album()


@pytest.mark.parametrize("records", [[], ck_photo("a") + ck_photo("a"), [ck_photo("a")[1]], [{"serverErrorCode": "ACCESS_DENIED"}]])
def test_icloud_incomplete_listing_cannot_be_empty_success(records):
    client = icloud_client([resolved(), ck_count(1), {"records": records}, ck_count(1)])
    with pytest.raises(SyncError):
        client.album()


def test_icloud_valid_empty_album():
    client = icloud_client([resolved(), ck_count(0), ck_count(0)])
    assert client.album().photos == []


@pytest.mark.parametrize("cls,url", [(GooglePhotos, "https://photos.app.goo.gl/private"),
                                     (ICloud, "https://photos.icloud.com/shared/album/private")])
def test_public_network_errors_hide_urls(cls, url):
    client = cls(url)
    client.session.request = Mock(side_effect=requests.ConnectionError(url))
    with pytest.raises(SyncError) as error:
        client.album()
    assert "private" not in str(error.value)


def test_source_configuration_multiple_same_service_and_secret_env(tmp_path, monkeypatch):
    path = tmp_path / "sources.toml"
    path.write_text('''[[sources]]
id = "one"
provider = "google_photos"
url_env = "TEST_PHOTOS_URL"
[[sources]]
id = "two"
provider = "google_photos"
url = "https://photos.app.goo.gl/secret-two"
target_album = "Family"
''')
    monkeypatch.setenv("TEST_PHOTOS_URL", "https://photos.app.goo.gl/secret-one")
    sources = load_sources(Config(sources_file=path))
    assert [s.id for s in sources] == ["one", "two"]
    assert [s.target_album for s in sources] == ["Photos", "Family"]
    assert "secret" not in repr(sources)


@pytest.mark.parametrize("content", ["", 'sources = []', 'bad = 1', '''[[sources]]
id = "../escape"
provider = "google_photos"
url = "https://photos.app.goo.gl/test"
''', '''[[sources]]
id = "one"
provider = "unknown"
url = "https://example.com"
'''])
def test_invalid_source_configuration(tmp_path, content):
    path = tmp_path / "sources.toml"
    path.write_text(content)
    with pytest.raises(SyncError):
        load_sources(Config(sources_file=path))


@pytest.fixture
def multi(tmp_path):
    path = tmp_path / "sources.toml"
    path.write_text('''[[sources]]
id = "one"
provider = "immich"
url = "https://immich.test/share/private"
[[sources]]
id = "two"
provider = "google_photos"
url = "https://photos.app.goo.gl/private"
''')
    sync = MultiSynchronizer(Config(host="127.0.0.1", data_dir=tmp_path / "data", sources_file=path, frame_album="immich"))
    sync.frame = FakeFrame()
    for name, color in [("one", "red"), ("two", "blue")]:
        sync.clients[name] = Mock()
        sync.clients[name].album.return_value = Album("album", name, [Asset("same-id", "v1", "a.jpg")])
        sync.clients[name].preview.return_value = jpeg(color)
    return sync


def cycle(sync):
    sync.refresh()
    result = sync.sync_album(play=False)
    assert not result["errors"]
    return result["albums"][0]


def test_multi_unions_all_sources_and_prunes_only_missing_owned(multi):
    assert cycle(multi)["uploaded"] == 2
    assert multi.frame.db.members[123] == {1, 10, 11}
    assert cycle(multi)["uploaded"] == 0
    multi.clients["one"].album.return_value = Album("album", "one", [])
    assert cycle(multi)["removed"] == 1
    assert multi.frame.db.members[123] == {1, 11}
    assert len(multi.caches["two"].read()["photos"]) == 1


def test_multi_same_bytes_in_different_sources_remain_until_both_removed(multi):
    multi.clients["two"].preview.return_value = jpeg("red")
    assert cycle(multi)["uploaded"] == 1
    multi.clients["one"].album.return_value = Album("album", "one", [])
    assert cycle(multi)["removed"] == 0
    multi.clients["two"].album.return_value = Album("album", "two", [])
    assert cycle(multi)["removed"] == 1
    assert multi.frame.db.members[123] == {1}


def test_multi_failure_keeps_shared_target_unchanged_and_recovers(multi):
    cycle(multi)
    multi.clients["one"].album.return_value = Album("album", "one", [])
    multi.clients["two"].album.side_effect = SyncError("unavailable")
    multi.refresh()
    result = multi.sync_album()
    assert result["errors"] == {"two": "unavailable"} and result["albums"] == []
    assert multi.frame.db.members[123] == {1, 10, 11}
    with pytest.raises(SyncError, match="Refresh"):
        multi.sync_album()
    multi.clients["two"].album.side_effect = None
    assert cycle(multi)["removed"] == 1


def test_multi_other_target_continues_after_source_failure(multi):
    second = replace(multi.specs[1], target_album="other")
    multi.specs[1] = second
    multi.groups = {"immich": [multi.specs[0]], "other": [second]}
    multi.frame.db.albums.append({"id": 234, "type": 0, "name": "other"})
    multi.frame.db.members[234] = set()
    multi.clients["one"].album.side_effect = SyncError("unavailable")
    multi.refresh()
    result = multi.sync_album()
    assert result["errors"] == {"one": "unavailable"}
    assert result["albums"][0]["album"] == "other"
    assert multi.frame.db.members[123] == {1}
    assert multi.frame.db.members[234] == {10}
    assert multi.frame.played == [234]


def test_multi_migration_preserves_legacy_ownership(multi):
    legacy = Synchronizer(replace(multi.config, sources_file=None, share_url="https://immich.test/share/private"))
    legacy.frame = multi.frame
    legacy.cache.sync(multi.clients["one"], Album("album", "one", [Asset("old", "v1", "old.jpg")]))
    legacy.sync_album(play=False)
    multi.clients["one"].album.return_value = Album("album", "one", [])
    result = cycle(multi)
    assert result["removed"] == 1 and result["uploaded"] == 1
    assert multi.frame.db.members[123] == {1, 11}


def test_multi_append_ownership_survives_restart(multi):
    cycle(multi)
    multi.config = replace(multi.config, sync_mode="append")
    for client in multi.clients.values():
        client.album.return_value = Album("album", "empty", [])
    assert cycle(multi)["retained"] == 2
    restarted = MultiSynchronizer(replace(multi.config, sync_mode="mirror"))
    restarted.frame, restarted.clients = multi.frame, multi.clients
    assert cycle(restarted)["removed"] == 2
    assert restarted.frame.db.members[123] == {1}


def test_cache_remote_ids_cannot_escape_directory_and_album_change_invalidates_cache(tmp_path):
    cache = Cache(Config(data_dir=tmp_path))
    client = Mock()
    client.preview.return_value = jpeg()
    asset = Asset("../../escape", "same-version", "a.jpg")
    assert cache.sync(client, Album("album-one", "one", [asset]))["downloaded"] == 1
    assert cache.sync(client, Album("album-two", "two", [asset]))["downloaded"] == 1
    assert ".." not in cache.read()["photos"][0]["file"]
    assert not (tmp_path.parent / "escape").exists()


def test_two_targets_share_storage_but_keep_independent_membership(multi):
    second = replace(multi.specs[1], target_album="other")
    multi.specs[1] = second
    multi.groups = {"immich": [multi.specs[0]], "other": [second]}
    multi.frame.db.albums.append({"id": 234, "type": 0, "name": "other"})
    multi.frame.db.members[234] = set()
    multi.clients["two"].preview.return_value = jpeg("red")
    multi.refresh()
    result = multi.sync_album()
    assert not result["errors"]
    assert multi.frame.uploads == 1
    assert multi.frame.db.members == {123: {1, 10}, 234: {10}}
    assert multi.frame.played == [123]
    multi.clients["one"].album.return_value = Album("album", "empty", [])
    multi.refresh()
    result = multi.sync_album(play=False)
    assert not result["errors"]
    assert multi.frame.db.members == {123: {1}, 234: {10}}


def test_failed_preview_keeps_ownership_and_target_unchanged(multi):
    cycle(multi)
    state_path = next((multi.config.data_dir / "targets").glob("*/device-state.json"))
    previous = state_path.read_bytes()
    multi.clients["one"].album.return_value = Album("album", "empty", [])
    multi.clients["two"].album.return_value = Album("album", "two", [Asset("new", "v2", "b.jpg")])
    multi.clients["two"].preview.side_effect = SyncError("expired preview URL")
    multi.refresh()
    result = multi.sync_album()
    assert "target:immich" in result["errors"]
    assert state_path.read_bytes() == previous
    assert multi.frame.db.members[123] == {1, 10, 11}


def test_sources_file_allows_config_without_immich_url(tmp_path, monkeypatch):
    monkeypatch.setattr("timesframesync.config.load_dotenv", Mock())
    monkeypatch.delenv("IMMICH_SHARE_URL", raising=False)
    monkeypatch.setenv("DIVOOM_HOST", "127.0.0.1")
    monkeypatch.setenv("SOURCES_FILE", "environment.toml")
    config = Config.load(sources_file=str(tmp_path / "explicit.toml"))
    assert config.sources_file == tmp_path / "explicit.toml"
    assert config.share_url == ""


def test_source_ids_cannot_collide_on_case_insensitive_filesystems(tmp_path):
    path = tmp_path / "sources.toml"
    path.write_text(''.join(f'''[[sources]]
id = "{name}"
provider = "google_photos"
url = "https://photos.app.goo.gl/test"
''' for name in ["One", "one"]))
    with pytest.raises(SyncError, match="Duplicate"):
        load_sources(Config(sources_file=path))


def test_cli_returns_failure_for_partial_sync(monkeypatch):
    from timesframesync.cli import main
    from pathlib import Path
    config = Config(sources_file=Path("sources.toml"))
    monkeypatch.setattr("timesframesync.cli.Config.load", Mock(return_value=config))
    sync = Mock()
    sync.sync_album.return_value = {"photos": 2, "errors": {"google": "unavailable"}}
    monkeypatch.setattr("timesframesync.cli.MultiSynchronizer", Mock(return_value=sync))
    monkeypatch.setattr("sys.argv", ["tfs", "sync"])
    from contextlib import nullcontext
    monkeypatch.setattr("timesframesync.cli.locked", lambda _: nullcontext())
    assert main() == 1
    sync.refresh.assert_called_once()
    sync.sync_album.assert_called_once()
