# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

"""Refresh independent providers and reconcile each target against their union."""
from dataclasses import replace
import hashlib
import json
import logging
import time

from .cache import Cache, atomic_write
from .config import Config, FrameUnavailable, SyncError
from .frame import Frame
from .providers import create_source
from .sources_config import load_sources
from .sync import Synchronizer

log = logging.getLogger(__name__)


class MultiSynchronizer:
    def __init__(self, config: Config):
        self.config = config
        self.specs = load_sources(config)
        self.frame = Frame(config)
        self.clients = {s.id: create_source(s, config) for s in self.specs}
        self.caches = {s.id: Cache(replace(config, data_dir=config.data_dir / "sources" / s.id)) for s in self.specs}
        self.groups: dict[str, list] = {}
        for spec in self.specs:
            self.groups.setdefault(spec.target_album, []).append(spec)
        self.ready: set[str] | None = None
        self.errors: dict[str, str] = {}
        self.root_state = Synchronizer(replace(config, share_url=""))

    def refresh(self, *, download: bool = False) -> dict:
        self.ready, self.errors = set(), {}
        results = {}
        for spec in self.specs:
            started = time.monotonic()
            try:
                album = self.clients[spec.id].album()
                # Prefix remote IDs by provider so changing a source's provider
                # cannot accidentally reuse an unrelated cache with equal IDs.
                album = replace(album, id=f"{spec.provider}:{album.id}")
                result = self.caches[spec.id].sync(self.clients[spec.id], album, download=download)
                self.ready.add(spec.id)
                results[spec.id] = result
                log.debug("Source %s (%s), album %s: listed %d photos, %d videos; %d prefetched, %d videos skipped; %.1fs",
                         spec.id, spec.provider, album.name, result["photos"], result["videos"], result["downloaded"], result["skipped_videos"],
                         time.monotonic() - started)
            except (SyncError, OSError) as error:
                self.errors[spec.id] = str(error)
                log.error("Source %s failed: %s; %.1fs", spec.id, error, time.monotonic() - started)
        return {"sources": results, "errors": dict(self.errors)}

    def target_sync(self, name: str, specs: list, *, migrate: bool = True) -> Synchronizer:
        key = hashlib.sha256(name.encode()).hexdigest()[:24]
        target = Synchronizer(replace(self.config, share_url="", frame_album=name,
                                      data_dir=self.config.data_dir / "targets" / key))
        target.frame = self.frame
        manifests = [(s, self.caches[s.id].read()) for s in specs]
        photos = []
        for spec, manifest in manifests:
            for photo in manifest["photos"]:
                photo_id = f"{spec.id}:{photo['id']}"
                photos.append({**photo, "id": photo_id})
                target.photo_sources[photo_id] = (self.caches[spec.id], self.clients[spec.id], photo)
        manifest = {"version": 1, "album_id": f"multi:{key}", "album_name": name, "photos": photos}
        atomic_write(target.cache.path, json.dumps(manifest, indent=2).encode())
        # One-time import of ownership from the original single-Immich layout.
        # Verify source, physical device, target album and host before reusing it.
        if migrate and not (target.config.data_dir / "device-state.json").exists():
            legacy = self.root_state.device_state()
            scope = legacy.get("native_scope", {})
            source_ids = {m["album_id"] for _, m in manifests}
            if "immich:" + str(scope.get("source_album_id")) in source_ids:
                target_id = self.frame.inventory().album(name)["id"]
                device_id = self.frame.info()["clock"].get("DeviceId")
                if scope.get("album_id") == target_id and scope.get("device_id") == device_id:
                    target.save_device_state({"native_scope": {**scope, "source_album_id": manifest["album_id"]},
                                              "managed_photos": legacy.get("managed_photos", {}),
                                              "previous_clock_id": legacy.get("previous_clock_id")})
        return target

    def sync_album(self, *, play: bool = True) -> dict:
        if self.ready is None:
            raise SyncError("Refresh all sources before syncing to the frame")
        results = []
        errors = dict(self.errors)
        # One native album can be active. Prefer DIVOOM_ALBUM, then config order.
        names = sorted(self.groups, key=lambda name: name != self.config.frame_album)
        played = False
        try:
            for name in names:
                specs = self.groups[name]
                if any(s.id not in self.ready for s in specs):
                    log.warning("Target %s skipped: not every source has a complete snapshot", name)
                    continue
                started = time.monotonic()
                try:
                    target = self.target_sync(name, specs)
                    result = target.sync_album(play=play and not played)
                    results.append(result)
                    if play and not played and result["items"]:
                        played = True
                        previous = target.device_state().get("previous_clock_id")
                        if previous and not self.root_state.device_state().get("previous_clock_id"):
                            self.root_state.save_device_state({"previous_clock_id": previous})
                except FrameUnavailable:
                    # Every destination uses the same frame. Stop immediately,
                    # preserving any completed operations in their journals.
                    raise
                except (SyncError, OSError) as error:
                    errors["target:" + name] = str(error)
                    log.error("Target %s failed: %s; %.1fs", name, error, time.monotonic() - started)
        finally:
            # A cached snapshot alone cannot authorize another reconciliation.
            self.ready = None
        return {"albums": results, **{key: sum(r[key] for r in results) for key in
                                      ("items", "photos", "videos", "downloaded", "uploaded", "linked", "removed", "retained", "checked", "skipped")},
                "errors": errors}

    def repair_album(self, *, dry_run: bool = False) -> dict:
        if self.ready is None:
            raise SyncError("Refresh all sources before repairing the frame")
        results, errors, checked = [], dict(self.errors), {}
        try:
            for name, specs in self.groups.items():
                if any(s.id not in self.ready for s in specs):
                    log.warning("Repair target %s skipped: incomplete source listing", name)
                    continue
                started = time.monotonic()
                try:
                    result = self.target_sync(name, specs, migrate=not dry_run).repair_album(dry_run=dry_run, checked=checked)
                    results.append(result)
                    errors.update({f"{name}:{key}": value for key, value in result["errors"].items()})
                except (SyncError, OSError) as error:
                    errors["target:" + name] = str(error)
                    log.error("Repair target %s failed: %s; %.1fs", name, error, time.monotonic() - started)
        finally:
            self.ready = None
        return {"albums": results, "dry_run": dry_run, "errors": errors,
                **{key: sum(r[key] for r in results) for key in ("items", "checked", "healthy", "problems", "repaired", "downloaded")}}

    def status(self) -> dict:
        inventory = self.frame.inventory()
        sources, errors = [], {}
        for spec in self.specs:
            try:
                album = self.clients[spec.id].album()
                sources.append({"id": spec.id, "provider": spec.provider, "album": album.name,
                                "photos": album.photo_count, "videos": album.video_count, "videos_skipped": album.skipped,
                                "target_album": spec.target_album})
            except SyncError as error:
                errors[spec.id] = str(error)
        targets = []
        for name in self.groups:
            try:
                album = inventory.album(name)
                members = inventory.members.get(album["id"], set())
                targets.append({**album, "photos": len(members - inventory.video_ids),
                                "videos": len(members & inventory.video_ids)})
            except SyncError as error:
                errors["target:" + name] = str(error)
        return {"sync_mode": self.config.sync_mode, "sources": sources, "targets": targets,
                "errors": errors, "frame": self.frame.info()}

    def restore_previous(self):
        self.root_state.frame = self.frame
        self.root_state.restore_previous()
