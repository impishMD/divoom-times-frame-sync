# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

"""Service-independent album contract. Providers must return complete snapshots."""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Asset:
    id: str
    revision: str
    name: str
    kind: str = "photo"


@dataclass(frozen=True)
class Album:
    id: str
    name: str
    photos: list[Asset]
    skipped: int = 0

    @property
    def photo_count(self) -> int:
        return sum(asset.kind == "photo" for asset in self.photos)

    @property
    def video_count(self) -> int:
        return sum(asset.kind == "video" for asset in self.photos)


class Source(Protocol):
    def album(self) -> Album:
        """Return the complete album, or raise SyncError (never partial data)."""
        ...

    def preview(self, asset_id: str) -> bytes:
        """Return a decodable still image for an asset from the last listing."""
        ...


class VideoSource(Source, Protocol):
    def download_video(self, asset_id: str, destination: Path) -> None:
        """Download a complete video to a local file using bounded memory."""
        ...
