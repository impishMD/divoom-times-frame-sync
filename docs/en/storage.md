# Divoom Times Frame Sync storage and recovery

**English** | [Русский](../ru/storage.md)

## Sync cycle

1. Read the complete listing of each source album; validate pagination and response completeness.
2. Atomically update the source manifest. Keep existing checksums for unchanged IDs and revisions. Delete unused temporary versions for that source.
3. Read the frame database for each destination album. Find known WebP and MP4 files by their SHA-256-derived names (including recovery generations). Match saved verification records against the physical device, native record ID, path, media type, full SHA-256, and video cover hash.
4. Download previews only for missing or new photos and prepare temporary 800×1280 JPEGs. Convert them to WebP in memory and save checksums in the manifest. For videos, download the playback file, convert it to MP4, and create a WebP cover.
5. Upload the WebP or the MP4 with its cover, or reuse an existing matching file. Read its contents once and verify membership in the destination album. Previously verified, unchanged records need no media readback.
6. Atomically record ownership in `device-state.json`, then immediately delete the temporary JPEG or MP4/WebP pair. Continue with the next item.
7. Once all current photos and videos have been verified, apply the `mirror`/`append` policy to missing items.

A normal run does not download the entire album in advance. Each process handles one photo or video at a time. Files from earlier failed attempts may remain until a successful upload or a change to the source listing. The optional `tfs cache` command explicitly prefetches media and keeps prepared photos and videos locally until synchronization.

## Persistent files

| Path relative to `DATA_DIR` | Purpose |
| --- | --- |
| `sources/<id>/manifest.json` | Media kind, source ID and revision, prepared-file checksums, and frame filename |
| `sources/<id>/photos/*.jpg` | Temporary images; deleted after a verified transfer |
| `sources/<id>/videos/*` | Temporary input, MP4, and cover; see below |
| `targets/<hash>/manifest.json` | Combined current contents of a destination album |
| `targets/<hash>/device-state.json` | Ownership, full verification fingerprints and timestamps, device/album identity, and any pending repair |
| `device-state.json` | Previous screen for `restore`; also the journal for single-source Immich mode |
| `manifest.json`, `photos/`, `videos/` | Manifest and temporary media for single-source Immich mode |
| `.lock` | Prevents concurrent access by multiple processes |
| `snapshot.webp` | Screenshot created by an explicit `snapshot` command |

Direct image URLs, CDN signatures, passwords, and tokens are not written to manifests. Album IDs and names are personal data; the entire `DATA_DIR` is excluded from the public repository.

A normal subsequent cycle reads source listings and the frame database, with no media readback for unchanged verified items and no unnecessary journal rewrites. Missing native records trigger recovery from the source; missing album links are restored without reuploading. The frame database has no content hash or file size, so silent byte corruption with intact metadata requires an explicit [repair](repair.md). Full readback is performed for new uploads, untrusted entries, and repair checks; it is never saved as a second local media copy.

## Temporary video files

Names in `videos/` contain a hash of the album/item ID and the conversion revision. Extensions:

- `.download` — incomplete download; deleted on an ordinary error and overwritten on the next attempt after a crash.
- `.source` — fully downloaded video. Retained after an FFmpeg error to avoid downloading it again. Deleted once the MP4 and cover have been prepared.
- `.part.mp4` — incomplete conversion, deleted on an ordinary error. A completed result is atomically renamed to `.mp4`.
- `.mp4`, `.webp` — prepared video and cover. Deleted after verifying both files, the database video flag, album membership, and the saved journal.

Peak storage for one video is the downloaded file plus the converted MP4 and cover. This is temporary space, not a permanent album copy. Source video size is not limited by the screen resolution: Immich returns the original if no transcoded version exists. Google Photos supplies a transcoded download from the public album. iCloud Photos prefers prepared video resources and falls back to the original when necessary; its advertised resource size is verified during download. OneDrive and Yandex Disk download original videos and verify their sizes against the album metadata before conversion. Downloads, uploads, and SHA-256 calculations process files in chunks.

## Errors and restarts

A prepared local file is kept until the frame record is verified and the journal is saved. An interrupted upload, readback mismatch, missing album membership, or journal write failure leaves it available for retry. If the process stops after import but before recording ownership, the next run finds the file by checksum.

On an upload failure, new photos already added to the frame may remain in the destination album. Old photos are not removed until the entire current set has been verified. Failure to read any source listing blocks its destination before uploads begin.

Normal `mirror` removal changes only frame album membership and does not reclaim internal device storage. This is separate from cleaning up temporary files on the computer.

## Lost state

Keep manifests and journals between runs and back them up with the configuration. If only the journal is lost, the manifest checksums can identify current photos again. If both manifests and journals are lost, photos and videos are downloaded and converted again; matching prepared bytes allow reuse of existing frame files.

In both cases, photos already missing from the sources are not removed automatically because the service can no longer establish ownership. Different processing settings or codecs may produce different bytes and a separate copy of a visually identical photo.

When upgrading from a version with a permanent cache, existing JPEGs are used to compute checksums and deleted after verification on the frame. The `im-<24 hex>.webp` naming scheme is retained for compatibility and to prevent duplicate uploads.

## Verification journal upgrade

Version 0.7.0 adds `verified_files` to each destination's `device-state.json`. Entries contain native ID/path, media kind, full prepared-file SHA-256, video cover SHA-256, and `verified_at` (Unix seconds). The scope includes physical device ID, target album ID, and source album identity. Source IDs and revisions remain in manifests. Existing ownership records are retained; entries without verification metadata receive one full check on the next sync. No cloud download is needed if their saved fingerprints are present.

The journal also records `pending_repair` before uploading a replacement or changing links. An interrupted repair must be resumed with `tfs repair`; ordinary sync refuses to prune that destination while its repair is pending. See [repair](repair.md) for recovery naming, retries, and missing-source behavior.
