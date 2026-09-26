# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

"""Anonymous CloudKit shared collections used by photos.icloud.com."""
from urllib.parse import urlsplit

from ..config import SyncError
from ..source import Album, Asset
from .public import PublicSource


def field(record: dict, name: str, default=None):
    return record.get("fields", {}).get(name, {}).get("value", default)


class ICloud(PublicSource):
    label = "iCloud Photos"
    image_domains = ("icloud-content.com",)
    database = "/database/1/com.apple.photos.cloud/production/"

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
            photos, images, skipped = [], {}, 0
            for asset_id, asset in assets.items():
                master = masters[field(asset, "masterRef")["recordName"]]
                item_type = field(master, "itemType", "")
                if item_type in {"com.apple.quicktime-movie", "public.mpeg-4", "public.movie", "public.video"}:
                    skipped += 1
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
            return Album(album_id, title, photos, skipped)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise SyncError("iCloud Photos: incomplete or unsupported public album listing; check sharing or retry") from None
