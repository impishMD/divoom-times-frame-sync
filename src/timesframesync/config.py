# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import os

from dotenv import load_dotenv


class SyncError(Exception):
    """An actionable error safe to print without credentials."""


class FrameUnavailable(SyncError):
    """A transport failure, distinct from an API rejection or corrupt response."""


SYNC_MODES = ("mirror", "append")


@dataclass(frozen=True)
class Config:
    share_url: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    host: str = ""
    port: int = 9000
    token: str = field(default="", repr=False)
    data_dir: Path = Path("data")
    sync_interval: int = 300
    frame_album: str = "Photos"
    user_id: int = 0
    image_fit: str = "contain"
    sync_mode: str = "mirror"
    sources_file: Path | None = None

    @classmethod
    def load(cls, env_file: str = ".env", *, sync_mode: str | None = None, sources_file: str | None = None):
        load_dotenv(env_file)
        source_path = sources_file if sources_file is not None else os.environ.get("SOURCES_FILE")
        try:
            config = cls(
                sources_file=Path(source_path).expanduser().resolve() if source_path else None,
                share_url=os.environ.get("IMMICH_SHARE_URL", ""),
                password=os.environ.get("IMMICH_SHARE_PASSWORD", ""),
                host=os.environ.get("DIVOOM_HOST", ""),
                port=int(os.environ.get("DIVOOM_PORT", "9000")),
                token=os.environ.get("DIVOOM_TOKEN", ""),
                data_dir=Path(os.environ.get("DATA_DIR", "data")).expanduser().resolve(),
                sync_interval=int(os.environ.get("SYNC_INTERVAL", "300")),
                frame_album=os.environ.get("DIVOOM_ALBUM", "Photos"),
                user_id=int(os.environ.get("DIVOOM_USER_ID", "0")),
                image_fit=os.environ.get("IMAGE_FIT", "contain"),
                sync_mode=sync_mode if sync_mode is not None else os.environ.get("SYNC_MODE", "mirror"),
            )
        except ValueError:
            raise SyncError("Port, user ID and interval must be integers") from None
        if not config.host or "/" in config.host or ":" in config.host:
            raise SyncError("Set DIVOOM_HOST to the frame's IPv4 address or hostname")
        if not 1 <= config.port <= 65535:
            raise SyncError("DIVOOM_PORT must be between 1 and 65535")
        if config.sync_interval < 10:
            raise SyncError("Intervals must be at least 10 seconds")
        if not config.frame_album.strip() or not 0 <= config.user_id < 2**31:
            raise SyncError("Set a nonempty DIVOOM_ALBUM and a valid DIVOOM_USER_ID")
        if config.image_fit not in {"contain", "cover"}:
            raise SyncError("IMAGE_FIT must be contain or cover")
        if config.sync_mode not in SYNC_MODES:
            raise SyncError("SYNC_MODE must be mirror or append")
        if config.token and not config.token.isdigit():
            raise SyncError("DIVOOM_TOKEN must be numeric")
        if config.sources_file is None:
            parse_share_url(config.share_url)
        return config


def parse_share_url(url: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username:
        raise SyncError("IMMICH_SHARE_URL must be a full Immich shared album URL")
    path = parts.path.rstrip("/")
    if "/share/" not in path:
        raise SyncError("IMMICH_SHARE_URL must contain /share/<key>")
    prefix, key = path.rsplit("/share/", 1)
    if not key or "/" in key:
        raise SyncError("Invalid shared album key")
    return f"{parts.scheme}://{parts.netloc}{prefix}/api", {"key": key}
