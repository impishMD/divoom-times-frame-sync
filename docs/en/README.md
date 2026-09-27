# Divoom Times Frame Sync

**English** | [Русский](/docs/ru/README.md)

Sync **public photo albums** from Google Photos, iCloud Photos, Immich, OneDrive, and Yandex Disk to your **Divoom Times Frame**.

Add album sharing links, choose destination albums on the frame, and keep them up to date automatically. Combine albums from different services into one slideshow or sync them to separate frame albums.

Photos and videos are stored in the frame's own albums and play **autonomously**. The service only needs to run to pick up changes; stopping it does not stop playback.

## What it does

- Syncs every configured public album in one run, with multiple albums per service.
- Uploads new photos and videos over the local network.
- Combines sources that share the same destination album.
- Removes missing items from the destination album or keeps them, depending on the sync mode.
- Deletes temporary local media files after a verified upload and keeps a small sync journal between runs.

## Supported public albums

The service uses **album sharing links** that can be opened without signing into the source account. Password-protected Immich links are also supported. Enable public or link sharing for each album you want to sync.

| Service | Album link format | Photos | Video |
| --- | --- | --- | --- |
| Google Photos | `photos.app.goo.gl/...` or `photos.google.com/share/...` | Yes | Yes |
| iCloud Photos | `photos.icloud.com/shared/album/...` | Yes | Yes |
| Immich | `<your-server>/share/...`, optionally with a password | Yes | Yes |
| OneDrive | `1drv.ms/a/...` from a personal account | Yes | Yes |
| Yandex Disk | `disk.yandex.ru/a/...` or `disk.yandex.com/a/...` | Yes | Yes |

An account login, OAuth setup, and browser automation are not required to read these albums. Each link grants access to its album, so keep your actual links and passwords in local configuration files.

## Getting started

### 1. Prepare the frame and configuration

You need a Divoom Times Frame reachable over the local network, its IP address and local token, and at least one public album link. Create the destination albums in the Divoom app first; their names must match your configuration exactly.

Get the project and copy the example configuration:

```sh
git clone https://github.com/impishMD/divoom-times-frame-sync.git
cd divoom-times-frame-sync
cp .env.example .env
cp sources.example.toml sources.toml
chmod 600 .env sources.toml
```

Set the frame connection and the links you plan to use in `.env`. Replace the example IP address, token, and links with your own:

```dotenv
DIVOOM_HOST=192.168.1.100
DIVOOM_TOKEN=123456
DIVOOM_ALBUM=Photos
SOURCES_FILE=./sources.toml
SYNC_MODE=mirror
SYNC_INTERVAL=300

GOOGLE_PHOTOS_SHARE_URL=https://photos.app.goo.gl/your-album
ICLOUD_SHARE_URL=https://photos.icloud.com/shared/album/your-album
IMMICH_SHARE_URL=https://immich.example.com/share/your-key
IMMICH_SHARE_PASSWORD=
ONEDRIVE_SHARE_URL=https://1drv.ms/a/your-album
YANDEX_DISK_SHARE_URL=https://disk.yandex.ru/a/your-album
```

In `sources.toml`, keep only the sources you want to sync. The example file includes all five services; you only need to fill in links for the sections you keep. For example, these two sources feed the same frame album:

```toml
[[sources]]
id = "google-family"
provider = "google_photos"
url_env = "GOOGLE_PHOTOS_SHARE_URL"
target_album = "Photos"

[[sources]]
id = "icloud-family"
provider = "icloud"
url_env = "ICLOUD_SHARE_URL"
target_album = "Photos"
```

Add more `[[sources]]` sections for additional albums, including albums from the same service. Give each source a unique, stable `id` and its own sharing link. The same `target_album` combines sources; different values sync to separate albums on the frame. See [source configuration](/docs/en/sources.md) for the full format.

### 2. Start the service

Choose Docker Compose or a local Python installation. Both use the same `.env`, `sources.toml`, and `data/` directory.

#### Docker Compose

Prebuilt images are available for **Linux amd64 and arm64**:

- Docker Hub: `impishmd/divoom-times-frame-sync`
- GitHub Container Registry: `ghcr.io/impishmd/divoom-times-frame-sync`

Compose uses Docker Hub and the latest stable release by default:

```sh
docker compose pull
docker compose up -d --no-build
docker compose logs -f
```

To pin a version or use GHCR, set `TFS_IMAGE` in `.env`, for example `TFS_IMAGE=ghcr.io/impishmd/divoom-times-frame-sync:v0.7.1`. Stable releases have a version tag and `latest`; prereleases only have a version tag. Use `docker compose pull && docker compose up -d --no-build` to update. To build from your local checkout, use `docker compose up -d --build --pull never`.

The container includes FFmpeg for video conversion. It reads `sources.toml` through a read-only mount and stores sync state in `data/`. It needs outbound access to your album services and the frame's local API, normally on port `9000`. No inbound ports need to be published.

#### Python on macOS or Linux

Requires Python 3.11+. From the project directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .

# Check source albums and the frame:
.venv/bin/tfs status

# Sync all configured albums once and start playback:
.venv/bin/tfs sync

# Keep syncing automatically:
.venv/bin/tfs run
```

For video, also install FFmpeg with `ffprobe` and the `libx264` encoder: `brew install ffmpeg` on macOS or `sudo apt-get install ffmpeg` on Debian/Ubuntu. Photo-only sync does not require FFmpeg.

The command is `tfs`. Press Ctrl+C to stop the service; playback continues on the frame. `SYNC_INTERVAL` controls the wait between sync cycles, not the slideshow interval.

At the default `INFO` level, logs show startup/shutdown, the first successful cycle, changes, and warnings/errors. When nothing changes, `run` reports a successful cycle once every 10 minutes. Use `.venv/bin/tfs --log-level DEBUG run` for every cycle and all source, destination, media, API, and local metadata timings. See [logging and timings](/docs/en/sync-modes.md#checking-operation).

## Sync modes

| Mode | New items | Items removed from the source albums |
| --- | --- | --- |
| `mirror` — default | Added to the frame album | Removed from that album if tracked by the service |
| `append` | Added to the frame album | Kept, including previous versions of changed items |

Set `SYNC_MODE` in `.env` or override it for a run:

```sh
.venv/bin/tfs run --sync-mode mirror
.venv/bin/tfs run --sync-mode append
```

**Removal means removing an item from the destination album.** The file stays in the frame's memory and in “All Photos”; it is not a way to reclaim device storage. Other albums and items added manually are preserved. The service does not change the source albums or cloud originals.

Each destination is reconciled against the combined contents of all its sources. If one source cannot be fully listed, that destination is skipped for the cycle. Old items are removed only after all current items have been verified on the frame. Other destinations can continue syncing independently.

## Playback and media

The frame displays one album at a time. For one slideshow containing all your sources, give them the same `target_album`. The service prefers the album named by `DIVOOM_ALBUM`, then the first nonempty destination in configuration order.

`sync` selects an album after uploading. `run` selects one after its first successful nonempty cycle; later updates do not switch the screen. Set slideshow timing, order, effects, video autoplay, and volume in the Divoom app.

Photos are resized to 800×1280 and uploaded as WebP. `IMAGE_FIT=contain` keeps the whole image with borders; `cover` fills the screen by cropping.

Video sync is available for all five supported services. Videos are converted locally to MP4 with H.264 video and AAC audio when present, at 800×1280 and 30 fps. Rotation is applied, `IMAGE_FIT` controls framing, and the full duration is retained. Each video is uploaded with a WebP cover and follows the same sync rules as photos. Enable video autoplay on the frame for automatic playback.

iCloud Photos supplies downloadable video resources through the public album link. The service prefers rendered edits and prepared video versions, falling back to the original when necessary. Live Photos are synced as still images.

Google Photos supplies a transcoded video download through the public album link. Newly uploaded videos may need time to finish processing on Google's side. If a video is unavailable or its download fails, the service retries on the next cycle and preserves existing album items.

OneDrive and Yandex Disk supply original videos through temporary download links. The service verifies the downloaded size against the album metadata before conversion. Expired links or interrupted downloads are retried on the next cycle.

## Local storage

Normal `sync` and `run` process media one item at a time. **The temporary local copy is deleted after the file, album membership, and sync journal have been confirmed.** You do not need disk space for a permanent copy of all your albums.

Photos need temporary space for a prepared image. Video conversion needs room for the downloaded video, the converted MP4, and a cover. Completed uploads leave only small metadata files in `data/`: source IDs, revisions, checksums, and records of items managed on the frame.

Unchanged items are matched against source metadata, the frame database, and the saved verification journal. Normal cycles neither download their contents from the source nor read them back from the frame. New uploads are checked once; old or missing verification records require a one-time full check. Use `tfs repair` for an explicit integrity check and recovery. Interrupted uploads retain prepared files for a retry; obsolete temporary versions are cleaned up when the source list is refreshed successfully.

**Keep `data/` between runs and container restarts.** Without it, matching files may be found again, but ownership of items already removed from the sources cannot be recovered. Reprocessing with different settings or codecs may also produce a separate copy. See [storage and recovery](/docs/en/storage.md).

## Integrity checks and repair

Stop `run` before repair; both commands use the same `DATA_DIR` lock.

```sh
.venv/bin/tfs repair --dry-run  # Read full contents and report problems
.venv/bin/tfs repair            # Check and restore damaged or missing media
```

Repair checks photos, MP4 files, video covers, media types, and album membership across all configured sources. Healthy files are not reuploaded. It does not prune items removed from source albums or switch playback. A dry run makes no frame changes and exits with status 1 when problems are found. Full checks read media over the LAN and can take time, especially for video. See [repair and recovery](/docs/en/repair.md).

## Settings

| Variable | Purpose | Default |
| --- | --- | --- |
| `SOURCES_FILE` | TOML file listing public source albums | Set to `./sources.toml` in the example |
| `DIVOOM_HOST` | Frame IP address or hostname | Required |
| `DIVOOM_PORT` | Local API port | `9000` |
| `DIVOOM_TOKEN` | Numeric local device token | Empty |
| `DIVOOM_ALBUM` | Default destination and preferred playback album | `Photos` |
| `DIVOOM_USER_ID` | Optional sender ID | `0` |
| `SYNC_INTERVAL` | Wait between sync cycles, in seconds | `300` |
| `SYNC_MODE` | `mirror` or `append` | `mirror` |
| `IMAGE_FIT` | `contain` or `cover`, for photos and video | `contain` |
| `DATA_DIR` | Persistent sync state and temporary media | `./data` |

Album links are referenced through `url_env` in `sources.toml`, or supplied directly as `url`. For a password-protected Immich link, use `password_env` or `password`. Environment variable names can be chosen per source, allowing several albums from the same service.

Place global options such as `--env-file` and `--sources-file` before the command:

```sh
.venv/bin/tfs --env-file .env --sources-file sources.toml sync
```

Restart the service after changing configuration. For Compose, use `docker compose up -d --no-build --force-recreate`.

## Other commands

| Command | What it does |
| --- | --- |
| `.venv/bin/tfs status` | Read source and frame album status |
| `.venv/bin/tfs sync --dry-run` | Read status without changing the frame; does not list planned removals |
| `.venv/bin/tfs repair --dry-run` | Check full media contents; report damage without changing the frame |
| `.venv/bin/tfs repair` | Check and recover damaged or missing media |
| `.venv/bin/tfs snapshot` | Save the screen to `data/snapshot.webp` |
| `.venv/bin/tfs restore` | Select the previously active frame screen |
| `.venv/bin/tfs cache` | Prepare all media in advance and keep it locally until sync |

`cache` is an optional prefetch command and can use space for the entire prepared collection. It is not needed for normal operation. Paths shown above use the default `DATA_DIR`.

## Current scope

- Sources must be accessible through supported public album links. Private account libraries are not supported.
- iCloud Live Photos are synced as still images.
- Legacy iCloud `sharedalbum/#...` links, public folders on Yandex Disk or OneDrive, and OneDrive for Business/SharePoint are not supported.
- Public album viewers may change their protocols. Frame API compatibility depends on the firmware.

## Documentation

- [Multiple sources and destination albums](/docs/en/sources.md)
- [Storage, temporary files, and recovery](/docs/en/storage.md)
- [Mirror and append modes](/docs/en/sync-modes.md)
- [Source service APIs](/docs/en/source-api.md)
- [Frame API reference](/docs/en/frame-api.md)
- [Native album LAN protocol](/docs/en/protocol.md)

## Contributing and security

- [Contributor guidelines](/docs/en/CONTRIBUTING.md)
- [Security policy and vulnerability reporting](/docs/en/SECURITY.md)
- [Release process and registry setup](/docs/en/releases.md)

Source archives, Python packages, checksums, and container digests are available on the [releases page](https://github.com/impishMD/divoom-times-frame-sync/releases).

## License

Licensed under the [Apache License 2.0](/LICENSE). Copyright 2026 [impishMD](https://github.com/impishMD). Attribution is recorded in [NOTICE](/NOTICE).

When redistributing this project or derivative works, follow the license's requirements for the license copy, change notices, and applicable copyright and attribution notices. Dependencies and external tools retain their own licenses.
