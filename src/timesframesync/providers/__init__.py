# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

from dataclasses import replace

from ..config import Config
from ..immich import Immich
from ..sources_config import SourceConfig
from .google_photos import GooglePhotos
from .icloud import ICloud
from .yandex_disk import YandexDisk
from .onedrive import OneDrive


def create_source(source: SourceConfig, config: Config):
    if source.provider == "immich":
        return Immich(replace(config, share_url=source.url, password=source.password))
    return {"google_photos": GooglePhotos, "icloud": ICloud,
            "yandex_disk": YandexDisk, "onedrive": OneDrive}[source.provider](source.url)
