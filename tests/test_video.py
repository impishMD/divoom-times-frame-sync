# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

from dataclasses import replace
from email.parser import BytesParser
from email.policy import default
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import shutil
import sqlite3
import threading
from unittest.mock import Mock

from PIL import Image
import pytest
import requests

from timesframesync.cache import Cache
from timesframesync.config import Config, SyncError
from timesframesync.frame import FileMultipart, Frame, Inventory, read_inventory
from timesframesync.immich import Immich
from timesframesync.source import Album, Asset
from timesframesync.sync import Synchronizer
from timesframesync.video import file_digest, make_cover, probe, run_media, transcode
from test_sync import FakeFrame, jpeg
from test_sources import multi, cycle


class VideoFrame(FakeFrame):
    def import_video(self, album_id, filename, content, preview, user_id):
        record = self.import_photo(album_id, filename, content.read_bytes(), user_id)
        self.db.video_ids.add(record["id"])
        self.files[str(Path(record["path"]).with_suffix(".webp"))] = preview.read_bytes()
        return record

    def file_digest(self, path):
        content = self.fetch_file(path)
        return hashlib.sha256(content).hexdigest() if content is not None else None


@pytest.fixture
def video_sync(tmp_path, monkeypatch):
    sync = Synchronizer(Config(data_dir=tmp_path, frame_album="immich"))
    sync.frame = VideoFrame()
    sync.immich = Mock()
    sync.immich.album.return_value = Album("album", "Videos", [Asset("clip", "1", "clip.mov", "video")])
    sync.immich.download_video.side_effect = lambda asset, path: path.write_bytes(b"source-video")
    monkeypatch.setattr("timesframesync.cache.require_ffmpeg", lambda: None)
    def convert(source, destination, fit):
        destination.write_bytes(b"MP4:" + source.read_bytes())
        return {"duration": 3.0, "width": 800, "height": 1280}
    monkeypatch.setattr("timesframesync.cache.transcode", convert)
    monkeypatch.setattr("timesframesync.cache.make_cover", lambda path: b"WebP-cover")
    return sync


def test_video_only_upload_cleanup_restart_and_lost_metadata(video_sync):
    sync = video_sync
    result = sync.refresh()
    assert result["photos"] == 0 and result["videos"] == 1 and result["downloaded"] == 0
    result = sync.sync_album()
    assert result["items"] == result["videos"] == result["uploaded"] == result["downloaded"] == 1
    assert result["photos"] == 0 and sync.frame.played == [123]
    assert not list(sync.config.data_dir.glob("videos/*"))
    manifest = sync.cache.read()
    assert manifest["photos"][0]["preview_sha256"]
    sync.refresh()
    assert sync.sync_album()["uploaded"] == 0
    assert sync.immich.download_video.call_count == 1
    sync.cache.path.unlink()
    (sync.config.data_dir / "device-state.json").unlink()
    sync.refresh()
    assert sync.sync_album()["uploaded"] == 0
    assert sync.immich.download_video.call_count == 2
    assert sync.frame.db.members[123] == {1, 10}
    assert not list(sync.config.data_dir.glob("videos/*"))


def test_video_upload_failure_retains_prepared_pair_then_retries(video_sync):
    sync = video_sync
    sync.refresh()
    sync.frame.fail_upload = True
    with pytest.raises(SyncError, match="upload failed"):
        sync.sync_album()
    assert sorted(p.suffix for p in sync.config.data_dir.glob("videos/*")) == [".mp4", ".webp"]
    sync.frame.fail_upload = False
    sync.refresh()
    result = sync.sync_album()
    assert result["uploaded"] == 1 and result["downloaded"] == 0
    assert sync.immich.download_video.call_count == 1
    assert not list(sync.config.data_dir.glob("videos/*"))


def test_video_transcode_failure_retains_input_but_not_partial_output(video_sync, monkeypatch):
    sync = video_sync
    sync.refresh()
    with monkeypatch.context() as patch:
        def fail(source, output, fit):
            output.write_bytes(b"partial")
            raise SyncError("codec failed")
        patch.setattr("timesframesync.cache.transcode", fail)
        with pytest.raises(SyncError, match="codec failed"):
            sync.sync_album()
    assert [p.suffix for p in sync.config.data_dir.glob("videos/*")] == [".source"]
    assert sync.sync_album()["downloaded"] == 0
    assert sync.immich.download_video.call_count == 1
    assert not list(sync.config.data_dir.glob("videos/*"))


def test_partial_video_download_is_discarded(video_sync):
    sync = video_sync
    def interrupt(asset, path):
        path.write_bytes(b"incomplete")
        raise SyncError("interrupted")
    sync.immich.download_video.side_effect = interrupt
    sync.refresh()
    with pytest.raises(SyncError, match="interrupted"):
        sync.sync_album()
    assert not list(sync.config.data_dir.glob("videos/*"))
    assert sync.frame.uploads == 0


@pytest.mark.parametrize("damage", ["movie", "cover", "flag"])
def test_video_readback_failure_keeps_files_and_blocks_pruning(video_sync, damage):
    sync = video_sync
    sync.immich.preview.return_value = jpeg()
    sync.immich.album.return_value = Album("album", "Album", [Asset("old", "1", "old")])
    sync.refresh()
    sync.sync_album()
    original = sync.frame.import_video
    def corrupt(*args):
        record = original(*args)
        if damage == "flag":
            sync.frame.db.video_ids.clear()
        else:
            path = record["path"] if damage == "movie" else str(Path(record["path"]).with_suffix(".webp"))
            sync.frame.files[path] = b"damaged"
        return record
    sync.frame.import_video = corrupt
    sync.immich.album.return_value = Album("album", "Album", [Asset("clip", "1", "clip", "video")])
    sync.refresh()
    with pytest.raises(SyncError):
        sync.sync_album()
    assert sync.frame.db.members[123] == {1, 10, 11}
    assert len(list(sync.config.data_dir.glob("videos/*"))) == 2
    assert set(sync.device_state()["managed_photos"]) == {"10"}


def test_video_append_then_mirror_preserves_manual_items(video_sync):
    sync = video_sync
    sync.refresh()
    sync.sync_album()
    sync.immich.album.return_value = Album("album", "Videos", [])
    sync.config = replace(sync.config, sync_mode="append")
    sync.refresh()
    assert sync.sync_album()["retained"] == 1
    sync.config = replace(sync.config, sync_mode="mirror")
    sync.refresh()
    assert sync.sync_album()["removed"] == 1
    assert sync.frame.db.members[123] == {1}
    assert sync.frame.db.video_ids == {10}  # Still in device memory.


def test_video_multi_target_plays_and_mixes_with_photos(multi, video_sync):
    multi.frame = VideoFrame()
    multi.clients["one"] = video_sync.immich
    multi.refresh()
    result = multi.sync_album()
    assert not result["errors"]
    assert result["videos"] == result["photos"] == 1
    assert result["items"] == 2 and multi.frame.played == [123]
    assert not list(multi.config.data_dir.glob("sources/*/videos/*"))
    multi.clients["two"].album.return_value = Album("album", "Empty", [])
    result = cycle(multi)
    assert result["videos"] == 1 and result["photos"] == 0 and result["removed"] == 1


def test_video_paths_reject_symlinks_and_cleanup_only_owned_files(video_sync, tmp_path):
    sync = video_sync
    sync.refresh()
    item = sync.cache.read()["photos"][0]
    cover = sync.cache.cover_path(item)
    cover.parent.mkdir()
    unrelated = tmp_path / "private"
    unrelated.write_bytes(b"keep")
    cover.symlink_to(unrelated)
    with pytest.raises(SyncError, match="symlinks"):
        sync.cache.prepare(sync.immich, item)
    cover.unlink()
    sync.cache.prepare(sync.immich, item)
    note = cover.parent / "keep.mp4"
    note.write_bytes(b"not managed")
    sync.immich.album.return_value = Album("album", "Empty", [])
    sync.refresh()
    assert list(cover.parent.iterdir()) == [note]
    assert unrelated.read_bytes() == b"keep"


def test_native_video_inventory_reads_flag():
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE album_head(id INTEGER, type INTEGER, name TEXT);
            CREATE TABLE album_pic(album_id INTEGER, pic_id INTEGER);
            CREATE TABLE pic_info(id INTEGER, path_name TEXT, pic_video_flag INTEGER);
            INSERT INTO pic_info VALUES(1,'/userdata/photo.webp',0),(2,'/userdata/video.mp4',1);
        """)
        result = read_inventory(db.serialize())
    assert result.video_ids == {2}
    assert result.photos[2] == {"id": 2, "path": "/userdata/video.mp4"}


def test_native_video_upload_over_http_has_three_parts_and_readback(tmp_path):
    movie, cover = tmp_path / "clip.mp4", tmp_path / "clip.webp"
    movie.write_bytes(b"video-block" * 200000)
    cover.write_bytes(b"webp-cover")
    filename = "vi-" + file_digest(movie)[:24] + ".mp4"
    remote = "/userdata/app_pic/20269/" + filename
    files, commands = {}, []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/upload"
            assert not self.headers.get("Transfer-Encoding")
            body = self.rfile.read(int(self.headers["Content-Length"]))
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
            parts = list(message.iter_parts())
            assert len(parts) == 3
            for part in parts:
                assert int(part["Content-Length"]) == len(part.get_payload(decode=True))
            assert parts[0].get_filename() == "cmd.json"
            command = json.loads(parts[0].get_payload(decode=True))
            commands.append(command)
            assert command["FileName"] == parts[1].get_filename() == filename
            assert command["PreviewFileName"] == parts[2].get_filename() == filename[:-4] + ".webp"
            for part in parts[1:]:
                files["/userdata/app_pic/20269/" + part.get_filename()] = part.get_payload(decode=True)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ReturnCode":0}')

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(files[self.path])))
            self.end_headers()
            self.wfile.write(files[self.path])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        frame = Frame(Config(host="127.0.0.1", port=server.server_port))
        record = {"id": 9, "path": remote}
        frame.inventory = Mock(return_value=Inventory([], {9: record}, {123: {9}}, {9}))
        assert frame.import_video(123, filename, movie, cover) == record
        assert len(commands) == 1 and commands[0]["Command"] == "Photo/LocalAddToAlbum"
        assert files[remote] == movie.read_bytes()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_multipart_reads_bounded_chunks_and_closes_files(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * (3 * 1024 * 1024))
    with FileMultipart({}, [("clip.mp4", path)]) as body:
        total = 0
        while chunk := body.read(10 * 1024 * 1024):
            assert len(chunk) <= 1024 * 1024
            total += len(chunk)
        assert total == len(body)
        handles = [p for p in body.parts if not isinstance(p, BytesIO)]
    assert all(p.closed for p in handles)


def test_immich_video_download_streams_and_does_not_expose_url(tmp_path):
    client = Immich(Config(share_url="https://immich.test/share/private-key"))
    response = Mock(headers={"Content-Type": "video/mp4"})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = iter([b"first", b"second"])
    client._request = Mock(return_value=response)
    destination = tmp_path / "download"
    client.download_video("id", destination)
    assert destination.read_bytes() == b"firstsecond"
    client._request.assert_called_once_with("/assets/id/video/playback", stream=True)
    response.headers["Content-Type"] = "text/html"
    with pytest.raises(SyncError, match="non-video"):
        client.download_video("id", destination)
    response.headers["Content-Type"] = "video/mp4"
    response.iter_content.side_effect = requests.ConnectionError("https://immich.test/share/private-key")
    with pytest.raises(SyncError, match="interrupted") as error:
        client.download_video("id", destination)
    assert "private-key" not in str(error.value)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
@pytest.mark.parametrize("audio,fit", [(True, "contain"), (False, "cover")])
def test_real_ffmpeg_prepares_rotated_video_and_full_duration(tmp_path, audio, fit):
    source, rotated, output = [tmp_path / name for name in ("source.mp4", "rotated.mp4", "output.mp4")]
    command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    command += ["-t", "1.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "2"]
    if audio:
        command += ["-c:a", "aac"]
    run_media(command + [str(source)])
    run_media(["ffmpeg", "-v", "error", "-display_rotation", "90", "-i", str(source),
               "-c", "copy", str(rotated)])
    assert probe(rotated)["video"]["side_data_list"][0]["rotation"] == 90
    transcode(rotated, output, fit)
    result = probe(output)
    assert abs(result["duration"] - 1.5) < 0.1
    assert result["video"]["codec_name"] == "h264"
    assert result["video"]["profile"] == "Main"
    assert result["video"]["r_frame_rate"] == "30/1"
    assert not any(s.get("rotation", 0) for s in result["video"].get("side_data_list", []))
    assert [s["codec_name"] for s in result["streams"] if s["codec_type"] == "audio"] == (["aac"] if audio else [])
    cover = Image.open(BytesIO(make_cover(output)))
    assert cover.format == "WEBP" and cover.size == (800, 1280)
    if fit == "contain":
        assert sum(cover.getpixel((0, 640))) < 15  # Portrait after autorotation: side bars.
        assert sum(cover.getpixel((400, 5))) > 40
