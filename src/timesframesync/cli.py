# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

import argparse
import json
import logging
import signal
import threading
import time

from .cache import atomic_write, locked
from .config import Config, SYNC_MODES, SyncError
from .sync import Synchronizer
from .multi_sync import MultiSynchronizer

log = logging.getLogger(__name__)


def sync_cycle(sync, *, play: bool = True) -> dict:
    """Time all sources and destinations, excluding the wait between cycles."""
    started = time.monotonic()
    try:
        sync.refresh()
        result = sync.sync_album(play=play)
    except (SyncError, OSError):
        log.error("Sync cycle failed; %.1fs", time.monotonic() - started)
        raise
    if result.get("errors"):
        log.warning("Sync cycle finished with %d errors; %.1fs",
                    len(result["errors"]), time.monotonic() - started)
    else:
        log.info("Sync cycle complete; %.1fs", time.monotonic() - started)
    return result


def run(sync):
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    first = True
    with locked(sync.config.data_dir):
        while not stop.is_set():
            try:
                result = sync_cycle(sync, play=first)
                if result["items"]:
                    first = False
            except (SyncError, OSError) as error:
                log.error("Sync failed; will retry: %s", error)
            stop.wait(sync.config.sync_interval)
    # The frame continues its native slideshow after this process exits.


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="tfs", description="Divoom Times Frame Sync — public albums from multiple photo services")
    root.add_argument("--env-file", default=".env")
    root.add_argument("--sources-file", help="TOML list of albums (overrides SOURCES_FILE)")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Read every source and native frame album status")
    commands.add_parser("cache", help="Prepare all photos and supported videos without changing the frame")
    sync = commands.add_parser("sync", help="Sync all configured albums and start autonomous playback")
    sync.add_argument("--dry-run", action="store_true", help="Read status without writing to the frame")
    repair = commands.add_parser("repair", help="Check full file contents and recover damaged or missing media")
    repair.add_argument("--dry-run", action="store_true", help="Check contents and report problems without changing the frame")
    daemon = commands.add_parser("run", help="Periodically update all albums; playback is autonomous")
    for command in (sync, daemon):
        command.add_argument("--sync-mode", choices=SYNC_MODES,
                             help="mirror: remove missing managed media; append: keep them (overrides SYNC_MODE)")
    commands.add_parser("snapshot", help="Save a screenshot to DATA_DIR/snapshot.webp")
    commands.add_parser("restore", help="Select the previously active Divoom clock or album")
    return root


def main() -> int:
    args = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.load(args.env_file, sync_mode=getattr(args, "sync_mode", None), sources_file=args.sources_file)
        sync = MultiSynchronizer(config) if config.sources_file else Synchronizer(config)
        if args.command == "status" or (args.command == "sync" and args.dry_run):
            if isinstance(sync, MultiSynchronizer):
                result = sync.status()
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return 1 if result["errors"] else 0
            else:
                album = sync.immich.album()
                inventory = sync.frame.inventory()
                target = inventory.album(sync.config.frame_album)
                print(json.dumps({"sync_mode": sync.config.sync_mode,
                                  "immich_album": album.name, "photos": album.photo_count, "videos": album.video_count, "videos_skipped": album.skipped,
                                  "frame_album": target,
                                  "frame_album_photos": len(inventory.members.get(target["id"], set()) - inventory.video_ids),
                                  "frame_album_videos": len(inventory.members.get(target["id"], set()) & inventory.video_ids),
                                  "frame": sync.frame.info()}, indent=2, ensure_ascii=False))
        elif args.command == "run":
            run(sync)
        else:
            with locked(sync.config.data_dir):
                if args.command in {"cache", "sync"}:
                    result = sync.refresh(download=True) if args.command == "cache" else sync_cycle(sync)
                    if result.get("errors"):
                        return 1
                elif args.command == "repair":
                    sync.refresh()
                    result = sync.repair_album(dry_run=args.dry_run)
                    print(json.dumps(result, indent=2, ensure_ascii=False))
                    if result["errors"] or (args.dry_run and result["problems"]):
                        return 1
                elif args.command == "snapshot":
                    path = sync.config.data_dir / "snapshot.webp"
                    atomic_write(path, sync.frame.snapshot())
                    print(path)
                elif args.command == "restore":
                    sync.restore_previous()
                    log.info("Previous Divoom display restored")
        return 0
    except (SyncError, OSError) as error:
        log.error("%s", error)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
