# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

"""Anonymous personal OneDrive photo albums, as read by the public viewer."""
import base64
import json
import re
from urllib.parse import quote, urlsplit

from ..config import SyncError
from ..source import Album, Asset
from .public import PublicSource


class OneDrive(PublicSource):
    label = "OneDrive"
    api_origin = "https://my.microsoftpersonalcontent.com"
    # Public viewer application identifier, not an account credential.
    app_id = "073204aa-c1e0-4e66-a200-e5815a0aa93d"
    image_domains = ("svc.ms", "1drv.com", "livefilestore.com", "onedrive.com")
    page_size = 200

    def __init__(self, url: str):
        super().__init__(url)
        self.authorization = {}
        self.thumbnail_endpoints = {}

    def api(self, method: str, url: str, **kwargs):
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.netloc != "my.microsoftpersonalcontent.com"
                or not parts.path.startswith("/_api/v2.") or parts.fragment):
            raise SyncError("OneDrive: unsupported API URL")
        response = self.request(method, url, headers=self.authorization, allow_redirects=False, **kwargs)
        data = response.json()
        if response.status_code != 200 or not isinstance(data, dict) or data.get("error"):
            raise ValueError
        return data

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9!_-]+", value):
            raise ValueError
        return quote(value, safe="!")

    def resolve(self):
        # A fresh anonymous session each cycle: no Microsoft account or browser
        # cookies. Never attach this authorization to a thumbnail CDN request.
        token = self.request("POST", "https://api-badgerp.svc.ms/v1.0/token",
                             json={"appId": self.app_id}, allow_redirects=False).json()
        if token.get("authScheme") != "badger" or not isinstance(token.get("token"), str) or not token["token"]:
            raise ValueError
        self.authorization = {"Authorization": "badger " + token["token"], "Prefer": "autoredeem"}
        share = "u!" + base64.urlsafe_b64encode(self.url.encode()).decode().rstrip("=")
        data = self.api("POST", self.api_origin + "/_api/v2.0/shares/" + share + "/driveitem",
                        params={"$select": "id,parentReference"})
        return (self.identifier(data["parentReference"]["driveId"]), self.identifier(data["id"]))

    @staticmethod
    def snapshot(data):
        # Do not mistake a public folder, a partial response or access error for
        # an empty photo album: mirror would otherwise remove managed photos.
        total = data["mediaAlbum"]["albumItemCount"]
        if type(total) is not int or total < 0 or data["folder"]["childCount"] != total:
            raise ValueError
        tag = data["eTag"]
        if not isinstance(tag, str) or not tag:
            raise ValueError
        return data["id"], total, tag, data["lastModifiedDateTime"]

    def album(self) -> Album:
        try:
            drive_id, album_id = self.resolve()
            base = f"{self.api_origin}/_api/v2.1/drives/{drive_id}"
            endpoint = base + "/albums/" + album_id
            metadata = self.api("GET", endpoint)
            snapshot = self.snapshot(metadata)
            title = metadata["name"]
            if snapshot[0] != album_id or not isinstance(title, str) or not title:
                raise ValueError
            photos, thumbnails, ids, pages = [], {}, set(), set()
            children_url = endpoint + "/children"
            url = children_url
            params = {"top": self.page_size}
            for _ in range(10000):
                if url in pages or urlsplit(url).path != urlsplit(children_url).path:
                    raise ValueError
                pages.add(url)
                data = self.api("GET", url, params=params)
                items = data["value"]
                if not isinstance(items, list):
                    raise ValueError
                for item in items:
                    asset_id = self.identifier(item["id"])
                    if asset_id in ids or "deleted" in item or "remoteItem" in item:
                        raise ValueError
                    ids.add(asset_id)
                    if item.get("video") is not None:
                        continue
                    if not isinstance(item.get("image"), dict) or not item["file"]["mimeType"].startswith("image/"):
                        raise SyncError("OneDrive: unsupported album media; keeping previous snapshot")
                    revision = json.dumps([item["cTag"], item["lastModifiedDateTime"], item["size"]])
                    photos.append(Asset(asset_id, revision, item["name"]))
                    thumbnails[asset_id] = base + "/items/" + asset_id + "/thumbnails"
                if len(ids) > snapshot[1]:
                    raise ValueError
                url = data.get("@odata.nextLink")
                if url is None:
                    break
                if not items or not isinstance(url, str) or not url:
                    raise ValueError
                params = None  # The server's continuation URL includes its query.
            else:
                raise ValueError
            if len(ids) != snapshot[1] or self.snapshot(self.api("GET", endpoint)) != snapshot:
                raise ValueError
            self.thumbnail_endpoints = thumbnails
            self.images = {}
            return Album(drive_id + ":" + album_id, title, photos, len(ids) - len(photos))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise SyncError("OneDrive: incomplete or unsupported public album; retry") from None

    def preview(self, asset_id: str) -> bytes:
        try:
            data = self.api("GET", self.thumbnail_endpoints[asset_id], params={"select": "c2048x2048"})
            url = data["value"][0]["c2048x2048"]["url"]
            if not isinstance(url, str) or not url:
                raise ValueError
            self.images[asset_id] = url
            return super().preview(asset_id)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise SyncError("OneDrive: photo preview unavailable") from None
