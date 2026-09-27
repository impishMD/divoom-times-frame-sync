# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

"""Anonymous CloudKit shared collections used by photos.icloud.com."""
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from ..config import SyncError
from ..source import Album, Asset
from .public import PublicSource


def field(record: dict, name: str, default=None):
    return record.get("fields", {}).get(name, {}).get("value", default)


class ICloud(PublicSource):
    label = "iCloud Photos"
    image_domains = ("icloud-content.com",)
    database = "/database/1/com.apple.photos.cloud/production/"

    def __init__(self, url: str):
        super().__init__(url)
        self.videos: dict[str, tuple[str, int]] = {}

    @staticmethod
    def video_resource(asset: dict, master: dict) -> dict:
        # Rendered edits on the asset take precedence over the original master.
        # Prefer a prepared rendition to downloading a much larger original.
        for record in (asset, master):
            for name in ("resVidFullRes", "resVidLargeRes", "resVidMedRes", "resVidSmallRes", "resOriginalRes"):
                resource = field(record, name)
                if resource is not None:
                    if (not isinstance(resource, dict)
                            or not isinstance(resource.get("fileChecksum"), str) or not resource["fileChecksum"]
                            or not isinstance(resource.get("downloadURL"), str) or not resource["downloadURL"]
                            or type(resource.get("size")) is not int or resource["size"] <= 0):
                        raise ValueError
                    return resource
        raise SyncError("iCloud Photos: video is not ready or has no downloadable resource; retry")

    def query(self, query: dict, *, limit: int = 200) -> dict:
        return self.json(self.query_url, params=self.params, headers={"Content-Type": "text/plain"},
                         json={"query": query, "zoneID": self.zone, "resultsLimit": limit})

    def count(self) -> tuple[int, str | None]:
        response = self.query({"recordType": "HyperionIndexCountLookup", "filterBy": [
            {"fieldName": "indexCountID", "comparator": "IN", "fieldValue": {
                "value": ["CPLAssetByAssetDateWithoutHiddenOrDeleted"], "type": "STRING_LIST"}}]})
        records = response["records"]
        if len(records) != 1 or records[0].get("recordType") != "IndexCountResult":
            raise ValueError
        count = field(records[0], "itemCount")
        if type(count) is not int or count < 0:
            raise ValueError
        return count, response.get("syncToken")

    def album(self) -> Album:
        try:
            key = urlsplit(self.url).path.rstrip("/").rsplit("/", 1)[1]
            self.params = {"remapEnums": "true", "getCurrentSyncToken": "true", "sharing_url_key": key}
            resolved = self.json("https://ckdatabasews.icloud.com" + self.database + "public/records/resolve",
                                 params=self.params, json={"shortGUIDs": [{"value": key}]},
                                 headers={"Content-Type": "text/plain"})["results"]
            if len(resolved) != 1:
                raise ValueError
            share = resolved[0]
            auth = share["anonymousPublicAccess"]
            partition = urlsplit(auth["databasePartition"])
            if (partition.scheme != "https" or not (partition.hostname or "").endswith("-ckdatabasews.icloud.com")
                    or partition.port not in {None, 443} or partition.username):
                raise ValueError
            self.query_url = auth["databasePartition"].rstrip("/") + self.database + "shared/records/query"
            self.params["publicAccessAuthToken"] = auth["token"]
            self.zone = share["zoneID"]
            album_id = self.zone["zoneName"] + ":" + self.zone["ownerRecordName"]
            title = field(share["share"], "cloudkit.title")
            if not isinstance(title, str) or not title or share["databaseScope"] != "SHARED":
                raise ValueError
            count, initial_token = self.count()
            assets, masters = {}, {}
            for _ in range(10000):
                if len(assets) == count:
                    break
                response = self.query({"recordType": "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted", "filterBy": [
                    {"fieldName": "direction", "comparator": "EQUALS", "fieldValue": {"value": "ASCENDING", "type": "STRING"}},
                    {"fieldName": "startRank", "comparator": "EQUALS", "fieldValue": {"value": len(assets), "type": "INT64"}}]})
                old_count = len(assets)
                for record in response["records"]:
                    name = record["recordName"]
                    if record.get("recordType") == "CPLMaster":
                        masters[name] = record
                    elif record.get("recordType") == "CPLAsset":
                        if name in assets:
                            raise ValueError
                        assets[name] = record
                    else:
                        raise ValueError
                if len(assets) <= old_count or len(assets) > count:
                    raise ValueError
            else:
                raise ValueError
            end_count, end_token = self.count()
            if end_count != count or (initial_token and end_token and initial_token != end_token):
                raise ValueError
            photos, images, videos = [], {}, {}
            for asset_id, asset in assets.items():
                master = masters[field(asset, "masterRef")["recordName"]]
                item_type = field(master, "itemType", "")
                if item_type in {"com.apple.quicktime-movie", "public.mpeg-4", "public.movie", "public.video"}:
                    resource = self.video_resource(asset, master)
                    revision = f"{resource['fileChecksum']}:{field(asset, 'recordModificationDate')}"
                    photos.append(Asset(asset_id, revision, asset_id + ".mp4", "video"))
                    videos[asset_id] = (resource["downloadURL"].replace("${f}", "video.mp4"), resource["size"])
                    preview = field(asset, "resJPEGFullRes") or field(asset, "resJPEGMedRes") or field(master, "resJPEGFullRes") or field(master, "resJPEGMedRes")
                    if preview:
                        images[asset_id] = preview["downloadURL"].replace("${f}", "photo.jpg")
                    continue
                if item_type not in {"public.jpeg", "public.heic", "public.heif", "public.png", "com.compuserve.gif", "public.tiff", "public.camera-raw-image"}:
                    raise SyncError("iCloud Photos: unsupported media type; keeping previous album")
                # Prefer rendered adjustments on CPLAsset, then the master's JPEG.
                resource = field(asset, "resJPEGFullRes") or field(asset, "resJPEGMedRes") or field(master, "resJPEGFullRes") or field(master, "resJPEGMedRes")
                if not resource:
                    raise ValueError
                checksum = resource["fileChecksum"]
                revision = f"{checksum}:{field(asset, 'recordModificationDate')}"
                photos.append(Asset(asset_id, revision, asset_id + ".jpg"))
                images[asset_id] = resource["downloadURL"].replace("${f}", "photo.jpg")
            self.images = images
            self.videos = videos
            return Album(album_id, title, photos)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise SyncError("iCloud Photos: incomplete or unsupported public album listing; check sharing or retry") from None

    def download_video(self, asset_id: str, destination: Path) -> None:
        video = self.videos.get(asset_id)
        if not video:
            raise SyncError("iCloud Photos: video missing from the last album listing; refresh and retry")
        url, expected_size = video
        try:
            for _ in range(6):
                parsed = urlsplit(url)
                if (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".icloud-content.com")
                        or parsed.port not in {None, 443} or parsed.username or parsed.password):
                    raise SyncError("iCloud Photos: unsupported video host")
                with self.request("GET", url, stream=True, allow_redirects=False,
                                  headers={"Accept-Encoding": "identity"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise SyncError("iCloud Photos: invalid video redirect")
                        url = urljoin(url, location)
                        continue
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if (response.status_code != 200
                            or not (content_type.startswith("video/") or content_type == "application/octet-stream")):
                        raise SyncError("iCloud Photos: video is not ready or response is not a complete video; retry")
                    if response.headers.get("Content-Encoding", "").lower() not in {"", "identity"}:
                        raise SyncError("iCloud Photos: unsupported video encoding")
                    length = response.headers.get("Content-Length")
                    if length is not None and (not length.isdigit() or int(length) != expected_size):
                        raise SyncError("iCloud Photos: video length does not match the album resource; retry")
                    size = 0
                    with destination.open("wb") as file:
                        for chunk in response.iter_content(1024 * 1024):
                            size += len(chunk)
                            if size > expected_size:
                                raise SyncError("iCloud Photos: video exceeds the album resource size; retry")
                            file.write(chunk)
                    if size != expected_size:
                        raise SyncError("iCloud Photos: empty or incomplete video; retry")
                    return
            raise SyncError("iCloud Photos: too many video redirects")
        except requests.RequestException:
            raise SyncError("iCloud Photos: video download interrupted; retry") from None
        except ValueError:
            raise SyncError("iCloud Photos: invalid video URL or response; retry") from None
