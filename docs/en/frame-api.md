# Frame API used by Divoom Times Frame Sync

**English** | [Русский](../ru/frame-api.md)

This reference describes every network operation in [frame.py](../../src/timesframesync/frame.py) and the order in which [sync.py](../../src/timesframesync/sync.py) uses them. It is based on the project code and checks on a specific Times Frame on September 26, 2026. It describes observed firmware behavior, not an official specification for all Divoom devices. The inspected binary version and research details are in [protocol.md](protocol.md).

## Methods

| Operation | HTTP | Client method | Purpose |
| --- | --- | --- | --- |
| `Channel/GetConfig` | `POST /divoom_api` | `info()` | General display configuration |
| `Channel/GetClockInfo` | `POST /divoom_api` | `info()`, `play_album()` | Device ID and active album/clock face |
| `Photo/LocalAddToAlbum` | `POST /upload` | `import_photo()`, `import_video()` | Upload a photo or video to a native album |
| `Photo/DevicePhotoToAlbum` | `POST /upload` | `add_existing()` | Add an existing photo to an album |
| `Photo/RemovePhotoFromAlbum` | `POST /upload` | `remove_from_album()` | Remove a photo from one album |
| `Device/ExitCustomControlMode` | `POST /divoom_api` | `restore()` | Exit external display control |
| `Channel/SetClockSelectId` | `POST /divoom_api` | `select_clock()` | Select a native album/clock face |
| `Device/GetScreenSnapshot` | `POST /divoom_api` | `snapshot()` | Create a screenshot |
| Read photo database | `GET /userdata/pic_db.bin` | `inventory()` | Find albums, media, and membership records |
| Read file | `GET /userdata/<path>` | `fetch_file()`, `file_digest()` | Verify media files or download a screenshot |

## Transport and format

Base address: `http://<DIVOOM_HOST>:<DIVOOM_PORT>`, with port `9000` by default. The client uses a separate `requests.Session` with `trust_env=False`, so frame requests do not use proxies from the environment.

### Command envelope

`Frame.metadata()` builds this JSON:

```json
{
  "Command": "Channel/GetClockInfo",
  "ReturnCode": 0,
  "DeviceToken": 123456,
  "LocalToken": 123456
}
```

| Field | Type | Value |
| --- | --- | --- |
| `Command` | string | Exact, case-sensitive command name |
| `ReturnCode` | integer | Always `0` in the incoming message, as accepted by the inspected handler |
| `DeviceToken` | integer | `DIVOOM_TOKEN`, if configured |
| `LocalToken` | integer | The same `DIVOOM_TOKEN` value |

Example numbers are fictional. Tokens are omitted below for brevity; the client adds them to every POST command through this envelope. File GET requests use no JSON body or query-string tokens, matching the observed device behavior. Whether either token field can be omitted independently has not been investigated.

### `/divoom_api`

The body is compact JSON with `Content-Type: application/json`. This endpoint is used to read state, select a screen, and create a screenshot.

### `/upload`

The body is `multipart/form-data; boundary=<random boundary>`. The first part always contains JSON; a second part carries a file when needed. For video, a third part contains the WebP cover; both file parts use `name="file"`. Part headers:

```text
--<boundary>\r\n
Content-Disposition: form-data; name="json"; filename="cmd.json"\r\n
Content-Type: application/json\r\n
Content-Length: <JSON byte count>\r\n
\r\n
<JSON>\r\n
--<boundary>\r\n
Content-Disposition: form-data; name="file"; filename="im-<24 hex>.webp"\r\n
Content-Type: application/octet-stream\r\n
Content-Length: <file byte count>\r\n
\r\n
<WebP bytes>\r\n
--<boundary>--\r\n
```

Here, `\r\n` represents actual CRLF bytes. **Every part** needs its own `Content-Length`, in addition to the HTTP body's total length set by `requests`. Album membership commands omit the `file` part: the closing boundary follows JSON. Photos and JSON-only commands use `multipart()`; video uses `FileMultipart`. The latter calculates the total length in advance and reads files in chunks of up to 1 MiB, without HTTP chunked encoding. Files are closed on both success and exceptions.

On the inspected firmware, `Photo/*` commands sent through `/divoom_api` could be acknowledged without taking effect. Through `/upload`, they enter the shared command queue and execute. Changing the endpoint for photo commands therefore breaks synchronization.

### Responses, errors, and retries

A typical display-control response, abbreviated:

```json
{
  "Command": "Channel/GetClockInfo",
  "DeviceId": 123000001,
  "PacketFlag": 1700000000,
  "DeviceType": "Frame",
  "ReturnCode": 0,
  "ReturnMessage": ""
}
```

`DeviceId` is the numeric device identifier. `PacketFlag` is preserved as an opaque firmware field; the client does not use it to verify execution. Responses from `/upload` are usually shorter:

```json
{"ReturnCode": 0, "ReturnMessage": ""}
```

`_post()` checks HTTP status, parses JSON, and requires `ReturnCode == 0`. One observed error was `ReturnCode: 1`, `ReturnMessage: "command timeout"`. Other codes have not been mapped; any nonzero or missing code is an error.

Default connection/read timeouts are `(5, 60)` seconds. Video uploads use `(5, 300)` and streaming file reads use `(5, 90)`. These are network-operation timeouts, not a total sync duration limit. POST requests are not retried automatically: an uncertain import result must first be checked against the database to avoid duplicates. `run` retries the entire cycle after `SYNC_INTERVAL`; `sync` returns an error.

A successful ACK only confirms message acceptance. Photo commands are also verified against the database and file; album selection is verified through `Channel/GetClockInfo`.

## 1. `Channel/GetConfig`

**HTTP:** `POST /divoom_api`. **Used by:** `Frame.info()`; `status`, `sync --dry-run`, and state reads before synchronization.

Request with no additional parameters:

```json
{"Command": "Channel/GetConfig", "ReturnCode": 0}
```

The response includes the common envelope and may contain `RotationFlag`, `ClockTime`, `GalleryTime`, `SingleGalleyTime`, `ChannelIndex`, `StartUpClockId`, and `GalleryShowTimeFlag`. `info()` returns the entire response under `config`. The program does not interpret or change these settings; their complete semantics have not been established in this project.

This is a read operation. Success uses the common HTTP/JSON/`ReturnCode` checks. Repeated reads do not change the album or selected screen. On failure, `info()` does not return a partially populated result.

## 2. `Channel/GetClockInfo`

**HTTP:** `POST /divoom_api`. **Used by:** `Frame.info()` and verification in `Frame.play_album()`.

Request:

```json
{"Command": "Channel/GetClockInfo", "ReturnCode": 0}
```

Abbreviated response example:

```json
{
  "Command": "Channel/GetClockInfo",
  "ReturnCode": 0,
  "DeviceId": 123000001,
  "DeviceType": "Frame",
  "Brightness": 100,
  "ClockId": 123456
}
```

| Response field | Use |
| --- | --- |
| `DeviceId` | Must be an integer; together with the source and destination album IDs, scopes the local journal |
| `ClockId` | ID of the active clock face **or native photo album**; saved for `restore` and checked after album selection |
| `Brightness` | Passed through unchanged in diagnostic output |

Reading does not switch the screen. `play_album()` requires the returned `ClockId` to match the destination album ID. A missing or different ID after selection is an error even if the ACK succeeded.

## 3. `Photo/LocalAddToAlbum`

**HTTP:** `POST /upload`, JSON and a file, plus a cover for video. **Used by:** `Frame.import_photo()` / `Frame.import_video()` when the database has no file with the expected short name.

Example JSON part:

```json
{
  "Command": "Photo/LocalAddToAlbum",
  "ReturnCode": 0,
  "ClockId": 123456,
  "ParentClockId": 0,
  "ParentItemId": 0,
  "UserId": 0,
  "SendTime": 1700000000000,
  "TakingTime": 1700000000000,
  "PhotoX": 0,
  "PhotoY": 0,
  "PhotoWidth": 800,
  "PhotoHeight": 1280,
  "PhotoIndex": 0,
  "PhotoTotalCnt": 1,
  "PhotoFlag": 123456789,
  "FileName": "im-0123456789abcdef01234567.webp",
  "PreviewFileName": "",
  "PhotoTitle": ""
}
```

| Field | Type | Value sent by the client |
| --- | --- | --- |
| `ClockId` | integer | ID of an existing ordinary album from `album_head` |
| `ParentClockId`, `ParentItemId` | integer | `0`; nested clock-face items are not used |
| `UserId` | integer | `DIVOOM_USER_ID`, or `0` for a local sender if omitted |
| `SendTime` | integer | Current Unix time in milliseconds |
| `TakingTime` | integer | The same current time; the source service's capture date is not transferred yet |
| `PhotoX`, `PhotoY` | integer | `0`, `0` |
| `PhotoWidth`, `PhotoHeight` | integer | `800`, `1280`, the prepared image dimensions |
| `PhotoIndex`, `PhotoTotalCnt` | integer | `0`, `1`; one file per operation |
| `PhotoFlag` | integer | Random integer from `0` to `2^31 - 2`, identifying the batch |
| `FileName` | string | Filename only; matches the second multipart part's `filename` |
| `PreviewFileName` | string | Empty for photos; the third part's WebP filename for videos |
| `PhotoTitle` | string | Empty; captions are not transferred |

`device_photo()` encodes a standalone RGB WebP with `quality=90`, `method=4`. Its name is `im-` + the first 24 hexadecimal SHA-256 characters **of the transmitted WebP bytes** + `.webp`, totaling 32 ASCII bytes. Preparation for 800×1280 takes place in a temporary file before upload. After the photo is verified and the journal saved, the temporary file is deleted while checksums remain. Identical bytes produce the same name and allow reuse.

`multipart()` permits filenames of at most 32 characters from `a-z`, `0-9`, `-`, `_`, and `.`. Long names are unsafe on this firmware: the cover-copy command uses a 128-byte buffer, and a long path caused the frame application to restart during investigation. Paths, spaces, and arbitrary characters are not sent as filenames.

Side effects include receiving the file in `/userdata/app_pic/`, moving it into a year/month directory, registering it in `pic_info`, creating an `album_pic` link, and internal preview/cover processing. The final path comes from the database rather than being constructed on the computer.

**Verification:** after ACK, the client polls the database every 0.5 seconds for a record with that filename. The waiting window is 15 seconds, potentially extended by network timeouts. It then downloads the file and compares bytes. If the record exists without album membership, it calls `Photo/DevicePhotoToAlbum`. A missing record, byte mismatch, or failed album addition returns an error.

**Retry:** import itself is not considered idempotent. The next cycle first looks up the filename and verifies bytes. A matching file is reused; multiple records with the same name stop synchronization because the result is ambiguous.

### Importing video with the same command

`Frame.import_video()` sends **one** multipart request with three parts in this order:

1. `Photo/LocalAddToAlbum` JSON with the same numeric fields as a photo.
2. MP4, `name="file"`, `filename="vi-<24 hex>.mp4"`.
3. WebP cover, `name="file"`, `filename="vi-<same 24 hex>.webp"`.

JSON differences:

```json
{
  "FileName": "vi-0123456789abcdef01234567.mp4",
  "PreviewFileName": "vi-0123456789abcdef01234567.webp"
}
```

These are only the changed fields, not a complete command. The filename hash is calculated from the **prepared MP4**; the MP4 name is 31 ASCII bytes and the cover name is 32. Both file parts have `Content-Type: application/octet-stream` and their own `Content-Length`. Firmware identifies video by `.mp4`, registers one record with `pic_video_flag=1`, and moves the cover next to the MP4 with a `.webp` extension. The cover must not be imported separately as a photo.

Divoom Times Frame Sync preparation settings:

| Parameter | Value |
| --- | --- |
| Container | MP4, `+faststart` |
| Video | H.264, Main profile, level 4.0, `yuv420p`, 30 fps |
| Dimensions | 800×1280; metadata rotation applied, `IMAGE_FIT=contain/cover` |
| Quality | `libx264`, CRF 23, medium preset, 4 Mbit/s maxrate, 8 Mbit buffer |
| Audio | First audio track if present: AAC, 128 kbit/s, stereo, 48 kHz |
| Cover | First prepared video frame → PNG in memory → RGB WebP, quality 90, method 4 |
| Duration | No clipping; compared with the input after processing, tolerance max(0.5 s; 1%) |

FFmpeg and ffprobe run locally. Conversion has a one-hour timeout; metadata reads and cover generation each have a 90-second timeout. An error stops the current synchronization before removing old records. Flag documentation: [FFmpeg](https://ffmpeg.org/ffmpeg.html).

Video upload HTTP timeouts are 5 seconds to connect and 300 seconds waiting for response data. These are network timeouts, not video duration limits. Database polling and membership checks still follow ACK. Verification additionally requires `pic_video_flag=1`, a matching MP4 SHA-256, and a separately matching cover SHA-256. File verification reads responses in chunks instead of retaining the whole MP4 in memory.

The next cycle uses saved checksums to find and verify the video without downloading it from the source or running FFmpeg again. Video autoplay, mute, and volume remain frame settings and are not changed during import.

## 4. `Photo/DevicePhotoToAlbum`

**HTTP:** `POST /upload`, JSON part only. **Used by:** `Frame.add_existing()` for an existing photo missing from the destination album.

```json
{
  "Command": "Photo/DevicePhotoToAlbum",
  "ReturnCode": 0,
  "ToClockId": 123456,
  "ParentClockId": 0,
  "ParentItemId": 0,
  "PhotoList": [42]
}
```

| Field | Type | Value |
| --- | --- | --- |
| `ToClockId` | integer | Destination album ID; specifically `ToClockId`, not `ClockId` |
| `ParentClockId`, `ParentItemId` | integer | `0` |
| `PhotoList` | array of integer | `pic_info` record IDs, not source service IDs or filenames; the current client sends one ID |

This command links an existing photo to an album without transferring the file again. Other album links remain intact.

After ACK, `wait_members()` reads the database until all required IDs appear in the destination album, using the same 15-second window and 0.5-second pause. The synchronizer checks membership before sending, so a subsequent cycle does not add an existing link again. It does not blindly repeat commands without reading the database.

## 5. `Photo/RemovePhotoFromAlbum`

**HTTP:** `POST /upload`, JSON part only. **Used by:** `Frame.remove_from_album()` in `mirror` mode.

```json
{
  "Command": "Photo/RemovePhotoFromAlbum",
  "ReturnCode": 0,
  "ClockId": 123456,
  "ParentClockId": 0,
  "ParentItemId": 0,
  "PhotoList": [42, 43]
}
```

| Field | Type | Value |
| --- | --- | --- |
| `ClockId` | integer | Album ID from which photos are removed |
| `ParentClockId`, `ParentItemId` | integer | `0` |
| `PhotoList` | array of integer | Sorted photo IDs from the frame database |

The command removes photos from the specified album. It is not used for global file deletion: the `pic_info` record, All Photos, and other album links remain. The client does not send empty ID sets. Large removals are split into batches of 100 IDs; this is a client limit, and the firmware limit is unknown.

After ACK, `wait_members()` requires all removed IDs to be absent from `album_pic` for that album. The file is expected to remain and is not considered an error. If verification fails, the cycle returns an error and preserves the ownership journal for a later attempt.

Before removal, the synchronizer checks that `device-state.json` tracks the record for the same device, destination album, and sync scope; that its ID and path still match; and that it remains in the destination but is absent from the current combined set of prepared images. See [sync-modes.md](sync-modes.md) for the full algorithm and mode switching.

## 6. `Device/ExitCustomControlMode`

**HTTP:** `POST /divoom_api`. **Used by:** `Frame.restore()`, before enabling a native album and by the `restore` CLI command.

```json
{"Command": "Device/ExitCustomControlMode", "ReturnCode": 0}
```

There are no additional parameters. The command exits external display control, which the old experimental display method used. It does not upload or remove photos and does not select an album by itself.

The response is checked by `_post()`. When enabling an album, `Channel/SetClockSelectId` follows. CLI `restore` selects the saved `previous_clock_id`, if available. The client does not separately verify custom-control state after exiting; the final `play_album()` check verifies the selected `ClockId`.

## 7. `Channel/SetClockSelectId`

**HTTP:** `POST /divoom_api`. **Used by:** `Frame.select_clock()` for album selection and restoring the previous screen.

```json
{"Command": "Channel/SetClockSelectId", "ReturnCode": 0, "ClockId": 123456}
```

`ClockId` is an integer identifying an existing album or clock face. Before selecting the destination, its ID is found in the database by the exact `DIVOOM_ALBUM` name. Exactly one ordinary album with that name must exist.

On the inspected firmware, two consecutive calls overrode the currently scheduled clock face. `select_clock()` therefore sends the command twice and checks both ACKs. The service does not reconfigure future schedules or the frame's startup screen.

`play_album()` then calls `Channel/GetClockInfo` and compares `ClockId`. CLI `restore` uses only `select_clock()` without this extra check. Repeated selection may affect playback, so `run` selects an album only on the first successful nonempty cycle, rather than every source check.

## 8. `Device/GetScreenSnapshot`

**HTTP:** `POST /divoom_api`. **Used by:** `Frame.snapshot()` and the `snapshot` CLI command.

```json
{"Command": "Device/GetScreenSnapshot", "ReturnCode": 0}
```

Abbreviated response from the inspected device:

```json
{
  "Command": "Device/GetScreenSnapshot",
  "ReturnCode": 0,
  "snapShotPath": "/userdata/app_pic/snapshot.webp"
}
```

Note the case of `snapShotPath`. The command creates a file on the device; the JSON response does not contain image bytes. The client waits 2 seconds and downloads that path. If the field is missing, it falls back to `/userdata/snapshot.webp`.

The file must be under `/userdata/`. A download HTTP 404 becomes a “snapshot file not found” error. The CLI atomically saves the bytes to `DATA_DIR/snapshot.webp`. The command does not switch photos. Later snapshots may overwrite the same file on the frame and computer; the current client has no separate snapshot ID or freshness check.

## 9. `GET /userdata/pic_db.bin`

**HTTP:** GET without JSON. **Used by:** `Frame.inventory()` for album discovery, file reuse, and verification of changes.

HTTP 200 must contain a binary SQLite database. The client deserializes it into `:memory:`, enables `PRAGMA query_only=ON`, requires `PRAGMA quick_check == "ok"`, and runs these queries:

```sql
SELECT * FROM album_head;
SELECT id, path_name FROM pic_info;
SELECT album_id, pic_id FROM album_pic;
```

Data used:

| Table | Interpretation |
| --- | --- |
| `album_head` | First three columns in the inspected schema: ID, type, name. Type `0` is an ordinary album; type `1` was observed for All Photos |
| `pic_info` | `id`: photo ID for `PhotoList`; `path_name`: absolute file path; `pic_video_flag=1`: video |
| `album_pic` | `album_id`, `pic_id` membership pairs |

The result is `Inventory(albums, photos, members, video_ids)`. `photos` contains all media; `video_ids` is a separate set of video IDs. On an older schema without `pic_video_flag`, photos remain available but videos cannot pass the required type check. Album selection matches the exact, case-sensitive name among type `0` albums only. No match or two matches is an error; the program neither creates an album automatically nor substitutes another one.

Parsing or `quick_check` failures trigger up to three read attempts with 0.5-second pauses. HTTP 404 means the required firmware interface is unavailable and fails immediately. Network errors also propagate to the caller.

Downloading a live database is not a SQLite backup transaction. Integrity checks detect a corrupt snapshot but do not freeze the frame's state throughout synchronization. Fresh reads therefore follow changes. Neither the database nor an edited copy is written back to the device.

## 10. `GET /userdata/<path>`

**HTTP:** GET without JSON. **Used by:** `Frame.fetch_file()` for the database, prepared photos, and screenshots; `Frame.file_digest()` for MP4 and cover verification.

Example prepared photo path:

```text
/userdata/app_pic/20269/im-0123456789abcdef01234567.webp
```

Paths come from the database or `Device/GetScreenSnapshot`. Only strings starting with `/userdata/` are allowed; a `..` path component is rejected. HTTP 200 returns `bytes`, 404 returns `None`, and other unsuccessful statuses or network errors raise `SyncError`.

For photos, SHA-256 of the read bytes must match the saved WebP checksum or the current `device_photo()` result. An existing file mismatch stops synchronization: the client neither overwrites it nor creates another record with the same name. Successful downloading is sufficient for screenshots.

`file_digest()` permits the same paths and returns hexadecimal SHA-256, or `None` for 404. It uses `stream=True`, reads 1 MiB chunks, and has a 5-second connection and 90-second read timeout. Interruptions, HTTP errors, or checksum mismatches retain the prepared local file for retry and block removal of old records. The video cover uses the MP4 path with a `.webp` extension.

## Sequence of one cycle

1. Read complete listings for every source feeding the destination and save manifests. An error here skips the destination before sending frame mutations; independent destinations may still update.
2. Read the frame database, find the album by name, and request `Channel/GetConfig` and `Channel/GetClockInfo`.
3. For each photo and video, look up its saved filename and verify SHA-256. Prepare temporary JPEG/WebP files for new or missing photos, or an MP4 with cover for videos. Call `Photo/LocalAddToAlbum` / `Photo/DevicePhotoToAlbum` as needed.
4. After each verified item, save its journal record and delete the temporary JPEG or MP4/cover pair. Reread the database and check the destination album ID before removing missing records.
5. In `mirror`, remove tracked missing images through `Photo/RemovePhotoFromAlbum`. In `append`, keep them and their journal entries.
6. Verify current image membership and save the journal. If needed, activate the album by exiting custom control and selecting `ClockId`.

`sync --dry-run` reads status rather than calculating planned removals. `snapshot` and `restore` are separate diagnostic/control commands. Unused experimental commands and firmware binary access are not needed during normal operation.
