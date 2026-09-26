# Divoom Times Frame Sync storage and recovery

**English** | [Русский](../ru/storage.md)

## Sync cycle

1. Read the complete listing of each source album; validate pagination and response completeness.
2. Atomically update the source manifest. Keep existing checksums for unchanged IDs and revisions. Delete unused temporary versions for that source.
3. Read the frame database for each destination album. Find known WebP and MP4 files by their SHA-256-derived names and verify their contents.
4. Download previews only for missing or new photos and prepare temporary 800×1280 JPEGs. Convert them to WebP in memory and save checksums in the manifest. For videos, download the playback file, convert it to MP4, and create a WebP cover.
5. Upload the WebP or the MP4 with its cover, or reuse an existing matching file. Verify its contents and membership in the destination album.
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
| `targets/<hash>/device-state.json` | Ownership of frame records, device, album, and file paths |
| `device-state.json` | Previous screen for `restore`; also the journal for single-source Immich mode |
| `manifest.json`, `photos/`, `videos/` | Manifest and temporary media for single-source Immich mode |
| `.lock` | Prevents concurrent access by multiple processes |
| `snapshot.webp` | Screenshot created by an explicit `snapshot` command |

Direct image URLs, CDN signatures, passwords, and tokens are not written to manifests. Album IDs and names are personal data; the entire `DATA_DIR` is excluded from the public repository.

A normal subsequent cycle checks a fresh source listing and the files on the frame. A missing temporary JPEG or MP4 alone does not trigger another cloud download. SHA-256 verification reads the entire frame file over the LAN, even when it has not changed. Large videos and slow Wi-Fi can make this take noticeable time. Verification bytes are not saved to disk. Persistent storage is needed for metadata, not copies of the entire photo library.

## Temporary video files

Names in `videos/` contain a hash of the album/item ID and the conversion revision. Extensions:

- `.download` — incomplete download; deleted on an ordinary error and overwritten on the next attempt after a crash.
- `.source` — fully downloaded video. Retained after an FFmpeg error to avoid downloading it again. Deleted once the MP4 and cover have been prepared.
- `.part.mp4` — incomplete conversion, deleted on an ordinary error. A completed result is atomically renamed to `.mp4`.
- `.mp4`, `.webp` — prepared video and cover. Deleted after verifying both files, the database video flag, album membership, and the saved journal.

Peak storage for one video is the downloaded file plus the converted MP4 and cover. This is temporary space, not a permanent album copy. Source video size is not limited by the screen resolution: Immich returns the original if no transcoded version exists. Google Photos supplies a transcoded download from the public album. Downloads, uploads, and SHA-256 calculations process files in chunks.

## Errors and restarts

A prepared local file is kept until the frame record is verified and the journal is saved. An interrupted upload, readback mismatch, missing album membership, or journal write failure leaves it available for retry. If the process stops after import but before recording ownership, the next run finds the file by checksum.

On an upload failure, new photos already added to the frame may remain in the destination album. Old photos are not removed until the entire current set has been verified. Failure to read any source listing blocks its destination before uploads begin.

Normal `mirror` removal changes only frame album membership and does not reclaim internal device storage. This is separate from cleaning up temporary files on the computer.

## Lost state

Keep manifests and journals between runs and back them up with the configuration. If only the journal is lost, the manifest checksums can identify current photos again. If both manifests and journals are lost, photos and videos are downloaded and converted again; matching prepared bytes allow reuse of existing frame files.

In both cases, photos already missing from the sources are not removed automatically because the service can no longer establish ownership. Different processing settings or codecs may produce different bytes and a separate copy of a visually identical photo.

When upgrading from a version with a permanent cache, existing JPEGs are used to compute checksums and deleted after verification on the frame. The `im-<24 hex>.webp` naming scheme is retained for compatibility and to prevent duplicate uploads.
