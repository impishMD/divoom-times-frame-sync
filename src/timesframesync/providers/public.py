# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from ..config import SyncError


class PublicSource:
    label = "Public album"

    def __init__(self, url: str):
        self.url = url
        self.session = requests.Session()
        self.images: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs):
        try:
            response = self.session.request(method, url, timeout=(10, 90), **kwargs)
            if not response.ok:
                response.close()
                raise SyncError(f"{self.label}: HTTP {response.status_code}")
            return response
        except requests.RequestException:
            raise SyncError(f"{self.label}: connection or timeout error") from None

    def json(self, url: str, **kwargs):
        try:
            result = self.request("POST", url, **kwargs).json()
            if not isinstance(result, dict) or result.get("serverErrorCode"):
                raise ValueError
            return result
        except ValueError:
            raise SyncError(f"{self.label}: invalid API response") from None

    def preview(self, asset_id: str) -> bytes:
        url = self.images[asset_id]
        host = urlsplit(url).hostname or ""
        if (urlsplit(url).scheme != "https" or not any(
                host.endswith("." + domain) for domain in self.image_domains)):
            raise SyncError(f"{self.label}: unsupported image host")
        response = self.request("GET", url)
        if not response.headers.get("Content-Type", "").lower().startswith("image/"):
            raise SyncError(f"{self.label}: preview is not an image")
        return response.content

    def download_video_resource(self, url: str, expected_size: int, destination: Path, *,
                                domains: tuple[str, ...], hosts: tuple[str, ...] = ()) -> None:
        """Stream a signed media resource without forwarding API authorization."""
        try:
            for _ in range(6):
                parsed = urlsplit(url)
                host = parsed.hostname or ""
                if (parsed.scheme != "https"
                        or not (host in hosts or any(host.endswith("." + domain) for domain in domains))
                        or parsed.port not in {None, 443} or parsed.username or parsed.password):
                    raise SyncError(f"{self.label}: unsupported video host")
                with self.request("GET", url, stream=True, allow_redirects=False,
                                  headers={"Accept-Encoding": "identity"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise SyncError(f"{self.label}: invalid video redirect")
                        url = urljoin(url, location)
                        continue
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if (response.status_code != 200
                            or not (content_type.startswith("video/") or content_type == "application/octet-stream")):
                        raise SyncError(f"{self.label}: video is not ready or response is not a complete video; retry")
                    if response.headers.get("Content-Encoding", "").lower() not in {"", "identity"}:
                        raise SyncError(f"{self.label}: unsupported video encoding")
                    length = response.headers.get("Content-Length")
                    if length is not None and (not length.isdigit() or int(length) != expected_size):
                        raise SyncError(f"{self.label}: video length does not match the album resource; retry")
                    size = 0
                    with destination.open("wb") as file:
                        for chunk in response.iter_content(1024 * 1024):
                            size += len(chunk)
                            if size > expected_size:
                                raise SyncError(f"{self.label}: video exceeds the album resource size; retry")
                            file.write(chunk)
                    if size != expected_size:
                        raise SyncError(f"{self.label}: empty or incomplete video; retry")
                    return
            raise SyncError(f"{self.label}: too many video redirects")
        except requests.RequestException:
            raise SyncError(f"{self.label}: video download interrupted; retry") from None
        except ValueError:
            raise SyncError(f"{self.label}: invalid video URL or response; retry") from None
