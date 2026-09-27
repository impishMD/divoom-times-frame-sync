# Album sync modes

**English** | [Русский](../ru/sync-modes.md)

The mode applies to `sync` and `run`. It determines what happens to previously tracked photos that are no longer in a source album or the combined source set for a destination. Storage and autonomous playback on the frame work the same way in both modes.

These rules apply equally to photos and videos. Video is supported from Google Photos, iCloud Photos, Immich, OneDrive, and Yandex Disk; media kind does not change ownership, album removal, or recovery rules.

## Choosing a mode

| Mode | New photos | Missing photos | Previous version of a changed photo |
| --- | --- | --- | --- |
| `mirror` | Add | Remove from the destination album | Remove after verifying the new version |
| `append` | Add | Keep | Keep alongside the new version |

The default is `mirror`. Settings take precedence in this order:

1. Command-line `--sync-mode`.
2. `SYNC_MODE` in the process environment.
3. `SYNC_MODE` in `.env` or the file selected by `--env-file`.
4. The default value, `mirror`.

An unknown value causes an error before synchronization starts.

Run once and remove missing photos:

```sh
.venv/bin/tfs sync --sync-mode mirror
```

Keep syncing in the same mode:

```sh
.venv/bin/tfs run --sync-mode mirror
```

Only add photos:

```sh
.venv/bin/tfs run --sync-mode append
```

Persistent configuration in `.env`, including Docker Compose:

```dotenv
SYNC_MODE=mirror
SYNC_INTERVAL=300
```

Restart the process after changing `.env`; settings are read at startup. For an existing container, use `docker compose up -d --no-build --force-recreate`. When building updated local source, use `docker compose up -d --build --pull never`.

## What removal means in `mirror`

For example, Immich contains A and B, while the destination frame album contains service-uploaded A and B plus manually added C. After A is removed from Immich's album, the next successful cycle leaves B and C in the destination. Removing a photo from the source album is sufficient; its original does not need to be deleted from the Immich library.

`Photo/RemovePhotoFromAlbum` removes the link between a photo and the **destination album**. The file stays in the frame's memory and may remain visible in All Photos or other albums. This does not reclaim storage or perform a global deletion through `Photo/DeletePhoto`.

The service removes only records it tracks as its own. All of these conditions must hold:

- In `device-state.json`, the device (`DeviceId`), destination album ID, and source album ID (legacy single-source configuration) or stable destination group ID (multiple sources) match. The journal is also bound to the frame's address and port.
- The record is known to the local journal: it was imported or reused after verifying its bytes.
- Its ID and path in the fresh frame database match the journal. This protects unrelated files if a numeric ID is reused.
- The photo is still in the destination album but is absent from the current combined set of prepared images.

If two source photos produce identical prepared WebP bytes, they share one frame record. That record is kept while at least one of those photos remains in at least one source.

## Empty albums and errors

A successfully read **empty** album is valid. In `mirror`, all tracked photos are removed from the destination; manually added photos stay. In `append`, photos and journal entries remain. An empty source set does not switch the active screen; the firmware determines how an empty album appears.

A source access error is not an empty album. The complete listing is read and item counts are checked first. Previews are downloaded one at a time only when an upload is needed. If listing fails, the CLI does not update the frame using an old manifest. In the normal cycle, removals begin only after all current photos have been verified on the frame.

Frame changes are not a single transaction. If some new photos were added before a later upload failed, they may remain, but old-photo removal has not started. If a removal batch fails, earlier batches may already have completed. The journal preserves information for the next cycle; the service rereads the database and proceeds from its actual state.

When several sources feed one album, a listing error in any of them blocks the entire destination before uploads start. Other destinations continue syncing. A successfully read empty source contributes no items to the combined set, while photos from other sources remain.

## Switching modes

In `append`, the service continues tracking retained photos in the journal. After switching to `mirror`, they are removed if still absent from the sources. This also works after restarts and after several cycles with an empty source album.

In the legacy single-source configuration, changing the device, source album, or destination album changes the journal scope. With `SOURCES_FILE`, each destination has its own journal; changing its source list changes the combined set. Removing a source section in `mirror` may remove its photos from a shared destination. A destination removed entirely from configuration is no longer updated. See [sources.md](sources.md) for the full rules.

If the journal (`data/device-state.json` in the legacy layout or `data/targets/<hash>/device-state.json` for multiple sources) is lost, current images can be found on the frame by name and verified by bytes. Ownership cannot be recovered this way for photos already missing from the sources: the service does not delete them based only on a filename prefix. Keep the journal between runs and mount persistent `./data:/app/data` storage in containers.

## Checking operation

`status` shows the configured mode and the destination's photo and video counts, including manual additions. `sync --sync-mode mirror --dry-run` shows status with the selected mode without writing to the frame. It does not list planned removals.

The default `INFO` level shows service startup/shutdown and one summary after the first successful cycle or a cycle that uploads media, restores album membership, or removes items:

```text
INFO Service started; sync interval=30s
INFO Sync cycle complete: 87 items, 1 uploaded, 0 removed; 9.0s
```

Unchanged cycles stay quiet. During `run`, a successful cycle is reported again after at least 10 minutes without an INFO summary; the next successful cycle supplies this heartbeat. A one-shot `sync` always reports its result. Warnings, errors, and detected damage remain visible immediately. Failed or partially failed cycles never produce a successful heartbeat.

`items` is the total number of current source items across completed destinations (the same media in two destinations counts twice). `uploaded` and `removed` count changes made in this cycle. A nonzero `linked` counter means media already on the frame was added back to a destination without uploading it again. For per-destination counts and all operation timings, enable DEBUG:

```sh
.venv/bin/tfs --log-level DEBUG run
```

Example detailed log for a completed cycle:

```text
DEBUG Source family (immich), album Family: listed 2 photos, 1 videos; 0 prefetched, 0 videos skipped; 0.5s
DEBUG Native album Photos (123456), mode=mirror: 2 photos, 1 videos, 3 matched metadata, 0 content checked, 0 downloaded, 0 uploaded, 0 linked, 1 removed, 0 missing items retained; 1.2s
INFO Sync cycle complete: 3 items, 0 uploaded, 1 removed; 1.8s
```

At DEBUG, both `sync` and each `run` cycle report three elapsed times, measured with a monotonic clock:

- `Source` (or `Immich album` in single-source mode): reading that source album's complete listing and updating its local manifest. Explicit `cache` runs also include prefetching.
- `Native album`: synchronizing one destination, including frame database reads, reconciliation, any necessary source downloads and conversion, uploads, verification, journal writes, and playback selection when requested. It excludes source listing and is not the duration of one HTTP request.
- `Sync cycle`: the entire operation across all configured sources and destinations, including preparation between stages. It excludes lock acquisition and the `SYNC_INTERVAL` wait, which starts after this line.

The total can differ slightly from the sum of the displayed stage times because of intermediate work and rounding. A partial failure ends with `Sync cycle finished with N errors`; an exception that stops the cycle produces `Sync cycle failed`, both with elapsed time.

Individual operations report their duration at DEBUG. For a new photo, for example:

```text
Downloading photo: complete; 0.4s
Preparing photo: complete; 0.2s
Uploading native photo: complete; 1.3s
Checking photo contents in album Photos: complete; 0.7s
Removing temporary photo files: complete; 0.0s
```

Video download, MP4 conversion, cover generation, local checksums, upload, and video/cover verification each have timings. Album membership changes, playback selection, repair operations and summaries, cache preparation, status, screenshot capture, and display restoration also report durations. Timings cover whole operations, including required waiting and retries; nested timings must not be added to their parent duration.

`complete` is emitted after an operation finishes. Long video operations first announce `started; 0.0s elapsed` and later print the final duration; a streaming readback progress line reports MiB read and elapsed time. `failed`, `interrupted`, and `problems found` distinguish errors, cancellation, and completed checks that found damage. Durations use seconds rounded to one decimal place, so a very short operation can show `0.0s`. Reused media do not acquire extra verification or upload operations just to print timings.

DEBUG also includes frame API calls, database reads, import/membership waits, local manifests, journal writes, and temporary-file maintenance. Successful per-file operations and source/destination details stay at DEBUG even when a cycle makes changes. Explicit commands such as `repair`, `cache`, `status`, `snapshot`, and `restore` retain INFO completion summaries. DEBUG is enabled only for application logging; HTTP library debug logging, which can contain private URLs, remains disabled.

`photos` and `videos` count unique prepared photos and videos in the current combined source set; `removed` counts records removed from the destination; `missing items retained` counts tracked records kept by `append` despite their absence from the current set. Manually added photos are not included in these counters.

For removal commands and verification, see [frame-api.md](frame-api.md).

`matched metadata` counts items reused without media reads; `content checked` counts full integrity checks (including new uploads). The initial upgrade checks existing files once. Use [repair](repair.md) for a full audit independently of mirror/append.
