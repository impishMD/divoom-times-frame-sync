# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import threading
from unittest.mock import Mock

from PIL import Image
import pytest
import requests

from timesframesync.cache import Cache, locked, render_photo
from timesframesync.cli import parser
from timesframesync.config import Config, SyncError, parse_share_url
from timesframesync.frame import Frame, Inventory, device_photo, multipart, read_inventory
from timesframesync.immich import Album, Asset, Immich
from timesframesync.sync import Synchronizer

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
V = "33333333-3333-4333-8333-333333333333"


def jpeg(color="red"):
    result = BytesIO()
    Image.new("RGB", (160, 120), color).save(result, "JPEG")
    return result.getvalue()


@pytest.fixture
def config(tmp_path):
    return Config("https://immich.test/share/private-key", password="private-password", host="127.0.0.1",
                  token="123456", data_dir=tmp_path, frame_album="immich")


def test_shared_url_subpath_and_secret_repr(config):
    base, params = parse_share_url("https://example.com/photos/share/abc_def?ignored=1")
    assert base == "https://example.com/photos/api"
    assert params == {"key": "abc_def"}
    assert "private-key" not in repr(config)
    assert "private-password" not in repr(config)
    assert "123456" not in repr(config)


@pytest.mark.parametrize("url", ["", "ftp://a/share/key", "https://a/share/", "https://a/albums/key"])
def test_invalid_urls(url):
    with pytest.raises(SyncError):
        parse_share_url(url)


def test_v3_cookie_login_all_buckets_and_videos(config):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, cookie=False):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if cookie:
                self.send_header("Set-Cookie", "immich_shared_link_token=test; Path=/")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert payload == {"password": config.password}
            self.reply({"type": "ALBUM", "album": {"id": A, "albumName": "Test", "assetCount": 3}}, True)

        def do_GET(self):
            from urllib.parse import urlsplit, parse_qs
            path = urlsplit(self.path)
            query = parse_qs(path.query)
            assert query["key"] == ["private-key"]
            assert self.headers.get("Cookie") == "immich_shared_link_token=test"
            seen.append(path.path)
            if path.path.endswith("/buckets"):
                assert query["withStacked"] == ["false"]
                assert query["order"] == ["asc"]
                self.reply([{"timeBucket": "2026-01-01", "count": 2}, {"timeBucket": "2026-02-01", "count": 1}])
            elif path.path.endswith("/bucket"):
                self.reply({"id": [A, V], "isImage": [True, False]} if query["timeBucket"] == ["2026-01-01"]
                           else {"id": [B], "isImage": [True]})
            else:
                self.reply({"type": "VIDEO" if path.path.endswith(V) else "IMAGE", "updatedAt": "rev1", "originalFileName": "same.jpg"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Immich(config)
        client.base_url = f"http://127.0.0.1:{server.server_port}/api"
        client.session.trust_env = False
        album = client.album()
        assert [p.id for p in album.photos] == [A, V, B]
        assert [p.kind for p in album.photos] == ["photo", "video", "photo"]
        assert album.photo_count == 2 and album.video_count == 1 and album.skipped == 0
        assert seen.count("/api/timeline/bucket") == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_incomplete_listing_fails_closed(config):
    client = Immich(config)
    client._json = Mock(side_effect=[
        {"type": "ALBUM", "album": {"id": A, "albumName": "test", "assetCount": 2}},
        [{"timeBucket": "2026-01-01", "count": 2}],
        {"id": [A], "isImage": [True]},
    ])
    with pytest.raises(SyncError, match="changed"):
        client.album()


def test_network_errors_do_not_leak_share_url(config):
    client = Immich(config)
    client.session.request = Mock(side_effect=requests.ConnectionError(config.share_url))
    with pytest.raises(SyncError) as error:
        client.album()
    assert "private-key" not in str(error.value)


def test_cache_is_idempotent_and_removes_from_playlist(config):
    client = Mock()
    client.preview.return_value = jpeg()
    cache = Cache(config)
    album = Album(A, "test", [Asset(A, "v1", "same.jpg"), Asset(B, "v1", "same.jpg")], 0)
    assert cache.sync(client, album)["downloaded"] == 2
    assert cache.sync(client, album)["downloaded"] == 0
    assert client.preview.call_count == 2
    assert len({p["file"] for p in cache.read()["photos"]}) == 2
    smaller = Album(A, "test", [Asset(B, "v2", "same.jpg")], 0)
    result = cache.sync(client, smaller)
    assert result["removed"] == 1
    assert result["downloaded"] == 1
    assert [p["id"] for p in cache.read()["photos"]] == [B]


def test_failed_download_does_not_replace_complete_manifest(config):
    cache = Cache(config)
    client = Mock()
    client.preview.return_value = jpeg()
    cache.sync(client, Album(A, "test", [Asset(A, "v1", "a.jpg")], 0))
    before = cache.path.read_bytes()
    client.preview.side_effect = [jpeg("blue"), SyncError("timeout")]
    with pytest.raises(SyncError):
        cache.sync(client, Album(A, "test", [Asset(A, "v2", "a.jpg"), Asset(B, "v1", "b.jpg")], 0))
    assert cache.path.read_bytes() == before


def test_lock_prevents_concurrent_sync(config):
    with locked(config.data_dir):
        with pytest.raises(SyncError, match="Another sync"):
            with locked(config.data_dir):
                pass


def test_render_preserves_whole_photo():
    im = Image.open(BytesIO(render_photo(jpeg(), "contain")))
    assert im.size == (800, 1280)
    assert im.getpixel((0, 0)) == (0, 0, 0)
    assert im.getpixel((400, 640))[0] > 240


def test_multipart_has_order_and_lengths():
    body, content_type = multipart({"Command": "Channel/GetConfig", "ReturnCode": 0}, "photo.jpg", b"12345")
    boundary = content_type.split("boundary=")[1].encode()
    parts = body.split(b"--" + boundary)
    assert len(parts) == 4
    for part in parts[1:3]:
        headers, content = part.lstrip(b"\r\n").split(b"\r\n\r\n", 1)
        length = int(next(row.split(b": ")[1] for row in headers.split(b"\r\n") if row.startswith(b"Content-Length:")))
        assert len(content[:-2]) == length
    assert b'filename="cmd.json"' in parts[1]
    assert b'filename="photo.jpg"' in parts[2]


def test_filename_limits_prevent_firmware_abort():
    for filename in ["x" * 33, "../photo.webp", "photo;rm.webp", "photo\n.webp", ""]:
        with pytest.raises(SyncError, match="filenames"):
            multipart({}, filename, b"image")
    name, content = device_photo(jpeg())
    assert len(name) == 32
    assert Image.open(BytesIO(content)).format == "WEBP"
    assert device_photo(jpeg()) == (name, content)


def test_json_only_native_command_uses_upload(config):
    frame = Frame(config)
    frame._post = Mock(return_value={"ReturnCode": 0})
    frame.native_command("Photo/RemovePhotoFromAlbum", ClockId=123, PhotoList=[5])
    path, body, mime = frame._post.call_args.args
    assert path == "/upload"
    assert b'"PhotoList":[5]' in body
    assert body.count(b"Content-Disposition") == 1
    assert body.endswith(b"--\r\n")


def test_inventory_reads_copy_and_requires_unique_named_album():
    import sqlite3
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE album_head(id INTEGER, type INTEGER, name TEXT);
            CREATE TABLE pic_info(id INTEGER, path_name TEXT);
            CREATE TABLE album_pic(album_id INTEGER, pic_id INTEGER);
            INSERT INTO album_head VALUES(123,0,'immich');
            INSERT INTO album_head VALUES(456,1,'All Photos');
            INSERT INTO pic_info VALUES(1,'/userdata/app_pic/20269/im-one.webp');
            INSERT INTO album_pic VALUES(123,1);
        """)
        snapshot = db.serialize()
    inventory = read_inventory(snapshot)
    assert inventory.album("immich")["id"] == 123
    assert inventory.members[123] == {1}
    assert inventory.find_file("im-one.webp")["id"] == 1
    with pytest.raises(SyncError, match="ordinary"):
        inventory.album("All Photos")
    inventory.albums.append({"id": 789, "type": 0, "name": "immich"})
    with pytest.raises(SyncError, match="found 2"):
        inventory.album("immich")
    with pytest.raises(SyncError):
        read_inventory(b"invalid")


def test_success_response_without_membership_is_not_success(config, monkeypatch):
    frame = Frame(config)
    frame.inventory = Mock(return_value=Inventory([], {}, {}))
    monkeypatch.setattr("timesframesync.frame.time.monotonic", Mock(side_effect=[0, 16]))
    with pytest.raises(SyncError, match="membership did not change"):
        frame.wait_members(123, present=[9])


class FakeFrame:
    def __init__(self):
        self.db = Inventory([{"id": 123, "type": 0, "name": "immich"}],
                            {1: {"id": 1, "path": "/userdata/app_pic/manual.webp"}}, {123: {1}})
        self.files = {"/userdata/app_pic/manual.webp": b"manual"}
        self.uploads = 0
        self.played = []
        self.fail_upload = False
        self.next_id = 10

    def inventory(self):
        import copy
        return copy.deepcopy(self.db)

    def info(self):
        return {"clock": {"DeviceId": 999, "ClockId": 456}}

    def import_photo(self, album_id, filename, content, user_id, *, verify=True):
        if self.fail_upload:
            raise SyncError("upload failed")
        self.uploads += 1
        pic_id = self.next_id
        self.next_id += 1
        record = {"id": pic_id, "path": "/userdata/app_pic/20269/" + filename}
        self.db.photos[pic_id] = record
        self.files[record["path"]] = content
        self.db.members.setdefault(album_id, set()).add(pic_id)
        return record

    def fetch_file(self, path):
        return self.files.get(path)

    def add_existing(self, album_id, pic_id):
        self.db.members.setdefault(album_id, set()).add(pic_id)

    def remove_from_album(self, album_id, pic_ids):
        self.db.members[album_id] -= pic_ids

    def wait_members(self, album_id, *, present=(), absent=()):
        assert set(present) <= self.db.members[album_id]
        assert not set(absent) & self.db.members[album_id]

    def play_album(self, album_id):
        self.played.append(album_id)


@pytest.fixture
def prepared(config):
    sync = Synchronizer(config)
    sync.frame = FakeFrame()
    client = Mock()
    client.preview.side_effect = [jpeg("red"), jpeg("blue")]
    sync.cache.sync(client, Album(A, "test", [Asset(A, "v1", "a"), Asset(B, "v1", "b")], 0))
    return sync


def test_native_sync_deduplicates_and_only_prunes_managed_entries(prepared):
    sync = prepared
    assert sync.sync_album()["uploaded"] == 2
    assert sync.frame.db.members[123] == {1, 10, 11}
    assert sync.sync_album(play=False)["uploaded"] == 0
    assert sync.frame.uploads == 2
    assert sync.frame.played == [123]
    assert sync.device_state()["previous_clock_id"] == 456
    manifest = sync.cache.read()
    manifest["photos"] = manifest["photos"][1:]
    sync.cache.path.write_text(json.dumps(manifest))
    assert sync.sync_album()["removed"] == 1
    assert sync.frame.db.members[123] == {1, 11}
    assert 10 in sync.frame.db.photos  # Removed from album, not globally deleted.


def test_partial_failure_retains_previous_members_and_can_resume(prepared):
    sync = prepared
    sync.sync_album()
    original_members = set(sync.frame.db.members[123])
    client = Mock()
    client.preview.return_value = jpeg("green")
    sync.cache.sync(client, Album(A, "test", [Asset(A, "v2", "a")], 0))
    sync.frame.fail_upload = True
    with pytest.raises(SyncError, match="upload failed"):
        sync.sync_album()
    assert sync.frame.db.members[123] == original_members
    sync.frame.fail_upload = False
    result = sync.sync_album()
    assert result["uploaded"] == 1 and result["removed"] == 2
    assert sync.frame.db.members[123] == {1, 12}


def test_recover_missing_state_without_reupload_and_readd_missing_membership(prepared):
    sync = prepared
    sync.sync_album()
    (sync.config.data_dir / "device-state.json").unlink()
    sync.frame.db.members[123].remove(10)
    assert sync.sync_album()["uploaded"] == 0
    assert sync.frame.db.members[123] == {1, 10, 11}
    assert sync.frame.uploads == 2


def test_empty_album_and_reused_id_do_not_remove_foreign_photo(prepared):
    sync = prepared
    sync.sync_album()
    sync.frame.db.photos[10] = {"id": 10, "path": "/userdata/app_pic/reused.webp"}
    client = Mock()
    sync.cache.sync(client, Album(A, "test", [], 0))
    result = sync.sync_album()
    assert result["removed"] == 1
    assert sync.frame.db.members[123] == {1, 10}
    assert sync.frame.played == [123]


def test_silent_corruption_is_left_for_explicit_repair(prepared):
    sync = prepared
    sync.sync_album()
    sync.frame.files[sync.frame.db.photos[10]["path"]] = b"corrupt"
    assert sync.sync_album()["skipped"] == 2
    assert sync.frame.db.members[123] == {1, 10, 11}


def test_append_retains_missing_photos_and_mirror_prunes_them_after_restart(prepared):
    sync = prepared
    sync.config = replace(sync.config, sync_mode="append")
    sync.sync_album()
    manifest = sync.cache.read()
    manifest["photos"] = manifest["photos"][1:]
    sync.cache.path.write_text(json.dumps(manifest))
    result = sync.sync_album()
    assert result["sync_mode"] == "append"
    assert result["removed"] == 0 and result["retained"] == 1
    assert sync.frame.db.members[123] == {1, 10, 11}
    assert set(sync.device_state()["managed_photos"]) == {"10", "11"}

    restarted = Synchronizer(replace(sync.config, sync_mode="mirror"))
    restarted.frame = sync.frame
    result = restarted.sync_album()
    assert result["removed"] == 1 and result["retained"] == 0
    assert sync.frame.db.members[123] == {1, 11}
    assert set(restarted.device_state()["managed_photos"]) == {"11"}


def test_append_preserves_empty_source_and_changed_versions(prepared):
    sync = prepared
    sync.config = replace(sync.config, sync_mode="append")
    sync.sync_album()
    client = Mock()
    client.preview.return_value = jpeg("green")
    sync.cache.sync(client, Album(A, "test", [Asset(A, "v2", "a")], 0))
    assert sync.sync_album()["retained"] == 2
    assert sync.frame.db.members[123] == {1, 10, 11, 12}
    sync.cache.sync(client, Album(A, "test", [], 0))
    assert sync.sync_album()["retained"] == 3
    assert set(sync.device_state()["managed_photos"]) == {"10", "11", "12"}
    sync.config = replace(sync.config, sync_mode="mirror")
    assert sync.sync_album()["removed"] == 3
    assert sync.frame.db.members[123] == {1}


def test_mirror_preserves_other_albums_and_recovers_failed_removal(prepared):
    sync = prepared
    sync.sync_album()
    sync.frame.db.members[456] = {10, 11}
    sync.cache.sync(Mock(), Album(A, "test", [], 0))
    remove = sync.frame.remove_from_album
    sync.frame.remove_from_album = Mock(side_effect=SyncError("removal timeout"))
    with pytest.raises(SyncError, match="removal timeout"):
        sync.sync_album()
    assert set(sync.device_state()["managed_photos"]) == {"10", "11"}
    sync.frame.remove_from_album = remove
    assert sync.sync_album()["removed"] == 2
    assert sync.frame.db.members == {123: {1}, 456: {10, 11}}


def test_mirror_keeps_shared_bytes_when_one_identical_asset_disappears(config):
    sync = Synchronizer(config)
    sync.frame = FakeFrame()
    client = Mock()
    client.preview.return_value = jpeg()
    sync.cache.sync(client, Album(A, "test", [Asset(A, "v1", "a"), Asset(B, "v1", "b")], 0))
    assert sync.sync_album()["uploaded"] == 1
    sync.cache.sync(client, Album(A, "test", [Asset(B, "v1", "b")], 0))
    assert sync.sync_album()["removed"] == 0
    assert sync.frame.db.members[123] == {1, 10}


def test_mode_environment_default_and_cli_override(config, monkeypatch):
    monkeypatch.setattr("timesframesync.config.load_dotenv", Mock())
    monkeypatch.setenv("IMMICH_SHARE_URL", config.share_url)
    monkeypatch.setenv("DIVOOM_HOST", config.host)
    monkeypatch.delenv("SYNC_MODE", raising=False)
    assert Config.load().sync_mode == "mirror"
    monkeypatch.setenv("SYNC_MODE", "append")
    assert Config.load().sync_mode == "append"
    assert Config.load(sync_mode="mirror").sync_mode == "mirror"
    monkeypatch.setenv("SYNC_MODE", "unknown")
    with pytest.raises(SyncError, match="SYNC_MODE"):
        Config.load()
    assert Config.load(sync_mode="append").sync_mode == "append"


@pytest.mark.parametrize("command", ["sync", "run"])
@pytest.mark.parametrize("mode", ["mirror", "append"])
def test_cli_accepts_modes_for_both_sync_commands(command, mode):
    assert parser().parse_args([command, "--sync-mode", mode]).sync_mode == mode
    assert parser().parse_args([command]).sync_mode is None


def test_invalid_mode_never_changes_frame(prepared):
    prepared.config = replace(prepared.config, sync_mode="unknown")
    prepared.frame = Mock()
    with pytest.raises(SyncError, match="SYNC_MODE"):
        prepared.sync_album()
    assert not prepared.frame.mock_calls


def test_import_acknowledgment_without_database_entry_is_failure(config, monkeypatch):
    frame = Frame(config)
    frame._post = Mock(return_value={"ReturnCode": 0})
    frame.inventory = Mock(return_value=Inventory([], {}, {}))
    monkeypatch.setattr("timesframesync.frame.time.monotonic", Mock(side_effect=[0, 16]))
    with pytest.raises(SyncError, match="absent from its native database"):
        frame.import_photo(123, "im-test.webp", b"image")


def test_import_checks_bytes_even_when_membership_exists(config):
    frame = Frame(config)
    frame._post = Mock(return_value={"ReturnCode": 0})
    frame.inventory = Mock(return_value=Inventory([], {10: {"id": 10, "path": "/userdata/app_pic/20269/im-test.webp"}}, {123: {10}}))
    frame.fetch_file = Mock(return_value=b"wrong bytes")
    with pytest.raises(SyncError, match="read-back verification"):
        frame.import_photo(123, "im-test.webp", b"expected")
