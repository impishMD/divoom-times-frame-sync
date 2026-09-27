# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

import json
import hashlib
import logging
import time
from pathlib import PurePosixPath

from .cache import Cache, atomic_write
from .config import Config, SYNC_MODES, SyncError
from .frame import Frame
from .immich import Immich
from .timing import timed, timed_operation

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
        started = time.monotonic()
        try:
            album = self.immich.album()
            result = self.cache.sync(self.immich, album, download=download)
        except (SyncError, OSError) as error:
            log.error("Immich source failed: %s; %.1fs", error, time.monotonic() - started)
            raise
        log.info("Immich album %s: listed %d photos, %d videos; %d prefetched, %d videos skipped; %.1fs",
                 album.name, result["photos"], result["videos"], result["downloaded"], result["skipped_videos"],
                 time.monotonic() - started)
        return result

    @timed(log, "Reading frame journal", level=logging.DEBUG)
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

    @timed(log, "Saving frame journal", level=logging.DEBUG)
    def save_device_state(self, values: dict):
        atomic_write(self.config.data_dir / "device-state.json",
                     json.dumps({**self.device_state(), **values}, indent=2).encode())

    @timed(log, "Restoring previous Divoom display")
    def restore_previous(self):
        self.frame.restore()
        clock_id = self.device_state().get("previous_clock_id")
        if clock_id:
            self.frame.select_clock(clock_id)

    @staticmethod
    def fingerprint(record: dict, photo: dict) -> dict:
        return {**record, "sha256": photo["device_sha256"],
                "kind": photo.get("kind", "photo"),
                "cover_sha256": photo.get("preview_sha256")}

    def check_file(self, record: dict, photo: dict, inventory) -> list[str]:
        """Network failures raise; only a completed check can report damage."""
        kind = "video and cover" if photo.get("kind") == "video" else "photo"
        with timed_operation(log, "Checking %s contents in album %s", kind, self.config.frame_album,
                             announce=photo.get("kind") == "video") as operation:
            problems = []
            if photo.get("kind") == "video":
                digest = self.frame.file_digest(record["path"])
                cover = str(PurePosixPath(record["path"]).with_suffix(".webp"))
                if self.frame.file_digest(cover) != photo["preview_sha256"]:
                    problems.append("cover missing or corrupt")
            else:
                content = self.frame.fetch_file(record["path"])
                digest = hashlib.sha256(content).hexdigest() if content is not None else None
            if digest != photo["device_sha256"]:
                problems.append("media missing or corrupt")
            if (record["id"] in inventory.video_ids) != (photo.get("kind") == "video"):
                problems.append("wrong media type")
            if problems:
                operation.status = "problems found"
            return problems

    def audit_file(self, record: dict, photo: dict, inventory, device_id: int, checked: dict) -> list[str]:
        key = (device_id, record["id"], record["path"], photo["device_sha256"],
               photo.get("preview_sha256"), record["id"] in inventory.video_ids)
        if key not in checked:
            checked[key] = self.check_file(record, photo, inventory)
        return checked[key]

    def sync_album(self, *, play: bool = True) -> dict:
        if self.config.sync_mode not in SYNC_MODES:
            raise SyncError("SYNC_MODE must be mirror or append")
        started = time.monotonic()
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
        if state.get("pending_repair"):
            raise SyncError("An interrupted repair needs completing; run tfs repair")
        managed = dict(state.get("managed_photos", {})) if state.get("native_scope") == scope else {}
        verified = dict(state.get("verified_files", {})) if state.get("native_scope") == scope else {}
        def journal():
            self.save_device_state({"native_scope": scope, "managed_photos": managed, "verified_files": verified})

        if clock.get("ClockId") != album_id and not state.get("previous_clock_id"):
            self.save_device_state({"previous_clock_id": clock.get("ClockId")})
        desired: set[int] = set()
        videos: set[int] = set()
        uploaded = 0
        downloaded = 0
        checked = skipped = 0
        for photo in manifest["photos"]:
            cache, source, source_photo = self.photo_sources.get(photo["id"], (self.cache, self.immich, photo))
            is_video = source_photo.get("kind") == "video"
            identity = cache.device_identity(source_photo)
            record = inventory.find_media(identity[0]) if identity else None
            content = None
            imported = False
            if record is None:
                filename, content, fetched = cache.prepare(source, source_photo)
                downloaded += fetched
                cache.remember(source_photo)
                identity = cache.device_identity(source_photo)
                record = inventory.find_media(filename)
                if record is None:
                    if is_video:
                        record = self.frame.import_video(album_id, filename, content, cache.cover_path(source_photo), self.config.user_id, verify=False)
                    else:
                        record = self.frame.import_photo(album_id, filename, content, self.config.user_id, verify=False)
                    uploaded += 1
                    imported = True
                    inventory = self.frame.inventory()
            fingerprint = self.fingerprint(record, source_photo)
            previous = verified.get(str(record["id"]), {})
            trusted = (not imported and all(previous.get(key) == value for key, value in fingerprint.items())
                       and previous.get("verified_at") is not None
                       and (record["id"] in inventory.video_ids) == is_video)
            if trusted:
                skipped += 1
            else:
                problems = self.check_file(record, source_photo, inventory)
                if problems:
                    raise SyncError("Existing native media has different bytes or metadata (" + ", ".join(problems) + "); run tfs repair")
                verified[str(record["id"])] = {**fingerprint, "verified_at": int(time.time())}
                checked += 1
            if record["id"] not in inventory.members.get(album_id, set()):
                self.frame.add_existing(album_id, record["id"])
                inventory = self.frame.inventory()
            if record["id"] not in inventory.members.get(album_id, set()):
                raise SyncError("Native album membership verification failed")
            changed = not trusted or managed.get(str(record["id"])) != record
            desired.add(record["id"])
            if is_video:
                videos.add(record["id"])
            managed[str(record["id"])] = record
            # Journal every successful addition, retaining older versions until
            # all uploads succeed. A restart can then finish safely.
            if changed:
                journal()
            # The file and membership have been verified and ownership is
            # durable. Keep only fingerprints; the next cycle needs no media.
            cache.discard(source_photo)

        # Only remove our known entries from this album, never from All Photos
        # or other albums. A reused numeric ID must still have the same path.
        inventory = self.frame.inventory()
        if inventory.album(self.config.frame_album)["id"] != album_id:
            raise SyncError("Target album changed during sync; retry")
        if any(inventory.photos.get(pic_id) != managed[str(pic_id)] for pic_id in desired):
            raise SyncError("Frame records changed during sync; retry before pruning")
        if not desired <= inventory.members.get(album_id, set()):
            raise SyncError("Album membership changed during sync; retry before pruning")
        stale = {int(pic_id) for pic_id, record in managed.items()
                 if int(pic_id) not in desired
                 and inventory.photos.get(int(pic_id)) == record
                 and int(pic_id) in inventory.members.get(album_id, set())}
        removed = stale if self.config.sync_mode == "mirror" else set()
        stale_list = sorted(removed)
        for offset in range(0, len(stale_list), 100):
            self.frame.remove_from_album(album_id, set(stale_list[offset:offset + 100]))
        if removed:
            self.frame.wait_members(album_id, present=desired, absent=removed)
        # Keep ownership of retained entries across append runs, including an
        # empty source album, so switching back to mirror can remove them.
        tracked = desired | (stale - removed)
        managed = {str(pic_id): managed[str(pic_id)] for pic_id in tracked}
        verified = {key: value for key, value in verified.items() if key in managed}
        values = {"native_scope": scope, "managed_photos": managed, "verified_files": verified}
        current = self.device_state()
        if any(current.get(key) != value for key, value in values.items()):
            self.save_device_state(values)
        if play and desired:
            self.frame.play_album(album_id)
        result = {"album": self.config.frame_album, "album_id": album_id,
                  "sync_mode": self.config.sync_mode, "items": len(desired),
                  "photos": len(desired - videos), "videos": len(videos),
                  "downloaded": downloaded, "uploaded": uploaded, "checked": checked, "skipped": skipped, "removed": len(removed), "retained": len(stale - removed)}
        log.info("Native album %s (%d), mode=%s: %d photos, %d videos, %d matched metadata, %d content checked, %d downloaded, %d uploaded, %d removed, %d missing items retained; %.1fs",
                 self.config.frame_album, album_id, self.config.sync_mode,
                 result["photos"], result["videos"], skipped, checked, downloaded, uploaded, len(removed), len(stale - removed), time.monotonic() - started)
        return result

    @timed(log, "Committing repaired media and album links")
    def _finish_repair(self, pending: dict, photo: dict, inventory, verified: dict, managed: dict, scope: dict, checked: dict):
        """Commit album links only after the replacement's contents are verified."""
        if self.frame.info()["clock"].get("DeviceId") != scope["device_id"]:
            raise SyncError("Frame identity changed during repair")
        replacement = inventory.find_file(pending["filename"])
        if replacement is None:
            raise SyncError("Repair replacement is missing; retry repair")
        if self.audit_file(replacement, photo, inventory, scope["device_id"], checked):
            raise SyncError("Repair replacement failed verification; retry repair")
        old_records = pending.get("obsolete") or ([pending["old"]] if pending.get("old") else [])
        # Snapshot links before upload; also preserve links added in the meantime.
        albums = dict(pending["albums"])
        for old in old_records:
            if inventory.photos.get(old["id"]) == old:
                for album in inventory.albums:
                    if album["type"] == 0 and old["id"] in inventory.members.get(album["id"], set()):
                        albums[str(album["id"])] = album["name"]
        for key, name in albums.items():
            album_id = int(key)
            if not any(a["id"] == album_id and a["name"] == name and a["type"] == 0 for a in inventory.albums):
                raise SyncError("An album changed during repair; leaving existing links intact")
        # Persist the complete transfer before any links change, including on retry.
        pending = {**pending, "albums": albums}
        self.save_device_state({"native_scope": scope, "pending_repair": pending})
        for key in albums:
            album_id = int(key)
            if replacement["id"] not in inventory.members.get(album_id, set()):
                self.frame.add_existing(album_id, replacement["id"])
        inventory = self.frame.inventory()
        if inventory.photos.get(replacement["id"]) != replacement:
            raise SyncError("Repair replacement record changed before removing old links")
        if (replacement["id"] in inventory.video_ids) != (photo.get("kind") == "video"):
            raise SyncError("Repair replacement media type changed")
        if any(replacement["id"] not in inventory.members.get(int(key), set()) for key in albums):
            raise SyncError("Repair replacement membership verification failed")
        for old in old_records:
            if inventory.photos.get(old["id"]) == old and old["id"] != replacement["id"]:
                for key in albums:
                    if old["id"] in inventory.members.get(int(key), set()):
                        self.frame.remove_from_album(int(key), {old["id"]})
                managed.pop(str(old["id"]), None)
                verified.pop(str(old["id"]), None)
        managed[str(replacement["id"])] = replacement
        verified[str(replacement["id"])] = {**self.fingerprint(replacement, photo), "verified_at": int(time.time())}
        self.save_device_state({"native_scope": scope, "managed_photos": managed,
                                "verified_files": verified, "pending_repair": None})
        return replacement

    def repair_album(self, *, dry_run: bool = False, checked: dict | None = None) -> dict:
        """Audit current configured media; never prune absent source items or play."""
        started = time.monotonic()
        manifest = self.cache.read()
        if "album_id" not in manifest:
            raise SyncError("Refresh the source albums before repairing the frame")
        inventory = self.frame.inventory()
        album_id = inventory.album(self.config.frame_album)["id"]
        device_id = self.frame.info()["clock"].get("DeviceId")
        if not isinstance(device_id, int):
            raise SyncError("Frame did not return its device identity")
        scope = {"device_id": device_id, "album_id": album_id, "source_album_id": manifest["album_id"]}
        state = self.device_state()
        matches = state.get("native_scope") == scope
        managed = dict(state.get("managed_photos", {})) if matches else {}
        verified = dict(state.get("verified_files", {})) if matches else {}
        if state.get("pending_repair") and not matches:
            raise SyncError("Interrupted repair belongs to a different device, album or source; restore its configuration")
        pending = state.get("pending_repair") if matches else None
        checked = {} if checked is None else checked
        checks_before = len(checked)
        result = {"album": self.config.frame_album, "dry_run": dry_run, "items": len(manifest["photos"]),
                  "checked": 0, "healthy": 0, "problems": 0, "repaired": 0, "downloaded": 0,
                  "details": [], "errors": {}}
        if pending and not any(p["id"] == pending["asset_id"] for p in manifest["photos"]):
            raise SyncError("Interrupted repair source is no longer listed; preserve its journal and restore source access")
        # Resume a partially committed replacement before examining other items.
        photos = sorted(manifest["photos"], key=lambda p: not (pending and p["id"] == pending["asset_id"]))
        for photo in photos:
            item_started = time.monotonic()
            cache, source, item = self.photo_sources.get(photo["id"], (self.cache, self.immich, photo))
            try:
                identity = cache.device_identity(item)
                content = None
                if identity is None:
                    _, content, fetched = cache.prepare(source, item)
                    result["downloaded"] += fetched
                    if not dry_run:
                        cache.remember(item)
                    identity = cache.device_identity(item)
                record = inventory.find_media(identity[0])
                resuming = pending and pending["asset_id"] == photo["id"]
                if resuming and pending["sha256"] != identity[1]:
                    raise SyncError("Interrupted repair source changed; preserve its journal before recovering")
                problems = ["missing database record"] if record is None else []
                if record:
                    problems.extend(self.audit_file(record, item, inventory, device_id, checked))
                    if record["id"] not in inventory.members.get(album_id, set()):
                        problems.append("missing album membership")
                if resuming:
                    problems.append("interrupted repair")
                if not problems:
                    result["healthy"] += 1
                    if not dry_run:
                        managed[str(record["id"])] = record
                        verified[str(record["id"])] = {**self.fingerprint(record, item), "verified_at": int(time.time())}
                        self.save_device_state({"native_scope": scope, "managed_photos": managed, "verified_files": verified})
                        cache.discard(item)
                    continue
                result["problems"] += 1
                result["details"].append({"id": photo["id"], "problems": problems})
                log.warning("Repair: %s: %s; %.1fs", photo["id"], ", ".join(problems), time.monotonic() - item_started)
                if dry_run:
                    continue
                # Invalidate trust before a source download or mutation can fail.
                if record:
                    verified.pop(str(record["id"]), None)
                self.save_device_state({"native_scope": scope, "managed_photos": managed, "verified_files": verified})
                if problems == ["missing album membership"]:
                    self.frame.add_existing(album_id, record["id"])
                    managed[str(record["id"])] = record
                    verified[str(record["id"])] = {**self.fingerprint(record, item), "verified_at": int(time.time())}
                    self.save_device_state({"native_scope": scope, "managed_photos": managed, "verified_files": verified})
                else:
                    replacement = inventory.find_file(pending["filename"]) if resuming else None
                    replacement_ok = replacement is not None and not self.audit_file(replacement, item, inventory, device_id, checked)
                    if not replacement_ok:
                        if content is None:
                            _, content, fetched = cache.prepare(source, item)
                            result["downloaded"] += fetched
                        cache.remember(item)
                        identity = cache.device_identity(item)
                        old = pending["old"] if resuming else record
                        albums = pending["albums"] if resuming else {str(a["id"]): a["name"] for a in inventory.albums
                                  if a["type"] == 0 and record and record["id"] in inventory.members.get(a["id"], set())}
                        albums[str(album_id)] = self.config.frame_album
                        filename = inventory.recovery_name(identity[0]) if inventory.find_media(identity[0]) else identity[0]
                        obsolete = list(pending.get("obsolete", [old] if old else [])) if resuming else ([old] if old else [])
                        if resuming and replacement and replacement not in obsolete:
                            obsolete.append(replacement)
                        pending = {"obsolete": obsolete, "asset_id": photo["id"], "old": old, "filename": filename,
                                   "sha256": identity[1], "albums": albums}
                        self.save_device_state({"native_scope": scope, "pending_repair": pending})
                        if item.get("kind") == "video":
                            self.frame.import_video(album_id, filename, content, cache.cover_path(item), self.config.user_id, verify=False)
                        else:
                            self.frame.import_photo(album_id, filename, content, self.config.user_id, verify=False)
                        inventory = self.frame.inventory()
                    replacement = self._finish_repair(pending, item, inventory, verified, managed, scope, checked)
                    checked[(device_id, replacement["id"], replacement["path"], item["device_sha256"],
                             item.get("preview_sha256"), item.get("kind") == "video")] = []
                    pending = None
                log.info("Repair item %s restored; %.1fs", photo["id"], time.monotonic() - item_started)
                result["repaired"] += 1
                cache.discard(item)
                inventory = self.frame.inventory()
            except (SyncError, OSError) as error:
                result["errors"][photo["id"]] = str(error)
                log.error("Repair failed for %s: %s; %.1fs", photo["id"], error, time.monotonic() - item_started)
                # Never replace an unfinished transfer's durable journal.
                if not dry_run and self.device_state().get("pending_repair"):
                    break
        result["checked"] = len(checked) - checks_before
        log.info("Repair album %s: %d healthy, %d problems, %d repaired, %d errors%s; %.1fs",
                 self.config.frame_album, result["healthy"], result["problems"], result["repaired"], len(result["errors"]),
                 " (dry run)" if dry_run else "", time.monotonic() - started)
        return result
