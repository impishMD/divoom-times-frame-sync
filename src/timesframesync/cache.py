# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

from contextlib import contextmanager
import fcntl
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import tempfile

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Config, SyncError
from .source import Album, Source
from .timing import timed, timed_operation
from .frame import device_photo
from .video import VIDEO_REVISION, file_digest, make_cover, require_ffmpeg, transcode

log = logging.getLogger(__name__)


@timed(log, "Writing local file atomically", level=logging.DEBUG)
def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def locked(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("Another sync process is already using DATA_DIR") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def render_photo(content: bytes, fit: str) -> bytes:
    try:
        with Image.open(BytesIO(content)) as source:
            photo = ImageOps.exif_transpose(source).convert("RGB")
            size = (800, 1280)
            if fit == "cover":
                output = ImageOps.fit(photo, size, method=Image.Resampling.LANCZOS)
            else:
                output = ImageOps.pad(photo, size, method=Image.Resampling.LANCZOS, color="black")
            buffer = BytesIO()
            output.save(buffer, "JPEG", quality=90, optimize=True)
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise SyncError("Unable to decode the source image preview") from None


class Cache:
    photo_name_pattern = r"(?:[0-9a-f]{24}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})-[0-9a-f]{16}\.jpg"
    video_stem_pattern = r"[0-9a-f]{24}-[0-9a-f]{16}"
    video_suffixes = (".mp4", ".webp", ".source", ".download", ".part.mp4")
    def __init__(self, config: Config):
        self.directory = config.data_dir
        self.fit = config.image_fit
        self.path = self.directory / "manifest.json"

    @timed(log, "Reading source manifest", level=logging.DEBUG)
    def read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "photos": []}
        try:
            value = json.loads(self.path.read_text())
            if value.get("version") != 1 or not isinstance(value.get("photos"), list):
                raise ValueError
            return value
        except (OSError, ValueError):
            raise SyncError("Invalid cache manifest; restore it or choose a fresh DATA_DIR") from None

    def photo_path(self, photo: dict) -> Path:
        """Resolve a managed media path (legacy name retained for manifests)."""
        filename = photo.get("file", "")
        kind = photo.get("kind", "photo")
        pattern = "videos/" + self.video_stem_pattern + r"\.mp4" if kind == "video" else "photos/" + self.photo_name_pattern
        if kind not in {"photo", "video"} or not isinstance(filename, str) or not re.fullmatch(pattern, filename):
            raise SyncError("Invalid temporary media path")
        path = self.directory / filename
        if path.is_symlink() or path.parent.is_symlink():
            raise SyncError("Temporary photo paths must not be symlinks")
        return path

    def video_paths(self, photo: dict) -> dict[str, Path]:
        path = self.photo_path(photo)
        paths = {suffix: path.with_suffix(suffix) for suffix in self.video_suffixes}
        if any(p.is_symlink() for p in paths.values()):
            raise SyncError("Temporary video paths must not be symlinks")
        return paths

    def cover_path(self, photo: dict) -> Path:
        return self.video_paths(photo)[".webp"]

    @staticmethod
    def device_identity(photo: dict) -> tuple[str, str] | None:
        digest = photo.get("device_sha256")
        filename = photo.get("device_filename")
        if digest is None and filename is None:
            return None  # Manifest created before fingerprints were recorded.
        video = photo.get("kind", "photo") == "video"
        prefix, suffix = ("vi-", ".mp4") if video else ("im-", ".webp")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or filename != prefix + digest[:24] + suffix:
            raise SyncError("Invalid media fingerprint in manifest")
        if video and not re.fullmatch(r"[0-9a-f]{64}", str(photo.get("preview_sha256", ""))):
            raise SyncError("Invalid video cover fingerprint in manifest")
        return filename, digest

    def prepare(self, source: Source | None, photo: dict) -> tuple[str, bytes | Path, bool]:
        """Materialize only the current item; retain it if upload fails."""
        if photo.get("kind") == "video":
            return self.prepare_video(source, photo)
        path = self.photo_path(photo)
        downloaded = not path.is_file()
        if downloaded:
            if source is None:
                raise SyncError("Photo needs downloading; refresh its source before syncing")
            with timed_operation(log, "Downloading photo"):
                preview = source.preview(photo["id"])
        with timed_operation(log, "Preparing photo"):
            if downloaded:
                image = render_photo(preview, self.fit)
                atomic_write(path, image)
            else:
                image = path.read_bytes()
                if photo.get("sha256") and hashlib.sha256(image).hexdigest() != photo["sha256"]:
                    raise SyncError("Temporary photo checksum mismatch")
            filename, content = device_photo(image)
            photo.update(sha256=hashlib.sha256(image).hexdigest(), device_filename=filename,
                         device_sha256=hashlib.sha256(content).hexdigest())
        return filename, content, downloaded

    def prepare_video(self, source: Source | None, photo: dict) -> tuple[str, Path, bool]:
        paths = self.video_paths(photo)
        movie, cover, original = paths[".mp4"], paths[".webp"], paths[".source"]
        downloaded = False
        prepared = movie.is_file()
        if not prepared:
            require_ffmpeg()
            movie.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not original.is_file():
                download = getattr(source, "download_video", None)
                if not callable(download):
                    raise SyncError("Video needs downloading from a source with video support")
                with timed_operation(log, "Downloading video", announce=True):
                    partial = paths[".download"]
                    try:
                        partial.touch(mode=0o600)
                        download(photo["id"], partial)
                        os.replace(partial, original)
                    finally:
                        partial.unlink(missing_ok=True)
                downloaded = True
            with timed_operation(log, "Preparing native MP4 video", announce=True):
                partial = paths[".part.mp4"]
                try:
                    partial.touch(mode=0o600)
                    details = transcode(original, partial, self.fit)
                    os.replace(partial, movie)
                    photo.update(details)
                finally:
                    partial.unlink(missing_ok=True)
        with timed_operation(log, "Checking prepared video checksum"):
            digest = file_digest(movie)
            if prepared and photo.get("device_sha256") and digest != photo["device_sha256"]:
                raise SyncError("Temporary video checksum mismatch")
        if not prepared:
            cover.unlink(missing_ok=True)
        if not cover.is_file():
            require_ffmpeg()
            with timed_operation(log, "Preparing video cover"):
                atomic_write(cover, make_cover(movie))
        with timed_operation(log, "Checking prepared video cover checksum"):
            cover_digest = file_digest(cover)
            if prepared and photo.get("preview_sha256") and cover_digest != photo["preview_sha256"]:
                raise SyncError("Temporary video cover checksum mismatch")
        filename = "vi-" + digest[:24] + ".mp4"
        photo.update(device_filename=filename, device_sha256=digest, preview_sha256=cover_digest)
        # The complete converted file and cover suffice for upload retries.
        original.unlink(missing_ok=True)
        return filename, movie, downloaded

    @timed(log, "Saving media fingerprint", level=logging.DEBUG)
    def remember(self, photo: dict):
        """Commit a fingerprint before deleting bytes, preserving other entries."""
        manifest = self.read()
        for index, item in enumerate(manifest["photos"]):
            if item["id"] == photo["id"] and item["revision"] == photo["revision"]:
                manifest["photos"][index] = dict(photo)
                atomic_write(self.path, json.dumps(manifest, indent=2, ensure_ascii=False).encode())
                return
        raise SyncError("Source manifest changed during upload; retry")

    def discard(self, photo: dict):
        paths = self.video_paths(photo).values() if photo.get("kind") == "video" else [self.photo_path(photo)]
        if any(path.exists() for path in paths):
            with timed_operation(log, "Removing temporary %s files", photo.get("kind", "photo")):
                for path in paths:
                    path.unlink(missing_ok=True)

    @timed(log, "Cleaning obsolete temporary media", level=logging.DEBUG)
    def clean_unused(self, photos: list[dict]):
        active = {self.photo_path(photo) for photo in photos}
        directory = self.directory / "photos"
        if directory.is_symlink():
            raise SyncError("Temporary photo directory must not be a symlink")
        for path in directory.glob("*.jpg"):
            if re.fullmatch(self.photo_name_pattern, path.name) and path not in active:
                path.unlink()
        directory = self.directory / "videos"
        if directory.is_symlink():
            raise SyncError("Temporary video directory must not be a symlink")
        active_video = {p for photo in photos if photo.get("kind") == "video" for p in self.video_paths(photo).values()}
        suffixes = "(?:" + "|".join(re.escape(s) for s in self.video_suffixes) + ")"
        for path in directory.glob("*"):
            if re.fullmatch(self.video_stem_pattern + suffixes, path.name) and path not in active_video:
                path.unlink()

    def sync(self, source: Source, album: Album, *, download: bool = True) -> dict:
        old_manifest = self.read()
        previous = {photo["id"]: photo for photo in old_manifest["photos"]} if old_manifest.get("album_id") == album.id else {}
        photos = []
        downloaded = 0
        for asset in album.photos:
            if asset.kind not in {"photo", "video"}:
                raise SyncError("Unsupported source media kind")
            recipe = VIDEO_REVISION if asset.kind == "video" else "800x1280|jpeg90-v1"
            revision = hashlib.sha256(f"{asset.revision}|{self.fit}|{recipe}".encode()).hexdigest()
            old = previous.get(asset.id, {})
            if old.get("revision") == revision:
                photo = dict(old)
            else:
                safe_id = hashlib.sha256(f"{album.id}\0{asset.id}".encode()).hexdigest()[:24]
                directory, extension = ("videos", "mp4") if asset.kind == "video" else ("photos", "jpg")
                photo = {"id": asset.id, "revision": revision, "kind": asset.kind,
                         "file": f"{directory}/{safe_id}-{revision[:16]}.{extension}"}
            self.photo_path(photo)
            if download:
                _, _, fetched = self.prepare(source, photo)
                downloaded += fetched
            photos.append(photo)
        manifest = {"version": 1, "album_id": album.id, "album_name": album.name, "photos": photos}
        # Album enumeration is complete before this call. Explicit prefetches
        # commit only after every download; normal sync stages metadata only.
        atomic_write(self.path, json.dumps(manifest, indent=2, ensure_ascii=False).encode())
        self.clean_unused(photos)
        return {"items": len(photos), "photos": album.photo_count, "videos": album.video_count,
                "downloaded": downloaded, "removed": len(previous.keys() - {p["id"] for p in photos}), "skipped_videos": album.skipped}
