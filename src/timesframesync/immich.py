# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

from typing import Any
from uuid import UUID
from pathlib import Path

import requests

from .config import Config, SyncError, parse_share_url


from .source import Album, Asset


class Immich:
    def __init__(self, config: Config):
        self.base_url, self.params = parse_share_url(config.share_url)
        self.password = config.password
        self.session = requests.Session()

    def _request(self, path: str, *, body=None, params=None, stream=False):
        try:
            response = self.session.request(
                "POST" if body is not None else "GET",
                self.base_url + path,
                params={**self.params, **(params or {})},
                json=body,
                timeout=(10, 90),
                stream=stream,
            )
        except requests.RequestException:
            raise SyncError(f"Immich request failed: {path} (connection or timeout)") from None
        if not response.ok:
            response.close()
            raise SyncError(f"Immich {path}: HTTP {response.status_code}")
        return response

    def _json(self, path: str, **kwargs) -> Any:
        try:
            return self._request(path, **kwargs).json()
        except ValueError:
            raise SyncError(f"Immich {path}: invalid JSON response") from None

    def album(self) -> Album:
        # Password login sets immich_shared_link_token cookies in Immich >= 2.6.
        self.session.cookies.clear()
        link = self._json("/shared-links/login", body={"password": self.password}) if self.password else self._json("/shared-links/me")
        info = link.get("album")
        if link.get("type") != "ALBUM" or not info:
            raise SyncError("The shared link must point to an album")
        params = {"albumId": info["id"], "withStacked": "false", "order": "asc"}
        buckets = self._json("/timeline/buckets", params=params)
        ids: list[str] = []
        for bucket in buckets:
            contents = self._json("/timeline/bucket", params={**params, "timeBucket": bucket["timeBucket"]})
            if not isinstance(contents, dict) or not isinstance(contents.get("id"), list):
                raise SyncError("Unsupported Immich timeline response; expected v3 column arrays")
            bucket_ids = contents["id"]
            is_image = contents.get("isImage", [])
            if len(bucket_ids) != bucket["count"] or len(is_image) != len(bucket_ids):
                raise SyncError("Album changed during listing; retry sync")
            ids.extend(bucket_ids)
        if len(ids) != len(set(ids)) or len(ids) != info["assetCount"]:
            raise SyncError("Incomplete or changing album listing; refusing to replace local playlist")
        assets = []
        for asset_id in ids:
            try:
                UUID(asset_id)
            except (ValueError, TypeError):
                raise SyncError("Invalid asset ID returned by Immich") from None
            meta = self._json(f"/assets/{asset_id}")
            if meta.get("type") not in {"IMAGE", "VIDEO"} or meta.get("isTrashed"):
                raise SyncError("Album changed during listing; retry sync")
            kind = "video" if meta["type"] == "VIDEO" else "photo"
            assets.append(Asset(asset_id, meta["updatedAt"], meta.get("originalFileName", asset_id), kind))
        return Album(info["id"], info["albumName"], assets)

    def preview(self, asset_id: str) -> bytes:
        # Immich decodes HEIC/RAW and applies its edits to the preview.
        # This avoids shipping original files much larger than the 800x1280 panel.
        response = self._request(f"/assets/{asset_id}/thumbnail", params={"size": "preview"})
        if not response.headers.get("Content-Type", "").startswith("image/"):
            raise SyncError("Immich returned a non-image preview")
        return response.content

    def download_video(self, asset_id: str, destination: Path) -> None:
        # The playback endpoint works with shared-link view permission and
        # returns Immich's encoded version, or the original when none exists.
        try:
            with self._request(f"/assets/{asset_id}/video/playback", stream=True) as response:
                if not response.headers.get("Content-Type", "").lower().startswith("video/"):
                    raise SyncError("Immich returned a non-video playback response")
                with destination.open("wb") as file:
                    for chunk in response.iter_content(1024 * 1024):
                        file.write(chunk)
                if not destination.stat().st_size:
                    raise SyncError("Immich returned an empty video")
        except requests.RequestException:
            raise SyncError("Immich video download interrupted; retry") from None
