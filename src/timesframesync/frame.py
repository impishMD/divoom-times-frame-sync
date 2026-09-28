# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

import json
import secrets
import sqlite3
import hashlib
import logging
from contextlib import ExitStack
from dataclasses import dataclass, field
from io import BytesIO

from PIL import Image
import time
from pathlib import Path, PurePosixPath

import requests

from .config import Config, FrameUnavailable, SyncError
from .timing import timed, timed_operation

log = logging.getLogger(__name__)
PROBE_TIMEOUT = (3, 3)
NETWORK_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)


def validate_filename(filename: str):
    # Native cover generation has a small shell-command buffer.
    if len(filename) > 32 or not filename or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for c in filename):
        raise SyncError("Device filenames must use safe ASCII characters and be at most 32 bytes")


class FileMultipart:
    """Fixed-length multipart body; requests reads files in bounded chunks."""
    def __init__(self, metadata: dict, files: list[tuple[str, Path]]):
        self.stack = ExitStack()
        self.parts = []
        self.index = 0
        self.length = 0
        boundary = "DivoomSync" + secrets.token_hex(16)
        self.content_type = f"multipart/form-data; boundary={boundary}"
        try:
            data = json.dumps(metadata, separators=(",", ":")).encode()
            inputs = [("json", "cmd.json", "application/json", BytesIO(data), len(data))]
            for filename, path in files:
                validate_filename(filename)
                handle = self.stack.enter_context(path.open("rb"))
                inputs.append(("file", filename, "application/octet-stream", handle, path.stat().st_size))
            for name, filename, mime, handle, size in inputs:
                header = (f"--{boundary}\r\n"
                          f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                          f"Content-Type: {mime}\r\nContent-Length: {size}\r\n\r\n").encode()
                self.parts.extend([BytesIO(header), handle, BytesIO(b"\r\n")])
                self.length += len(header) + size + 2
            end = f"--{boundary}--\r\n".encode()
            self.parts.append(BytesIO(end))
            self.length += len(end)
        except BaseException:
            self.stack.close()
            raise

    def __len__(self):
        return self.length

    def read(self, size=64 * 1024):
        # Even callers omitting size cannot load a whole video into memory.
        size = min(size, 1024 * 1024) if size >= 0 else 64 * 1024
        chunks = []
        while size and self.index < len(self.parts):
            chunk = self.parts[self.index].read(size)
            if not chunk:
                self.index += 1
                continue
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stack.close()


def multipart(metadata: dict, filename: str | None = None, content: bytes = b"") -> tuple[bytes, str]:
    """JSON first; the firmware requires a Content-Length for every part."""
    boundary = "DivoomSync" + secrets.token_hex(16)
    parts = [("json", "cmd.json", "application/json", json.dumps(metadata, separators=(",", ":")).encode())]
    if filename is not None:
        # Native album cover generation has a 128-byte shell-command buffer.
        # Long names can abort divoom_app. Keep generated names short and safe.
        validate_filename(filename)
        parts.append(("file", filename, "application/octet-stream", content))
    chunks = []
    for name, leaf, mime, data in parts:
        chunks.append((
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{leaf}"\r\n'
            f"Content-Type: {mime}\r\n"
            f"Content-Length: {len(data)}\r\n\r\n"
        ).encode() + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def device_photo(content: bytes) -> tuple[str, bytes]:
    with Image.open(BytesIO(content)) as photo:
        output = BytesIO()
        photo.convert("RGB").save(output, format="WEBP", quality=90, method=4)
    data = output.getvalue()
    return "im-" + hashlib.sha256(data).hexdigest()[:24] + ".webp", data


@dataclass
class Inventory:
    albums: list[dict]
    photos: dict[int, dict]
    members: dict[int, set[int]]
    video_ids: set[int] = field(default_factory=set)

    def album(self, name: str) -> dict:
        matches = [album for album in self.albums if album["name"] == name and album["type"] == 0]
        if len(matches) != 1:
            raise SyncError(f"Expected exactly one ordinary frame album named {name!r}; found {len(matches)}. Create or rename it in Divoom.")
        return matches[0]

    def find_file(self, filename: str) -> dict | None:
        matches = [photo for photo in self.photos.values() if PurePosixPath(photo["path"]).name == filename]
        if len(matches) > 1:
            raise SyncError(f"Multiple frame photos use {filename}; resolve duplicates before syncing")
        return matches[0] if matches else None

    def find_media(self, filename: str) -> dict | None:
        """Prefer a recovery generation, retaining canonical content identity."""
        import re
        digest = filename[3:27]
        suffix = PurePosixPath(filename).suffix
        pattern = re.compile(r"r" + re.escape(digest) + r"([0-9a-f]{2})" + re.escape(suffix))
        names = [(int(match[1], 16), PurePosixPath(photo["path"]).name)
                 for photo in self.photos.values()
                 if (match := pattern.fullmatch(PurePosixPath(photo["path"]).name))]
        return self.find_file(max(names)[1] if names else filename)

    def recovery_name(self, filename: str) -> str:
        # 32 bytes for WebP, 31 for MP4: respect the firmware command buffer.
        existing = {PurePosixPath(p["path"]).name for p in self.photos.values()}
        generations = [generation for generation in range(256)
                       if f"r{filename[3:27]}{generation:02x}{PurePosixPath(filename).suffix}" in existing]
        generation = max(generations, default=-1) + 1
        if generation > 255:
            raise SyncError("Recovery filename generations exhausted")
        return f"r{filename[3:27]}{generation:02x}{PurePosixPath(filename).suffix}"



def read_inventory(content: bytes) -> Inventory:
    try:
        with sqlite3.connect(":memory:") as db:
            db.deserialize(content)
            db.execute("PRAGMA query_only=ON")
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError
            albums = [{"id": row[0], "type": row[1], "name": row[2]} for row in db.execute("SELECT * FROM album_head")]
            photos = {row[0]: {"id": row[0], "path": row[1]} for row in db.execute("SELECT id,path_name FROM pic_info")}
            members: dict[int, set[int]] = {}
            for album_id, pic_id in db.execute("SELECT album_id,pic_id FROM album_pic"):
                members.setdefault(album_id, set()).add(pic_id)
            columns = {row[1] for row in db.execute("PRAGMA table_info(pic_info)")}
            video_ids = {row[0] for row in db.execute("SELECT id FROM pic_info WHERE pic_video_flag=1")} if "pic_video_flag" in columns else set()
            return Inventory(albums, photos, members, video_ids)
    except (sqlite3.Error, ValueError, TypeError):
        raise SyncError("Cannot read a consistent native album database from this firmware") from None


class Frame:
    def __init__(self, config: Config):
        self.base_url = f"http://{config.host}:{config.port}"
        self.token = int(config.token) if config.token else None
        self.session = requests.Session()
        self.session.trust_env = False  # LAN traffic must not go through a proxy.

    def metadata(self, command: str, **values) -> dict:
        data = {"Command": command, "ReturnCode": 0, **values}
        if self.token is not None:
            data["DeviceToken"] = self.token
            data["LocalToken"] = self.token
        return data

    @timed(log, "Frame API request", level=logging.DEBUG)
    def _post(self, path: str, data, content_type: str, *, timeout=(5, 60)) -> dict:
        try:
            response = self.session.post(self.base_url + path, data=data, headers={"Content-Type": content_type}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
        except NETWORK_ERRORS:
            raise FrameUnavailable("Divoom connection failed or timed out") from None
        except (requests.RequestException, ValueError):
            raise SyncError(f"Divoom {path}: connection, HTTP or JSON error") from None
        if not isinstance(result, dict):
            raise SyncError(f"Divoom {path}: expected a JSON object")
        if result.get("ReturnCode") != 0:
            raise SyncError(f"Divoom rejected {path}: code {result.get('ReturnCode')}, {result.get('ReturnMessage', '')}")
        return result

    def command(self, command: str, **values) -> dict:
        return self._post("/divoom_api", json.dumps(self.metadata(command, **values), separators=(",", ":")).encode(), "application/json")

    def info(self) -> dict:
        return {"config": self.command("Channel/GetConfig"), "clock": self.command("Channel/GetClockInfo")}

    @timed(log, "Checking frame availability")
    def check_available(self):
        """Read the API before source work, without downloading the frame DB."""
        body = json.dumps(self.metadata("Channel/GetClockInfo"), separators=(",", ":")).encode()
        self._post("/divoom_api", body, "application/json", timeout=PROBE_TIMEOUT)

    @timed(log, "Reading frame file", level=logging.DEBUG)
    def fetch_file(self, path: str) -> bytes | None:
        if not path.startswith("/userdata/") or ".." in PurePosixPath(path).parts:
            raise SyncError("Invalid device file path")
        try:
            response = self.session.get(self.base_url + path, timeout=(5, 60))
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.content
        except NETWORK_ERRORS:
            raise FrameUnavailable("Divoom connection failed or timed out") from None
        except requests.RequestException:
            raise SyncError("Cannot read back the file from Divoom") from None

    @timed(log, "Reading frame database", level=logging.DEBUG)
    def inventory(self) -> Inventory:
        # Only read a copy. Never modify or upload the device database.
        for attempt in range(3):
            content = self.fetch_file("/userdata/pic_db.bin")
            if content is None:
                raise SyncError("Native album database is unavailable on this firmware")
            try:
                return read_inventory(content)
            except SyncError:
                if attempt == 2:
                    raise
                time.sleep(0.5)
        raise AssertionError("unreachable")

    @timed(log, "Reading frame file checksum", level=logging.DEBUG)
    def file_digest(self, path: str) -> str | None:
        """Read back a device file without retaining video bytes in memory."""
        if not path.startswith("/userdata/") or ".." in PurePosixPath(path).parts:
            raise SyncError("Invalid device file path")
        started = time.monotonic()
        try:
            with self.session.get(self.base_url + path, stream=True, timeout=(5, 90)) as response:
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                digest = hashlib.sha256()
                received = 0
                last_report = started
                for chunk in response.iter_content(1024 * 1024):
                    digest.update(chunk)
                    received += len(chunk)
                    now = time.monotonic()
                    if now - last_report >= 30:
                        log.debug("Verifying device file: %.1f MiB read; %.1fs elapsed", received / 1024**2, now - started)
                        last_report = now
                return digest.hexdigest()
        except NETWORK_ERRORS:
            raise FrameUnavailable("Divoom connection failed or timed out") from None
        except requests.RequestException:
            raise SyncError("Cannot read back the file from Divoom") from None

    def native_command(self, command: str, **values):
        # /divoom_api ACKs Photo/* without executing them. /upload dispatches
        # the full native command queue, even with only the JSON part.
        body, mime = multipart(self.metadata(command, **values))
        return self._post("/upload", body, mime)

    @timed(log, "Waiting for album membership", level=logging.DEBUG)
    def wait_members(self, album_id: int, *, present=(), absent=()):
        deadline = time.monotonic() + 15
        while True:
            inventory = self.inventory()
            members = inventory.members.get(album_id, set())
            if set(present) <= members and not set(absent) & members:
                return inventory
            if time.monotonic() >= deadline:
                raise SyncError("Frame acknowledged the command but native album membership did not change")
            time.sleep(0.5)

    @timed(log, "Adding existing media to album")
    def add_existing(self, album_id: int, pic_id: int):
        self.native_command("Photo/DevicePhotoToAlbum", ToClockId=album_id,
                            ParentClockId=0, ParentItemId=0, PhotoList=[pic_id])
        self.wait_members(album_id, present=[pic_id])

    @timed(log, "Removing media from album")
    def remove_from_album(self, album_id: int, pic_ids: set[int]):
        if not pic_ids:
            return
        self.native_command("Photo/RemovePhotoFromAlbum", ClockId=album_id,
                            ParentClockId=0, ParentItemId=0, PhotoList=sorted(pic_ids))
        self.wait_members(album_id, absent=pic_ids)

    @timed(log, "Uploading native photo")
    def import_photo(self, album_id: int, filename: str, content: bytes, user_id: int = 0, *, verify: bool = True) -> dict:
        metadata = self.import_metadata(album_id, filename, user_id)
        body, mime = multipart(metadata, filename, content)
        self._post("/upload", body, mime)
        return self.wait_import(album_id, filename, hashlib.sha256(content).hexdigest(), verify=verify)

    def import_metadata(self, album_id: int, filename: str, user_id: int, preview: str = "") -> dict:
        now = int(time.time() * 1000)
        return self.metadata(
            "Photo/LocalAddToAlbum", ClockId=album_id, ParentClockId=0, ParentItemId=0,
            UserId=user_id, SendTime=now, TakingTime=now,
            PhotoX=0, PhotoY=0, PhotoWidth=800, PhotoHeight=1280,
            PhotoIndex=0, PhotoTotalCnt=1, PhotoFlag=secrets.randbelow(2**31 - 1),
            FileName=filename, PreviewFileName=preview, PhotoTitle="",
        )

    @timed(log, "Uploading native video", announce=True)
    def import_video(self, album_id: int, filename: str, content: Path, preview: Path, user_id: int = 0, *, verify: bool = True) -> dict:
        from .video import file_digest
        if not filename.endswith(".mp4"):
            raise SyncError("Native videos must use MP4 filenames")
        preview_name = str(PurePosixPath(filename).with_suffix(".webp"))
        metadata = self.import_metadata(album_id, filename, user_id, preview_name)
        with FileMultipart(metadata, [(filename, content), (preview_name, preview)]) as body:
            self._post("/upload", body, body.content_type, timeout=(5, 300))
        return self.wait_import(album_id, filename, file_digest(content), preview_digest=file_digest(preview), verify=verify)

    @timed(log, "Waiting for native import", level=logging.DEBUG)
    def wait_import(self, album_id: int, filename: str, digest: str, *, preview_digest: str | None = None, verify: bool = True) -> dict:
        deadline = time.monotonic() + 15
        while True:
            inventory = self.inventory()
            photo = inventory.find_file(filename)
            if photo:
                if verify:
                    with timed_operation(log, "Checking imported media contents", announce=preview_digest is not None):
                        if preview_digest is None:
                            content = self.fetch_file(photo["path"])
                            actual = hashlib.sha256(content).hexdigest() if content is not None else None
                        else:
                            actual = self.file_digest(photo["path"])
                        if actual != digest:
                            raise SyncError("Native media read-back verification failed")
                        if preview_digest is not None:
                            cover = str(PurePosixPath(photo["path"]).with_suffix(".webp"))
                            if photo["id"] not in inventory.video_ids or self.file_digest(cover) != preview_digest:
                                raise SyncError("Native video flag or cover verification failed")
                if photo["id"] not in inventory.members.get(album_id, set()):
                    self.add_existing(album_id, photo["id"])
                return photo
            if time.monotonic() >= deadline:
                raise SyncError("Frame acknowledged upload but the photo is absent from its native database")
            time.sleep(0.5)

    @timed(log, "Exiting custom display control", level=logging.DEBUG)
    def restore(self):
        return self.command("Device/ExitCustomControlMode")

    @timed(log, "Selecting frame clock or album", level=logging.DEBUG)
    def select_clock(self, clock_id: int):
        # Paired selection also suppresses a scheduled dial for this period.
        for _ in range(2):
            self.command("Channel/SetClockSelectId", ClockId=clock_id)

    @timed(log, "Selecting native album playback")
    def play_album(self, album_id: int):
        self.restore()
        self.select_clock(album_id)
        if self.command("Channel/GetClockInfo").get("ClockId") != album_id:
            raise SyncError("Photos are synced, but the frame did not select the native album")

    @timed(log, "Capturing frame screenshot", level=logging.INFO)
    def snapshot(self) -> bytes:
        result = self.command("Device/GetScreenSnapshot")
        path = result.get("snapShotPath", "/userdata/snapshot.webp")
        time.sleep(2)
        content = self.fetch_file(path)
        if content is None:
            raise SyncError("Divoom snapshot file not found")
        return content
