# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from timesframesync.cli import main, parser
from timesframesync.config import SyncError
from timesframesync.frame import Inventory, validate_filename
from timesframesync.sync import Synchronizer
from timesframesync.source import Album, Asset
from test_storage import setup_sync
from test_video import video_sync
from test_sources import multi, cycle


def ready(tmp_path):
    sync = setup_sync(tmp_path)
    sync.refresh()
    sync.sync_album()
    return sync


def test_restart_and_unchanged_cycle_never_read_media_or_rewrite_journal(tmp_path):
    sync = ready(tmp_path)
    restarted = Synchronizer(sync.config)
    restarted.frame, restarted.immich = sync.frame, sync.immich
    restarted.frame.fetch_file = Mock(side_effect=AssertionError('no media reads'))
    restarted.cache.prepare = Mock(side_effect=AssertionError('no preparing'))
    restarted.save_device_state = Mock(side_effect=AssertionError('no journal writes'))
    restarted.immich.preview.reset_mock()
    restarted.refresh()
    result = restarted.sync_album(play=False)
    assert result['skipped'] == 2 and result['checked'] == 0
    assert result['downloaded'] == result['uploaded'] == 0
    restarted.immich.preview.assert_not_called()


def test_new_upload_checked_once_and_legacy_state_checked_once(tmp_path):
    sync = setup_sync(tmp_path)
    sync.frame.fetch_file = Mock(wraps=sync.frame.fetch_file)
    sync.refresh()
    assert sync.sync_album()['checked'] == 2
    assert sync.frame.fetch_file.call_count == 2
    state = sync.device_state()
    state.pop('verified_files')
    (tmp_path / 'device-state.json').write_text(json.dumps(state))
    assert sync.sync_album()['checked'] == 2
    assert sync.sync_album()['checked'] == 0
    assert sync.frame.fetch_file.call_count == 4


def test_different_device_does_not_reuse_verification(tmp_path):
    sync = ready(tmp_path)
    sync.frame.info = Mock(return_value={'clock': {'DeviceId': 1000, 'ClockId':123}})
    sync.frame.fetch_file = Mock(wraps=sync.frame.fetch_file)
    assert sync.sync_album()['checked'] == 2
    assert sync.frame.fetch_file.call_count == 2


def test_photo_repair_replaces_only_damaged_and_preserves_all_album_links(tmp_path):
    sync = ready(tmp_path)
    old = sync.frame.db.photos[10]
    sync.frame.db.albums.append({'id':456,'name':'Other','type':0})
    sync.frame.db.members[456] = {10,1}
    sync.frame.files[old['path']] = b'corrupt'
    assert sync.sync_album()['skipped'] == 2
    state = (tmp_path / 'device-state.json').read_bytes()
    uploads = sync.frame.uploads
    report = sync.repair_album(dry_run=True)
    assert report['problems'] == 1 and report['healthy'] == 1
    assert not report['errors'] and report['repaired'] == 0
    assert sync.frame.uploads == uploads
    assert (tmp_path / 'device-state.json').read_bytes() == state
    sync.immich.preview.reset_mock()
    report = sync.repair_album()
    assert report['repaired'] == report['downloaded'] == 1 and not report['errors']
    sync.immich.preview.assert_called_once_with('a')
    assert sync.frame.db.members == {123:{1,11,12},456:{1,12}}
    assert sync.frame.files[old['path']] == b'corrupt'  # no global deletion
    assert sync.frame.played == [123,123]
    assert not list(tmp_path.glob('photos/*.jpg'))
    sync.frame.fetch_file = Mock(side_effect=AssertionError('no recheck'))
    assert sync.sync_album(play=False)['skipped'] == 2
    assert sync.frame.uploads == uploads + 1


@pytest.mark.parametrize('damage',['movie','cover','flag','404'])
def test_video_repair(video_sync, damage):
    sync = video_sync
    sync.refresh();sync.sync_album()
    path = sync.frame.db.photos[10]['path']
    if damage == 'flag':
        sync.frame.db.video_ids.clear()
    elif damage == '404':
        sync.frame.files.pop(path)
    else:
        sync.frame.files[path if damage == 'movie' else str(Path(path).with_suffix('.webp'))] = b'bad'
    report = sync.repair_album()
    assert report['repaired'] == 1 and report['downloaded'] == 1 and not report['errors']
    assert sync.frame.db.members[123] == {1,11}
    assert 11 in sync.frame.db.video_ids
    assert not list(sync.config.data_dir.glob('videos/*'))
    sync.frame.file_digest = Mock(side_effect=AssertionError('no frame bytes'))
    assert sync.sync_album(play=False)['skipped'] == 1


def test_repair_missing_record_and_missing_membership(tmp_path):
    sync = ready(tmp_path)
    sync.frame.db.members[123].remove(10)
    report = sync.repair_album()
    assert report['repaired'] == 1 and report['downloaded'] == 0
    assert sync.frame.uploads == 2
    sync.frame.db.photos.pop(10)
    sync.frame.db.members[123].remove(10)
    report = sync.repair_album()
    assert report['repaired'] == report['downloaded'] == 1
    assert sync.frame.uploads == 3


def test_repair_network_error_does_not_trigger_replacement(tmp_path):
    sync = ready(tmp_path)
    sync.frame.fetch_file = Mock(side_effect=SyncError('timeout'))
    report = sync.repair_album()
    assert len(report['errors']) == 2 and report['repaired'] == report['problems'] == 0
    assert sync.frame.uploads == 2
    assert sync.frame.db.members[123] == {1,10,11}


def test_failed_source_download_preserves_links_and_invalidates_trust(tmp_path):
    sync = ready(tmp_path)
    sync.frame.files[sync.frame.db.photos[10]['path']] = b'bad'
    sync.immich.preview.side_effect = SyncError('source unavailable')
    report = sync.repair_album()
    assert report['errors'] and report['repaired'] == 0
    assert sync.frame.db.members[123] == {1,10,11}
    assert '10' not in sync.device_state()['verified_files']
    with pytest.raises(SyncError,match='tfs repair'):
        sync.sync_album()


def test_interrupted_membership_transfer_resumes_without_reupload(tmp_path):
    sync = ready(tmp_path)
    sync.frame.files[sync.frame.db.photos[10]['path']] = b'bad'
    remove = sync.frame.remove_from_album
    sync.frame.remove_from_album = Mock(side_effect=SyncError('timeout'))
    assert sync.repair_album()['errors']
    assert sync.device_state()['pending_repair']
    assert sync.frame.db.members[123] == {1,10,11,12}
    with pytest.raises(SyncError,match='interrupted repair'):
        sync.sync_album()
    sync.frame.remove_from_album = remove
    sync.immich.preview.reset_mock()
    report = sync.repair_album()
    assert not report['errors'] and report['repaired'] == 1
    assert sync.frame.db.members[123] == {1,11,12}
    assert sync.frame.uploads == 3
    sync.immich.preview.assert_not_called()
    assert sync.device_state()['pending_repair'] is None


def test_repair_never_prunes_source_absences(tmp_path):
    sync = ready(tmp_path)
    sync.immich.album.return_value = Album('album','Photos',[])
    sync.refresh()
    assert sync.repair_album()['items'] == 0
    assert sync.frame.db.members[123] == {1,10,11}
    assert set(sync.device_state()['managed_photos']) == {'10','11'}


def test_repaired_names_recover_after_lost_metadata(tmp_path):
    sync = ready(tmp_path)
    sync.frame.files[sync.frame.db.photos[10]['path']] = b'bad'
    assert sync.repair_album()['repaired'] == 1
    sync.cache.path.unlink();(tmp_path/'device-state.json').unlink()
    sync.refresh()
    result = sync.sync_album()
    assert result['uploaded'] == 0 and result['checked'] == 2
    assert sync.frame.uploads == 3


def test_repeated_repair_uses_increasing_generations(tmp_path):
    sync = ready(tmp_path)
    for pic_id in (10,12,13):
        sync.frame.files[sync.frame.db.photos[pic_id]['path']] = b'bad'
        assert not sync.repair_album()['errors']
    assert sync.frame.db.members[123] == {1,11,14}
    assert sync.sync_album()['uploaded'] == 0
    for record in sync.frame.db.photos.values():
        validate_filename(Path(record['path']).name)


def test_multi_repair_blocks_failed_group_and_handles_other_destinations(multi):
    cycle(multi)
    multi.frame.files[multi.frame.db.photos[10]['path']] = b'bad'
    multi.clients['one'].album.side_effect = SyncError('offline')
    multi.refresh()
    result = multi.repair_album()
    assert result['errors'] and result['repaired'] == 0
    assert multi.frame.uploads == 2
    assert multi.ready is None


def test_cli_repair_dry_run_runs_audit_and_returns_failure_on_damage(tmp_path, monkeypatch):
    sync = ready(tmp_path)
    sync.frame.files[sync.frame.db.photos[10]['path']] = b'bad'
    monkeypatch.setattr('sys.argv',['tfs','repair','--dry-run'])
    monkeypatch.setattr('timesframesync.cli.Config.load',lambda *a,**k:sync.config)
    monkeypatch.setattr('timesframesync.cli.Synchronizer',lambda *a:sync)
    assert main() == 1
    assert sync.frame.uploads == 2
    assert parser().parse_args(['repair']).dry_run is False


def test_failed_replacement_readback_then_retry_removes_all_broken_links(tmp_path):
    sync = ready(tmp_path)
    sync.frame.files[sync.frame.db.photos[10]['path']] = b'bad'
    original = sync.frame.import_photo
    def corrupt(*a, **kw):
        record = original(*a, **kw)
        sync.frame.files[record['path']] = b'bad replacement'
        return record
    sync.frame.import_photo = corrupt
    assert sync.repair_album()['errors']
    assert sync.frame.db.members[123] == {1,10,11,12}
    assert sync.device_state()['pending_repair']
    sync.frame.import_photo = original
    result = sync.repair_album()
    assert result['repaired'] == 1 and not result['errors']
    assert sync.frame.db.members[123] == {1,11,13}
    assert not sync.device_state()['pending_repair']


def test_repair_deduplicates_shared_physical_files_across_sources(multi):
    multi.clients['two'].preview.side_effect = multi.clients['one'].preview.side_effect
    multi.clients['two'].preview.return_value = multi.clients['one'].preview.return_value
    cycle(multi)
    multi.frame.fetch_file = Mock(wraps=multi.frame.fetch_file)
    multi.refresh()
    report = multi.repair_album(dry_run=True)
    assert not report['errors']
    assert report['checked'] == len(multi.frame.db.photos)-1


def test_reencoded_video_can_recover_after_encoder_output_changes(video_sync, monkeypatch):
    sync = video_sync
    sync.refresh(); sync.sync_album()
    sync.frame.files[sync.frame.db.photos[10]['path']] = b'bad'
    def encode(source, destination, fit):
        destination.write_bytes(b'new encoding')
        return {'duration':3.0,'width':800,'height':1280}
    monkeypatch.setattr('timesframesync.cache.transcode', encode)
    report = sync.repair_album()
    assert not report['errors'] and report['repaired'] == 1
    assert sync.sync_album()['uploaded'] == 0


def test_failed_final_repair_journal_keeps_copy_and_resumes(tmp_path):
    sync = ready(tmp_path)
    sync.frame.files[sync.frame.db.photos[10]['path']] = b'bad'
    save = sync.save_device_state
    def fail(values):
        if values.get('pending_repair', 'absent') is None:
            raise OSError('disk full')
        save(values)
    sync.save_device_state = fail
    assert sync.repair_album()['errors']
    assert sync.frame.db.members[123] == {1,11,12}
    assert sync.device_state()['pending_repair']
    assert list(tmp_path.glob('photos/*.jpg'))
    sync.save_device_state = save
    sync.frame.fetch_file = Mock(wraps=sync.frame.fetch_file)
    report = sync.repair_album()
    assert not report['errors'] and report['repaired'] == 1
    assert sync.frame.uploads == 3
    assert sync.frame.fetch_file.call_count == 2  # healthy replacement and other photo, once each
    assert not list(tmp_path.glob('photos/*.jpg'))


def test_generation_does_not_reuse_lower_hole():
    canonical = 'im-' + 'a'*24 + '.webp'
    name = 'r' + 'a'*24 + '01.webp'
    inv = Inventory([], {1:{'id':1,'path':'/userdata/'+name}}, {})
    assert inv.recovery_name(canonical) == 'r'+'a'*24+'02.webp'
