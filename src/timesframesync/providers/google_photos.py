# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

"""Public shared-page protocol, not the OAuth Photos Library API."""
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

import requests

from ..config import SyncError
from ..source import Album, Asset
from .public import PublicSource


class GooglePhotos(PublicSource):
    label = "Google Photos"
    image_domains = ("googleusercontent.com",)

    def __init__(self, url: str):
        super().__init__(url)
        self.videos: dict[str, str] = {}

    @staticmethod
    def initial_data(html: str) -> list:
        for match in re.finditer(r"AF_initDataCallback\(\{key:\s*'[^']+'.*?data:", html):
            try:
                data, _ = json.JSONDecoder().raw_decode(html[match.end():].lstrip())
                if isinstance(data, list) and len(data) > 3 and isinstance(data[3], list) and len(data[3]) > 21:
                    return data
            except ValueError:
                continue
        raise SyncError("Google Photos: public album data missing; check sharing or page format")

    def next_page(self, album_id: str, key: str, token: str) -> list:
        response = self.request("POST", "https://photos.google.com/_/PhotosUi/data/batchexecute",
                                params={"rpcids": "snAcKc"}, data={"f.req": json.dumps([
                                    [["snAcKc", json.dumps([album_id, token, None, key]), None, "generic"]]
                                ])})
        # batchexecute can include anti-XSSI prefix, byte-length lines and
        # separate JSON frames. Only accept the matching successful RPC.
        for line in response.text.splitlines():
            if not line.startswith("["):
                continue
            try:
                for entry in json.loads(line):
                    if entry[:2] == ["wrb.fr", "snAcKc"] and isinstance(entry[2], str):
                        return json.loads(entry[2])
            except (ValueError, TypeError, IndexError):
                pass
        raise SyncError("Google Photos: invalid pagination response")

    def album(self) -> Album:
        try:
            response = self.request("GET", self.url)
            if urlsplit(response.url).hostname != "photos.google.com":
                raise SyncError("Google Photos: public album redirects to login or consent")
            data = self.initial_data(response.text)
            info = data[3]
            album_id, title, key, count = info[0], info[1], info[19], info[21]
            if not all(isinstance(x, str) and x for x in (album_id, title, key)) or type(count) is not int or count < 0:
                raise ValueError
            photos, images, videos, ids, tokens = [], {}, {}, set(), set()
            for _ in range(10000):
                items = data[1]
                if items is None and count == 0:
                    items = []
                if not isinstance(items, list):
                    raise ValueError
                for item in items:
                    asset_id, details = item[0], item[1]
                    if not isinstance(asset_id, str) or not asset_id or asset_id in ids:
                        raise ValueError
                    ids.add(asset_id)
                    video = len(item) > 9 and isinstance(item[9], dict) and "76647426" in item[9]
                    url, width, height = details[:3]
                    if not isinstance(url, str) or not all(type(n) is int and n > 0 for n in (width, height)):
                        raise ValueError
                    revision = json.dumps([item[2], item[5], width, height])
                    kind, extension = ("video", ".mp4") if video else ("photo", ".jpg")
                    photos.append(Asset(asset_id, revision, asset_id + extension, kind))
                    images[asset_id] = url + "=w2048-h2048"
                    if video:
                        # Resolve fresh CDN URLs on every listing; never use
                        # their temporary signatures as a media revision.
                        videos[asset_id] = url + "=dv"
                token = data[2]
                if not token:
                    break
                if not isinstance(token, str) or token in tokens or not items or len(ids) >= count:
                    raise ValueError
                tokens.add(token)
                data = self.next_page(album_id, key, token)
            else:
                raise ValueError
            if len(ids) != count:
                raise ValueError
            self.images = images
            self.videos = videos
            return Album(album_id, title, photos)
        except (ValueError, TypeError, KeyError, IndexError):
            raise SyncError("Google Photos: incomplete or unsupported album listing; retry") from None

    def download_video(self, asset_id: str, destination: Path) -> None:
        url = self.videos.get(asset_id)
        if not url:
            raise SyncError("Google Photos: video missing from the last album listing; refresh and retry")
        try:
            # Public =dv URLs redirect to video-downloads.googleusercontent.com.
            # Validate each hop before sending a request, and stream the complete
            # response to the cache's temporary file without buffering it in RAM.
            for _ in range(6):
                parsed = urlsplit(url)
                if (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".googleusercontent.com")
                        or parsed.username or parsed.password):
                    raise SyncError("Google Photos: unsupported video host")
                with self.request("GET", url, stream=True, allow_redirects=False,
                                  headers={"Accept-Encoding": "identity"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise SyncError("Google Photos: invalid video redirect")
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200 or not response.headers.get("Content-Type", "").lower().startswith("video/"):
                        raise SyncError("Google Photos: video is not ready or response is not a complete video; retry")
                    length = response.headers.get("Content-Length")
                    if length is not None and (not length.isdigit() or int(length) <= 0):
                        raise SyncError("Google Photos: invalid video length")
                    size = 0
                    with destination.open("wb") as file:
                        for chunk in response.iter_content(1024 * 1024):
                            size += file.write(chunk)
                    identity = response.headers.get("Content-Encoding", "").lower() in {"", "identity"}
                    if not size or (length is not None and identity and size != int(length)):
                        raise SyncError("Google Photos: empty or incomplete video; retry")
                    return
            raise SyncError("Google Photos: too many video redirects")
        except requests.RequestException:
            raise SyncError("Google Photos: video download interrupted; retry") from None
        except ValueError:
            raise SyncError("Google Photos: invalid video URL or response; retry") from None
