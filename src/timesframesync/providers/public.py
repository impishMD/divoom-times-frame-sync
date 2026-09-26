# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

from urllib.parse import urlsplit

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
