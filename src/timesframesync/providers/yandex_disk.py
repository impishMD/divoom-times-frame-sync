# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

"""Public Yandex Disk photo albums (/a/), using the gallery's read-only API."""
import json
import re
from urllib.parse import quote, urlsplit

from ..config import SyncError
from ..source import Album, Asset
from .public import PublicSource


class YandexDisk(PublicSource):
    label = "Yandex Disk"
    image_domains = ("disk.yandex.ru", "disk.yandex.com", "disk.yandex.net", "yandex.net")
    # The public gallery serves a browser page; its default response to
    # python-requests can be a challenge instead of the album bootstrap.
    user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"

    def __init__(self, url: str):
        super().__init__(url)
        self.session.headers.update({"User-Agent": self.user_agent})

    def bootstrap(self):
        response = self.request("GET", self.url)
        match = re.search(r'<script\b[^>]*\bid=["\']store-prefetch["\'][^>]*>(.*?)</script>', response.text, re.S)
        if not match:
            raise SyncError("Yandex Disk: public album data unavailable (login, CAPTCHA or page format changed)")
        state = json.loads(match[1])
        root = state["resources"][state["rootResourceId"]]
        if root.get("type") != "album" or root.get("errorCode") or root.get("blocked"):
            raise SyncError("Yandex Disk: link must be an accessible public photo album")
        return state, root

    def page(self, album_hash: str, sk: str, cursor: str | None):
        parts = urlsplit(self.url)
        origin = f"{parts.scheme}://{parts.netloc}"
        payload = {"hash": album_hash, "sk": sk, "lastItemId": cursor}
        response = self.request("POST", origin + "/public/api/fetch-album-list",
                                headers={"Content-Type": "text/plain", "X-Requested-With": "XMLHttpRequest",
                                         "X-Retpath-Y": self.url},
                                data=quote(json.dumps(payload, separators=(",", ":")), safe="~()*!.'-"))
        data = response.json()
        if not isinstance(data, dict) or data.get("error") or data.get("type") == "captcha":
            raise SyncError("Yandex Disk: album page unavailable; retry or check public access")
        if type(data.get("completed")) is not bool or not isinstance(data.get("resources"), list):
            raise ValueError
        return data

    def album(self) -> Album:
        try:
            state, root = self.bootstrap()
            album_id, title = root["id"], root["name"]
            if not all(isinstance(v, str) and v for v in (album_id, title)):
                raise ValueError
            album_hash = root.get("path") or root["hash"]
            sk = state["environment"]["sk"]
            photos, images, ids, cursors = [], {}, set(), set()
            cursor = None
            # Always enumerate the API from page one, including small/empty
            # albums. The HTML bootstrap can contain just the first portion.
            for _ in range(10000):
                data = self.page(album_hash, sk, cursor)
                resources = data["resources"]
                for item in resources:
                    asset_id = item["id"]
                    if not isinstance(asset_id, str) or not asset_id or asset_id in ids:
                        raise ValueError
                    ids.add(asset_id)
                    meta = item["meta"]
                    if item.get("type") != "file" or item.get("virus") or meta.get("drweb") not in {None, 1}:
                        raise ValueError
                    if meta.get("mediatype") == "video":
                        continue
                    if meta.get("mediatype") != "image":
                        raise SyncError("Yandex Disk: unsupported album media; keeping previous snapshot")
                    preview = meta.get("xxxlPreview") or meta.get("original")
                    if not isinstance(preview, str) or not preview:
                        raise ValueError
                    revision = json.dumps([item["modified"], meta["size"], meta.get("file_id"), meta.get("mimetype")])
                    photos.append(Asset(asset_id, revision, item["name"]))
                    images[asset_id] = preview
                if data["completed"]:
                    break
                if not resources:
                    raise ValueError
                cursor = resources[-1]["albumItemId"]
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise ValueError
                cursors.add(cursor)
            else:
                raise ValueError
            # The album's modification marker changes when its membership is
            # edited. Do not reconcile a list assembled across such a change.
            _, current = self.bootstrap()
            if current["id"] != album_id or current["modified"] != root["modified"]:
                raise ValueError
            self.images = images
            return Album(album_id, title, photos, len(ids) - len(photos))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise SyncError("Yandex Disk: incomplete or unsupported album listing; retry") from None
