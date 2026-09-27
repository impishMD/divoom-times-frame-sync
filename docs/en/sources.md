# Multiple albums and services

**English** | [Русский](../ru/sources.md)

One `sync` run or one `run` cycle processes **every** `[[sources]]` section. A service can appear as many times as needed, for example three Immich albums and two Google Photos albums. This is a list of selected shared albums, not automatic access to an account's entire library.

## Configuration

```sh
cp sources.example.toml sources.toml
chmod 600 sources.toml
```

In `.env`:

```dotenv
SOURCES_FILE=./sources.toml
IMMICH_SHARE_URL=https://immich.example.com/share/your-key
IMMICH_SHARE_PASSWORD=
GOOGLE_PHOTOS_SHARE_URL=https://photos.app.goo.gl/your-album
ICLOUD_SHARE_URL=https://photos.icloud.com/shared/album/your-album
YANDEX_DISK_SHARE_URL=https://disk.yandex.ru/a/your-album
ONEDRIVE_SHARE_URL=https://1drv.ms/a/your-album
```

Store secrets in `.env` and reference them from TOML:

```toml
[[sources]]
id = "immich-family"
provider = "immich"
url_env = "IMMICH_SHARE_URL"
password_env = "IMMICH_SHARE_PASSWORD"
target_album = "Family"

[[sources]]
id = "google-family"
provider = "google_photos"
url_env = "GOOGLE_PHOTOS_SHARE_URL"
target_album = "Family"

[[sources]]
id = "icloud-holidays"
provider = "icloud"
url_env = "ICLOUD_SHARE_URL"
target_album = "Holidays"
```

Here, `Family` receives the combined contents of two source albums, while `Holidays` receives iCloud photos. Create ordinary albums named `Family` and `Holidays` in the Divoom app first. Names must match exactly, and the frame must not have duplicate album names. Automatic album creation is not implemented in this version.

Add sections for other services; the same service can appear with multiple `id` values:

```toml
[[sources]]
id = "yandex-family"
provider = "yandex_disk"
url_env = "YANDEX_DISK_SHARE_URL"
target_album = "Family"

[[sources]]
id = "onedrive-family"
provider = "onedrive"
url_env = "ONEDRIVE_SHARE_URL"
target_album = "Family"
```

Yandex Disk supports public photo albums at `disk.yandex.ru/a/...` and `disk.yandex.com/a/...`. OneDrive supports public photo albums from personal accounts using `1drv.ms/a/...` links. Yandex Disk `/d/` public folders, OneDrive folders, and OneDrive for Business/SharePoint are not implemented. Passwords and account login are not supported for these sources; links must open anonymously. No browser dependencies are required.

| Field | Purpose |
| --- | --- |
| `id` | Unique, case-insensitive, stable source name, up to 64 characters: letters, digits, `-`, `_`. Do not automatically derive a changing ID from the remote album name |
| `provider` | `immich`, `google_photos`, `icloud`, `yandex_disk`, or `onedrive` |
| `url` / `url_env` | Sharing link or the environment variable containing it; specify exactly one |
| `password` / `password_env` | Optional Immich password or the environment variable containing it |
| `target_album` | Exact frame album name; defaults to `DIVOOM_ALBUM` |

The sharing link itself grants access to photos. Actual links, passwords, and temporary tokens are not logged. `sources.toml`, `sources.local*.toml`, `.env`, and `data/` are excluded from Git and the Docker build context. If you use another name for a private file, add it to the exclusions yourself.

```sh
# All sources, one run:
.venv/bin/tfs sync

# All sources every SYNC_INTERVAL seconds:
.venv/bin/tfs run

# A different source list; global arguments precede the command:
.venv/bin/tfs --sources-file sources.local-family.toml sync

# Read source listings and frame status only:
.venv/bin/tfs sync --dry-run
```

`--sources-file` overrides `SOURCES_FILE`. The file and environment are read at startup; restart `run` after changing the source list. If `SOURCES_FILE` is unset, the legacy single-album configuration through `IMMICH_SHARE_URL` still works.

## Combining sources and removing items

Sources are grouped by `target_album`. Each provider first retrieves a complete listing and saves metadata. A combined set is then built for each destination. New photos are downloaded and uploaded one at a time; their local copies are deleted after verification on the frame.

In `mirror`, only previously tracked records absent from the entire combined set are removed. If two sources produce identical prepared WebP bytes, they share one file on the frame; removing the photo from one source does not remove that file's album membership while another source still uses it. Visually identical photos from different services may use different compression and produce different bytes; there is no visual similarity matching.

A listing error, incomplete page, or inaccessible sharing link blocks **the entire corresponding destination update** before uploads start. An image download error during transfer stops that destination; photos already added may remain, but removal of old items does not begin. Other destinations continue syncing. An old cache is not accepted as evidence of an inaccessible source's current state. A one-shot command returns a nonzero exit code on any error, even if other albums updated successfully; `run` retries in the next cycle.

Removing a source section removes its photos from the combined set. In `mirror`, previously tracked photos are removed from the shared destination if other sources still feed that album. In `append`, they stay. If a destination disappears entirely from configuration, the service no longer accesses or automatically clears it. Moving a source to another destination follows the same rules. Cloud source albums are read-only.

Removal uses `Photo/RemovePhotoFromAlbum` to remove destination album membership; the All Photos record and file in frame memory remain. Manually added photos are preserved. See [sync-modes.md](sync-modes.md).

## Autonomous playback

All destinations are uploaded, but only one native album can be active. `sync` and the first successful `run` cycle select the nonempty `DIVOOM_ALBUM` destination if available, otherwise the first successfully synced nonempty destination in TOML order. Later `run` cycles do not switch the screen.

To show all photos in one autonomous slideshow, give every source the same `target_album`. For separate destinations, configure album switching through Divoom. Stopping synchronization leaves photos and native playback on the frame.

## Cache and migration

- `data/sources/<id>/manifest.json` stores persistent source metadata; `photos/` and `videos/` hold temporary media until verified transfer. Remote IDs are hashed into safe filenames, and equal IDs from different services are kept separate.
- `data/targets/<hash>/manifest.json` stores the combined set for one destination, including source photo IDs.
- `data/targets/<hash>/device-state.json` records ownership of frame entries for that destination, validating the physical `DeviceId`, album, address, and file paths.
- Root `data/device-state.json` retains the previous screen for `restore` and the legacy single-source journal.

On first migration to a source list, the old journal is imported only if the Immich album, physical frame, and destination album match. Existing uploads are discovered and verified on the frame without being sent again. Temporary source files are deleted after frame verification. Manifests retain checksums for later cycles. Keep **manifests and journals in `data/`** between runs. See [storage and recovery](storage.md). One root filesystem lock prevents two processes using the same `DATA_DIR` from modifying the frame concurrently.

The included `compose.yaml` is configured for a source list, mounting `sources.toml` read-only and `data/` persistently. For a single Immich album, keep one TOML section. Prepare TOML and `.env`, then run `docker compose pull` and `docker compose up -d --no-build`. To build local source instead, use `docker compose up -d --build --pull never`.

## Adding a service

The contract is defined in `src/timesframesync/source.py`:

1. `album() -> Album` returns a stable album ID, name, **complete** list of `Asset(id, revision, name, kind="photo")`, and skipped-video count. Errors or incomplete responses must raise `SyncError`, not return an empty list.
2. `preview(asset_id) -> bytes` returns a decodable image for an item from the last listing. Temporary URLs may live in the provider object but do not enter the manifest. Revisions must reflect image changes without changing solely because a URL signature was refreshed.
3. For video, return `kind="video"` and implement `download_video(asset_id, destination: Path) -> None`: download the complete video in chunks to the supplied file or raise `SyncError`. Google Photos, iCloud Photos, Immich, and OneDrive currently implement this. The historical name `Album.photos` includes both media kinds; `photo_count` and `video_count` provide separate counts. Providers without video support still include videos in `skipped`.
4. Add the provider and URL validation to `sources_config.py`, its factory to `providers/__init__.py`, and tests for pagination, empty albums, errors, and secret-free error messages.

Combining sources, caching, frame uploads, and removal logic do not need to change. Current provider protocols are described in [source-api.md](source-api.md).
