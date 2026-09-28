# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

import argparse
import json
import logging
import signal
import threading
import time
from dataclasses import dataclass

from .cache import atomic_write, locked
from .config import Config, FrameUnavailable, SYNC_MODES, SyncError
from .sync import Synchronizer
from .multi_sync import MultiSynchronizer
from .timing import timed_operation

log = logging.getLogger(__name__)
HEARTBEAT_INTERVAL = 10 * 60


@dataclass
class FrameAvailability:
    unavailable_since: float | None = None
    last_warning: float | None = None

    def unavailable(self, *, phase: str, interval: int, elapsed: float):
        now = time.monotonic()
        if self.unavailable_since is None:
            self.unavailable_since = now
        report = self.last_warning is None or now - self.last_warning >= HEARTBEAT_INTERVAL
        log.log(logging.WARNING if report else logging.DEBUG,
                "Frame unavailable; sync cycle %s; retry in %ss; %.1fs", phase, interval, elapsed)
        if report:
            self.last_warning = now

    def recovered(self) -> bool:
        if self.unavailable_since is None:
            return False
        log.info("Frame connection restored after %.1fs; resuming sync",
                 time.monotonic() - self.unavailable_since)
        self.unavailable_since = self.last_warning = None
        return True


def has_changes(result: dict) -> bool:
    return any(result.get(key, 0) for key in ("uploaded", "linked", "removed"))


def sync_cycle(sync, *, play: bool = True, report_unchanged: bool = True,
               availability: FrameAvailability | None = None) -> dict:
    """Time all sources and destinations, excluding the wait between cycles."""
    started = time.monotonic()
    phase = "skipped"
    try:
        sync.frame.check_available()
        phase = "interrupted"
        sync.refresh()
        result = sync.sync_album(play=play)
    except FrameUnavailable:
        if availability is not None:
            availability.unavailable(phase=phase, interval=sync.config.sync_interval,
                                     elapsed=time.monotonic() - started)
        else:
            log.debug("Sync cycle %s: frame unavailable; %.1fs", phase, time.monotonic() - started)
        raise
    except (SyncError, OSError):
        log.error("Sync cycle failed; %.1fs", time.monotonic() - started)
        raise
    if availability is not None and availability.recovered():
        report_unchanged = True
    if result.get("errors"):
        log.warning("Sync cycle finished with %d errors; %.1fs",
                    len(result["errors"]), time.monotonic() - started)
    else:
        level = logging.INFO if report_unchanged or has_changes(result) else logging.DEBUG
        linked = f", {result['linked']} linked" if result.get("linked") else ""
        log.log(level, "Sync cycle complete: %d items, %d uploaded, %d removed%s; %.1fs",
                result["items"], result["uploaded"], result["removed"], linked,
                time.monotonic() - started)
    return result


def run(sync):
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    first = True
    last_report = None
    availability = FrameAvailability()
    with locked(sync.config.data_dir):
        log.info("Service started; sync interval=%ss", sync.config.sync_interval)
        try:
            while not stop.is_set():
                report_unchanged = (availability.unavailable_since is not None or last_report is None
                                    or time.monotonic() - last_report >= HEARTBEAT_INTERVAL)
                try:
                    result = sync_cycle(sync, play=first, report_unchanged=report_unchanged,
                                        availability=availability)
                    if not result.get("errors") and (report_unchanged or has_changes(result)):
                        last_report = time.monotonic()
                    if result["items"]:
                        first = False
                except FrameUnavailable:
                    pass  # Already reported once by the cycle; retry after the usual wait.
                except (SyncError, OSError) as error:
                    log.error("Sync failed; will retry: %s", error)
                stop.wait(sync.config.sync_interval)
        finally:
            log.info("Service stopped")
    # The frame continues its native slideshow after this process exits.


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="tfs", description="Divoom Times Frame Sync — public albums from multiple photo services")
    root.add_argument("--env-file", default=".env")
    root.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO",
                      help="Application log detail (DEBUG includes all cycles and operation timings)")
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
    # Restrict DEBUG to our namespace: HTTP library debug output can expose
    # sharing links and signed URLs.
    logging.getLogger("timesframesync").setLevel(getattr(logging, args.log_level))
    log.setLevel(getattr(logging, args.log_level))
    started = time.monotonic()
    try:
        config = Config.load(args.env_file, sync_mode=getattr(args, "sync_mode", None), sources_file=args.sources_file)
        sync = MultiSynchronizer(config) if config.sources_file else Synchronizer(config)
        if args.command == "status" or (args.command == "sync" and args.dry_run):
            with timed_operation(log, "Reading source and frame status", level=logging.INFO) as operation:
                if isinstance(sync, MultiSynchronizer):
                    result = sync.status()
                    print(json.dumps(result, indent=2, ensure_ascii=False))
                    if result["errors"]:
                        operation.status = "finished with errors"
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
                    if args.command == "cache":
                        with timed_operation(log, "Preparing source cache", level=logging.INFO) as operation:
                            result = sync.refresh(download=True)
                            if result.get("errors"):
                                operation.status = "finished with errors"
                    else:
                        result = sync_cycle(sync)
                    if result.get("errors"):
                        return 1
                elif args.command == "repair":
                    with timed_operation(log, "Repair cycle", level=logging.INFO) as operation:
                        sync.refresh()
                        result = sync.repair_album(dry_run=args.dry_run)
                        print(json.dumps(result, indent=2, ensure_ascii=False))
                        if result["errors"] or (args.dry_run and result["problems"]):
                            operation.status = "finished with errors" if result["errors"] else "problems found"
                            return 1
                elif args.command == "snapshot":
                    path = sync.config.data_dir / "snapshot.webp"
                    atomic_write(path, sync.frame.snapshot())
                    print(path)
                elif args.command == "restore":
                    sync.restore_previous()
        return 0
    except (SyncError, OSError) as error:
        log.error("Command %s failed: %s; %.1fs", args.command, error, time.monotonic() - started)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
