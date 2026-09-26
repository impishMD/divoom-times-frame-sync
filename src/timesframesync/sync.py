# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

import json
import hashlib
import logging
from pathlib import PurePosixPath

from .cache import Cache, atomic_write
from .config import Config, SYNC_MODES, SyncError
from .frame import Frame
from .immich import Immich

log = logging.getLogger(__name__)


class Synchronizer:
    def __init__(self, config: Config):
        self.config = config
        self.immich = Immich(config) if config.share_url else None
        self.cache = Cache(config)
        self.frame = Frame(config)
        self.photo_sources = {}

    def refresh(self, *, download: bool = False) -> dict:
        if self.immich is None:
            raise SyncError("This target has no direct source; refresh its source group first")
        album = self.immich.album()
        result = self.cache.sync(self.immich, album, download=download)
        log.info("Immich album %s: %d photos, %d videos, %d downloaded, %d videos skipped",
                 album.name, result["photos"], result["videos"], result["downloaded"], result["skipped_videos"])
        return result

    def device_state(self) -> dict:
        path = self.config.data_dir / "device-state.json"
        identity = f"{self.config.host}:{self.config.port}"
        previous = {}
        if path.exists():
            try:
                previous = json.loads(path.read_text())
                if not isinstance(previous, dict):
                    raise ValueError
            except ValueError:
                raise SyncError("Invalid device-state.json") from None
            if previous.get("target") != identity:
                previous = {}
        return {**previous, "target": identity}

    def save_device_state(self, values: dict):
        atomic_write(self.config.data_dir / "device-state.json",
                     json.dumps({**self.device_state(), **values}, indent=2).encode())

    def restore_previous(self):
        self.frame.restore()
        clock_id = self.device_state().get("previous_clock_id")
        if clock_id:
            self.frame.select_clock(clock_id)

    def sync_album(self, *, play: bool = True) -> dict:
        if self.config.sync_mode not in SYNC_MODES:
            raise SyncError("SYNC_MODE must be mirror or append")
        manifest = self.cache.read()
        if "album_id" not in manifest:
            raise SyncError("Refresh the source albums before syncing to the frame")
        inventory = self.frame.inventory()
        album_id = inventory.album(self.config.frame_album)["id"]
        clock = self.frame.info()["clock"]
        device_id = clock.get("DeviceId")
        if not isinstance(device_id, int):
            raise SyncError("Frame did not return its device identity")
        state = self.device_state()
        scope = {"device_id": device_id, "album_id": album_id, "source_album_id": manifest["album_id"]}
        managed = dict(state.get("managed_photos", {})) if state.get("native_scope") == scope else {}
        if clock.get("ClockId") != album_id and not state.get("previous_clock_id"):
            self.save_device_state({"previous_clock_id": clock.get("ClockId")})
        desired: set[int] = set()
        videos: set[int] = set()
        uploaded = 0
        downloaded = 0
        for photo in manifest["photos"]:
            cache, source, source_photo = self.photo_sources.get(photo["id"], (self.cache, self.immich, photo))
            is_video = source_photo.get("kind") == "video"
            identity = cache.device_identity(source_photo)
            record = inventory.find_file(identity[0]) if identity else None
            content = None
            if record is None:
                filename, content, fetched = cache.prepare(source, source_photo)
                downloaded += fetched
                cache.remember(source_photo)
                identity = cache.device_identity(source_photo)
                record = inventory.find_file(filename)
                if record is None:
                    if is_video:
                        log.info("Uploading native video to album %s", self.config.frame_album)
                        record = self.frame.import_video(album_id, filename, content, cache.cover_path(source_photo), self.config.user_id)
                    else:
                        record = self.frame.import_photo(album_id, filename, content, self.config.user_id)
                    uploaded += 1
                    inventory = self.frame.inventory()
            if record is not None:
                if is_video:
                    log.info("Verifying native video and cover in album %s", self.config.frame_album)
                    remote_digest = self.frame.file_digest(record["path"])
                    cover = str(PurePosixPath(record["path"]).with_suffix(".webp"))
                    if record["id"] not in inventory.video_ids or self.frame.file_digest(cover) != source_photo["preview_sha256"]:
                        raise SyncError("Native video flag or cover verification failed; refusing to prune the album")
                else:
                    remote = self.frame.fetch_file(record["path"])
                    remote_digest = hashlib.sha256(remote).hexdigest() if remote is not None else None
                if remote_digest != identity[1]:
                    raise SyncError("Existing native media has different bytes; refusing to duplicate or overwrite it")
                if record["id"] not in inventory.members.get(album_id, set()):
                    self.frame.add_existing(album_id, record["id"])
                    inventory = self.frame.inventory()
            self.frame.wait_members(album_id, present=[record["id"]])
            desired.add(record["id"])
            if is_video:
                videos.add(record["id"])
            managed[str(record["id"])] = record
            # Journal every successful addition, retaining older versions until
            # all uploads succeed. A restart can then finish safely.
            self.save_device_state({"native_scope": scope, "managed_photos": managed})
            # The file and membership have been verified and ownership is
            # durable. Keep only fingerprints; the next cycle needs no media.
            cache.discard(source_photo)

        # Only remove our known entries from this album, never from All Photos
        # or other albums. A reused numeric ID must still have the same path.
        inventory = self.frame.inventory()
        if inventory.album(self.config.frame_album)["id"] != album_id:
            raise SyncError("Target album changed during sync; retry")
        stale = {int(pic_id) for pic_id, record in managed.items()
                 if int(pic_id) not in desired
                 and inventory.photos.get(int(pic_id)) == record
                 and int(pic_id) in inventory.members.get(album_id, set())}
        removed = stale if self.config.sync_mode == "mirror" else set()
        stale_list = sorted(removed)
        for offset in range(0, len(stale_list), 100):
            self.frame.remove_from_album(album_id, set(stale_list[offset:offset + 100]))
        self.frame.wait_members(album_id, present=desired, absent=removed)
        # Keep ownership of retained entries across append runs, including an
        # empty source album, so switching back to mirror can remove them.
        tracked = desired | (stale - removed)
        managed = {str(pic_id): managed[str(pic_id)] for pic_id in tracked}
        self.save_device_state({"native_scope": scope, "managed_photos": managed})
        if play and desired:
            self.frame.play_album(album_id)
        result = {"album": self.config.frame_album, "album_id": album_id,
                  "sync_mode": self.config.sync_mode, "items": len(desired),
                  "photos": len(desired - videos), "videos": len(videos),
                  "downloaded": downloaded, "uploaded": uploaded, "removed": len(removed), "retained": len(stale - removed)}
        log.info("Native album %s (%d), mode=%s: %d photos and %d videos verified, %d downloaded, %d uploaded, %d removed, %d missing items retained",
                 self.config.frame_album, album_id, self.config.sync_mode,
                 result["photos"], result["videos"], downloaded, uploaded, len(removed), len(stale - removed))
        return result
