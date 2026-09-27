# Integrity checks and repair

**English** | [Русский](../ru/repair.md)

## Normal sync versus repair

`sync` and `run` compare complete source listings, saved media fingerprints, and the frame's database. Once an item has been verified, unchanged native ID/path, media type, device/album identity, and fingerprints allow it to be reused without cloud downloads, conversion, or media readback. New uploads and existing files without trusted verification records receive one full SHA-256 check. The first cycle after upgrading from 0.6.x therefore checks existing media once.

The frame database does not provide file hashes or sizes. A filename contains the first 24 hex digits of the prepared content's SHA-256, but the firmware does not update that name if bytes become damaged. Detecting silent corruption with intact database records requires a full read.

## Commands

Stop the running service first. `run`, `sync`, and both repair commands share the `DATA_DIR` lock. They must use the same configuration and persistent data directory.

```sh
.venv/bin/tfs repair --dry-run
.venv/bin/tfs repair
```

With Docker Compose:

```sh
docker compose stop
docker compose run --rm timesframesync repair --dry-run
docker compose run --rm timesframesync repair
docker compose up -d --no-build
```

`--dry-run` reads full file contents and reports problems without uploading, removing links, or switching playback. It still refreshes local source manifests and may prepare missing fingerprints when metadata has been lost. It does not change the verification journal. Exit status is 0 for a healthy audit and 1 for detected problems or errors.

`repair` checks every item currently listed by the configured sources. It compares full SHA-256 hashes for photos, MP4 files, and video covers, checks media types, and restores missing records or album links. Healthy files are not uploaded again. Shared physical files are checked once per invocation. The result includes counts and per-item problems/errors; a failed repair returns status 1.

Repair does not prune items that disappeared from source albums, change mirror/append settings, or start playback. Items unrelated to current configured media are left alone. Source listing failures skip the whole corresponding destination; independent destinations can continue. Network timeouts and HTTP failures other than 404 are reported as failed checks, not evidence of corrupt bytes.

## Replacement and interruptions

For a missing album link, repair reuses the verified file. For a corrupt or missing file, it prepares a replacement from the source. A missing fingerprint can also require downloading and converting the original again. Regenerating a video with a different encoder may produce different bytes; the new verified fingerprint is saved.

Repair imports a separate copy before unlinking damaged records. If the content's canonical filename is already present, the replacement uses `r<24 hex of SHA-256><2 hex generation>.webp` or `.mp4`. The generation starts at `00`, increases up to `ff`, and keeps filenames within firmware limits. Normal sync and state recovery prefer the latest generation while still requiring a complete hash check before first trust.

After the replacement has passed verification, its links are added to every affected ordinary album, including albums outside the source configuration. Only then are the damaged records unlinked from those albums. **Old damaged files remain in All Photos; repair does not globally delete files or reclaim frame storage.** Unrelated manual files are preserved.

Before upload or link changes, the journal records the planned replacement, original records, and destination links. If the operation is interrupted, keep `data/` and run `tfs repair` again. A successful replacement is reused; partial transfers resume. Normal sync refuses to reconcile a destination with an unfinished repair. Failed replacement copies are unlinked along with the original damaged record once a good replacement is verified.

If the source is unavailable, repair reports the error and preserves existing links. If an interrupted repair's source has disappeared or changed, or the device/album identity no longer matches, it stops with an explicit error rather than guessing. Restore the original configuration/source access and retry. Keep a backup of the journal if manual intervention is required.

See [storage](storage.md) for persistent metadata and [frame API](frame-api.md) for the commands used.
