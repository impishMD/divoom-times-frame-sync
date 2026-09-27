# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from timesframesync.cache import Cache
from timesframesync.config import Config, SyncError
from timesframesync.source import Album, Asset
from timesframesync.sync import Synchronizer
from test_sync import FakeFrame, jpeg
from test_sources import multi, cycle


def setup_sync(tmp_path):
    sync = Synchronizer(Config(data_dir=tmp_path, frame_album="immich"))
    sync.frame = FakeFrame()
    sync.immich = Mock()
    sync.immich.album.return_value = Album("album", "Photos", [Asset("a", "1", "a"), Asset("b", "1", "b")])
    sync.immich.preview.side_effect = lambda asset: jpeg("red" if asset == "a" else "blue")
    return sync


def test_streams_one_photo_and_removes_it_before_next_download(tmp_path):
    sync = setup_sync(tmp_path)
    def preview(asset):
        assert not list(tmp_path.glob("photos/*.jpg"))
        return jpeg("red" if asset == "a" else "blue")
    sync.immich.preview.side_effect = preview
    assert sync.refresh()["downloaded"] == 0
    sync.immich.preview.assert_not_called()
    result = sync.sync_album()
    assert result["downloaded"] == result["uploaded"] == 2
    assert not list(tmp_path.glob("photos/*.jpg"))
    assert len(sync.device_state()["managed_photos"]) == 2
    assert all(Cache.device_identity(p) for p in sync.cache.read()["photos"])

    restarted = Synchronizer(sync.config)
    restarted.frame, restarted.immich = sync.frame, sync.immich
    restarted.immich.preview.reset_mock()
    restarted.refresh()
    result = restarted.sync_album()
    assert result["downloaded"] == result["uploaded"] == 0
    restarted.immich.preview.assert_not_called()


def test_missing_device_file_record_is_redownloaded(tmp_path):
    sync = setup_sync(tmp_path)
    sync.refresh()
    sync.sync_album()
    removed = sync.frame.db.photos.pop(10)
    sync.frame.files.pop(removed["path"])
    sync.frame.db.members[123].remove(10)
    sync.immich.preview.reset_mock()
    sync.refresh()
    result = sync.sync_album()
    assert result["downloaded"] == result["uploaded"] == 1
    sync.immich.preview.assert_called_once_with("a")
    assert not list(tmp_path.glob("photos/*.jpg"))


def test_failed_upload_retains_only_pending_file_and_restart_resumes(tmp_path):
    sync = setup_sync(tmp_path)
    original_upload = sync.frame.import_photo
    def upload(*args, **kwargs):
        if sync.frame.uploads == 1:
            raise SyncError("upload interrupted")
        return original_upload(*args, **kwargs)
    sync.frame.import_photo = upload
    sync.refresh()
    with pytest.raises(SyncError, match="interrupted"):
        sync.sync_album()
    assert len(list(tmp_path.glob("photos/*.jpg"))) == 1
    assert len(sync.device_state()["managed_photos"]) == 1
    sync.frame.import_photo = original_upload
    sync.immich.preview.reset_mock()
    sync.refresh()
    result = sync.sync_album()
    assert result["uploaded"] == 1 and result["downloaded"] == 0
    sync.immich.preview.assert_not_called()
    assert not list(tmp_path.glob("photos/*.jpg"))


def test_failed_readback_keeps_temporary_file(tmp_path):
    sync = setup_sync(tmp_path)
    sync.refresh()
    sync.frame.fetch_file = Mock(return_value=b"damaged")
    with pytest.raises(SyncError, match="different bytes"):
        sync.sync_album()
    assert len(list(tmp_path.glob("photos/*.jpg"))) == 1
    assert not sync.device_state().get("managed_photos")


def test_failed_journal_write_keeps_temporary_file(tmp_path):
    sync = setup_sync(tmp_path)
    sync.refresh()
    save = sync.save_device_state
    def fail_on_ownership(values):
        if "managed_photos" in values:
            raise OSError("disk full")
        save(values)
    sync.save_device_state = fail_on_ownership
    with pytest.raises(OSError, match="disk full"):
        sync.sync_album()
    assert len(list(tmp_path.glob("photos/*.jpg"))) == 1
    sync.save_device_state = save
    sync.refresh()
    assert sync.sync_album()["uploaded"] == 1  # First photo was already imported.
    assert not list(tmp_path.glob("photos/*.jpg"))


def test_lost_manifests_and_journal_recover_without_duplicates(tmp_path):
    sync = setup_sync(tmp_path)
    sync.refresh()
    sync.sync_album()
    sync.cache.path.unlink()
    (tmp_path / "device-state.json").unlink()
    sync.refresh()
    result = sync.sync_album()
    assert result["downloaded"] == 2 and result["uploaded"] == 0
    assert sync.frame.db.members[123] == {1, 10, 11}
    assert not list(tmp_path.glob("photos/*.jpg"))


def test_existing_cache_migrates_to_fingerprints_without_download(tmp_path):
    sync = setup_sync(tmp_path)
    sync.cache.sync(sync.immich, sync.immich.album())
    manifest = sync.cache.read()
    for photo in manifest["photos"]:
        photo.pop("device_filename")
        photo.pop("device_sha256")
    sync.cache.path.write_text(json.dumps(manifest))
    sync.immich.preview.reset_mock()
    sync.refresh()
    result = sync.sync_album()
    assert result["downloaded"] == 0 and result["uploaded"] == 2
    sync.immich.preview.assert_not_called()
    assert not list(tmp_path.glob("photos/*.jpg"))


def test_original_uuid_cache_names_are_migrated_and_removed(tmp_path):
    sync = setup_sync(tmp_path)
    sync.cache.sync(sync.immich, sync.immich.album())
    manifest = sync.cache.read()
    photo = manifest["photos"][0]
    path = sync.cache.photo_path(photo)
    legacy = "photos/11111111-1111-4111-8111-111111111111-" + photo["sha256"][:16] + ".jpg"
    path.rename(tmp_path / legacy)
    photo["file"] = legacy
    photo.pop("device_filename")
    photo.pop("device_sha256")
    sync.cache.path.write_text(json.dumps(manifest))
    sync.immich.preview.reset_mock()
    sync.refresh()
    assert sync.sync_album()["downloaded"] == 0
    sync.immich.preview.assert_not_called()
    assert not list(tmp_path.glob("photos/*.jpg"))


def test_obsolete_temporary_versions_are_cleaned_but_unknown_files_survive(tmp_path):
    sync = setup_sync(tmp_path)
    sync.cache.sync(sync.immich, sync.immich.album())
    note = tmp_path / "photos" / "keep.jpg"
    note.write_bytes(b"unrelated")
    sync.immich.album.return_value = Album("album", "Photos", [])
    sync.refresh()
    assert list(tmp_path.glob("photos/*.jpg")) == [note]


def test_cleanup_rejects_paths_and_symlinks_outside_cache(tmp_path):
    cache = Cache(Config(data_dir=tmp_path / "data"))
    external = tmp_path / "external"
    external.mkdir()
    secret = external / ("a" * 24 + "-" + "b" * 16 + ".jpg")
    secret.write_bytes(b"keep")
    with pytest.raises(SyncError):
        cache.discard({"file": str(secret)})
    cache.directory.mkdir()
    (cache.directory / "photos").symlink_to(external, target_is_directory=True)
    with pytest.raises(SyncError):
        cache.discard({"file": "photos/" + secret.name})
    with pytest.raises(SyncError):
        cache.clean_unused([])
    assert secret.read_bytes() == b"keep"


def test_multi_restart_uses_fingerprints_without_any_local_photos(multi):
    cycle(multi)
    assert not list(multi.config.data_dir.glob("sources/*/photos/*.jpg"))
    from timesframesync.multi_sync import MultiSynchronizer
    restarted = MultiSynchronizer(multi.config)
    restarted.frame, restarted.clients = multi.frame, multi.clients
    for client in restarted.clients.values():
        client.preview.reset_mock()
    result = cycle(restarted)
    assert result["downloaded"] == result["uploaded"] == 0
    for client in restarted.clients.values():
        client.preview.assert_not_called()


def test_changed_image_uploads_then_prunes_without_local_cache(multi):
    cycle(multi)
    multi.clients["one"].album.return_value = Album("album", "one", [Asset("same-id", "v2", "a")])
    multi.clients["one"].preview.return_value = jpeg("green")
    result = cycle(multi)
    assert result["downloaded"] == result["uploaded"] == result["removed"] == 1
    assert multi.frame.db.members[123] == {1, 11, 12}
    assert not list(multi.config.data_dir.glob("sources/*/photos/*.jpg"))
