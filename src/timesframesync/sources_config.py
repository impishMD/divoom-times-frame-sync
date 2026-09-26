# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

"""Declarative source list; URLs are credentials and never appear in repr/logs."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from .config import Config, SyncError, parse_share_url

PROVIDERS = ("immich", "google_photos", "icloud", "yandex_disk", "onedrive")


@dataclass(frozen=True)
class SourceConfig:
    id: str
    provider: str
    url: str = field(repr=False)
    target_album: str
    password: str = field(default="", repr=False)


def validate_url(provider: str, url: str):
    if provider == "immich":
        parse_share_url(url)
        return
    parts = urlsplit(url)
    hosts = {"google_photos": {"photos.app.goo.gl", "photos.google.com"},
             "icloud": {"photos.icloud.com"},
             "yandex_disk": {"disk.yandex.ru", "disk.yandex.com"},
             "onedrive": {"1drv.ms"}}
    if (parts.scheme != "https" or parts.hostname not in hosts[provider]
            or parts.username or parts.port not in {None, 443}):
        raise SyncError(f"Invalid public album URL for {provider}")
    if provider == "yandex_disk" and not re.fullmatch(r"/a/[A-Za-z0-9_-]+/?", parts.path):
        raise SyncError("Yandex Disk currently supports public photo album /a/ links")
    if provider == "icloud" and not re.fullmatch(r"/shared/album/[A-Za-z0-9_-]+/?", parts.path):
        raise SyncError("iCloud currently supports photos.icloud.com/shared/album/ links")
    if provider == "onedrive" and not re.fullmatch(r"/a/[A-Za-z0-9!_/-]+", parts.path):
        raise SyncError("OneDrive currently supports public photo album /a/ links")


def load_sources(config: Config) -> list[SourceConfig]:
    if config.sources_file is None:
        validate_url("immich", config.share_url)
        return [SourceConfig("immich", "immich", config.share_url, config.frame_album, config.password)]
    try:
        document = tomllib.loads(config.sources_file.read_text())
    except (OSError, ValueError):
        raise SyncError("Unable to read SOURCES_FILE as TOML") from None
    if set(document) - {"sources"} or not isinstance(document.get("sources"), list) or not document["sources"]:
        raise SyncError("SOURCES_FILE must contain one or more [[sources]] sections")
    result = []
    ids = set()
    for item in document["sources"]:
        if not isinstance(item, dict) or set(item) - {"id", "provider", "url", "url_env", "password", "password_env", "target_album"}:
            raise SyncError("Unknown field in source configuration")
        source_id, provider = item.get("id", ""), item.get("provider", "")
        if not isinstance(source_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", source_id):
            raise SyncError("Each source needs a stable id (letters, digits, dash, underscore; max 64)")
        if source_id.casefold() in ids:
            raise SyncError(f"Duplicate source id: {source_id}")
        ids.add(source_id.casefold())
        if provider not in PROVIDERS:
            raise SyncError(f"Unknown provider for source {source_id}")
        def secret(name):
            if name in item and name + "_env" in item:
                raise SyncError(f"Source {source_id}: use {name} or {name}_env, not both")
            value = item.get(name, "")
            if name + "_env" in item:
                key = item[name + "_env"]
                if not isinstance(key, str) or key not in os.environ:
                    raise SyncError(f"Source {source_id}: missing environment variable for {name}")
                value = os.environ[key]
            if not isinstance(value, str):
                raise SyncError(f"Source {source_id}: {name} must be a string")
            return value
        url, password = secret("url"), secret("password")
        target = item.get("target_album", config.frame_album)
        if not isinstance(target, str) or not target.strip():
            raise SyncError(f"Source {source_id}: target_album must be a nonempty string")
        try:
            validate_url(provider, url)
        except ValueError:
            raise SyncError(f"Source {source_id}: invalid URL") from None
        result.append(SourceConfig(source_id, provider, url, target, password))
    return result
