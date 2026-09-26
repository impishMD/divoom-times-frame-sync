# Native Times Frame albums: LAN protocol

**English** | [Русский](../ru/protocol.md)

This description is based on Times Frame firmware behavior observed in September 2026. The investigation used device HTTP requests, database reads, and static analysis of the frame's own application. USB and Divoom cloud authentication are not required.

For every request used by the service, see [frame-api.md](frame-api.md). For removal policies, see [sync-modes.md](sync-modes.md).

## Confirmed operations

Ordinary display-control commands use `POST /divoom_api`. However, `Photo/*` commands on the inspected firmware can return `ReturnCode: 0` without executing. Their working endpoint is **`POST /upload`**, which passes JSON into the shared native-command queue.

Requests use multipart/form-data. The first part is JSON with `name="json"`, `filename="cmd.json"`, and `Content-Type: application/json`. Every part requires its own `Content-Length`, and line endings must be CRLF. For imports, the second part contains the image. Album membership changes require only the JSON part.

Common JSON fields:

```json
{
  "Command": "Photo/LocalAddToAlbum",
  "ReturnCode": 0,
  "DeviceToken": 123456,
  "LocalToken": 123456
}
```

Here, `ReturnCode: 0` is part of the incoming message parsed by the firmware's shared handler. An HTTP response with that code alone does not verify the result.

## Importing a photo

Additional fields:

| Field | Value |
| --- | --- |
| `ClockId` | ID of an existing native album |
| `ParentClockId`, `ParentItemId` | 0 for an ordinary album |
| `UserId` | Numeric sender ID |
| `SendTime`, `TakingTime` | Unix time in milliseconds |
| `PhotoX`, `PhotoY` | 0 |
| `PhotoWidth`, `PhotoHeight` | 800, 1280 |
| `PhotoIndex`, `PhotoTotalCnt` | 0, 1 — each file is a separate operation |
| `PhotoFlag` | Numeric batch identifier |
| `FileName` | Short filename matching the second multipart part |
| `PreviewFileName`, `PhotoTitle` | Empty strings |

The uploader first saves `/userdata/app_pic/<FileName>`. `Photo/LocalAddToAlbum` then moves it to `/userdata/app_pic/<year><month>/<FileName>`, registers it in `pic_info`, and links it through `album_pic`. Observed month values have no leading zero, for example `20269`.

Single-image WebP files use `im-<24 SHA-256 hex characters>.webp`, a 32-byte name. **Do not use long filenames or paths as filenames.** The firmware contains a 128-byte buffer for its cover-copy command. A long UUID-based name caused the frame application to restart after registering the image but before adding it to the album. Short names resolved this failure.

Import verification:

1. Fetch a fresh `GET /userdata/pic_db.bin` copy.
2. Check SQLite `quick_check` and find the short filename in `pic_info.path_name`.
3. Confirm the `album_id`, `pic_id` pair in `album_pic`.
4. Download the discovered photo path and compare its bytes with the upload.

The application reads SQLite only in memory. It neither edits the device database nor uploads it back.

## Importing video

The same `Photo/LocalAddToAlbum` command sends JSON, MP4, and a WebP cover in one multipart request. Both file parts are named `file`. `FileName` is `vi-<24 hex of MP4 SHA-256>.mp4`; `PreviewFileName` uses the same stem with `.webp`. Other fields match photo import, including the 800×1280 dimensions.

Firmware recognizes `.mp4` as video (`pic_info.pic_video_flag=1`) and moves the file and cover into the dated directory. The cover sits beside the MP4 without creating a separate photo record. Both SHA-256 values, the video flag, and album membership are verified. For encoding and transport details, see the [API reference](frame-api.md#importing-video-with-the-same-command).

In the inspected binary, the decoder reads `PreviewFileName` into the structure field at offset `0xb8` and `FileName` at `0x38`. Importer `0xff27c` moves a separate nonempty cover next to the video; handler `0x184e10` sets the video flag for `.mp4` files. These observations apply to the inspected firmware and are not a stable public Divoom contract.

## Album membership and playback

Add an already registered image:

```json
{
  "Command": "Photo/DevicePhotoToAlbum", "ReturnCode": 0,
  "ToClockId": 123456, "ParentClockId": 0, "ParentItemId": 0,
  "PhotoList": [42]
}
```

Remove an image from a specific album:

```json
{
  "Command": "Photo/RemovePhotoFromAlbum", "ReturnCode": 0,
  "ClockId": 123456, "ParentClockId": 0, "ParentItemId": 0,
  "PhotoList": [42]
}
```

Both commands use JSON-only multipart through `/upload`. Their effects are verified by reading the database. The synchronizer does not use global `Photo/DeletePhoto`; other albums may still need the files.

To activate an album, call `Device/ExitCustomControlMode`, then call `Channel/SetClockSelectId` twice with the album's `ClockId`, through `/divoom_api`. On this firmware, the double selection overrides the currently scheduled clock face. Verify with `Channel/GetClockInfo.ClockId`.

Slideshow timing and effects remain native Divoom settings. `Photo/SetAlbumConfig.SlideshowSpeed` is not measured in seconds: the handler maps a 0–100 scale to approximately 20–3 seconds. The old experimental `SLIDE_INTERVAL=30` setting therefore does not carry over.

## Reproducing the investigation

Inspected ELF: `/usr/bin/divoom_app`, SHA-256:

```text
d8eb027cfc0c4b3f6413497dda1d62926a1bfe86861990841298f33ebdaf7de2
```

Useful addresses in the ARM32 binary: `Photo/LocalAddToAlbum` decoder — `0x1a4b28`; importer — `0xff27c`; `/upload` handler — `0x23a3e4`; album membership changes — `0xd3520`; cover-copy command construction — `0x106940`.

The firmware binary, device databases, and photos are not included in this repository and are not dependencies of Divoom Times Frame Sync.
