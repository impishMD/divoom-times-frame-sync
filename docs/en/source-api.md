# Photo and video retrieval protocols

**English** | [Русский](../ru/source-api.md)

Behavior was checked on September 26–27, 2026. Every cloud request listed here is a read operation, including those technically using POST. The service does not upload, delete, or modify cloud photos.

The Divoom protocol is documented separately in [frame-api.md](frame-api.md). Adding sources does not introduce new frame methods.

## Immich

The API is accessed through a `/share/<key>` link, with the key passed as the `key` query parameter. Checked with Immich 3.2.2.

| Request | Purpose |
| --- | --- |
| `POST /api/shared-links/login` | JSON `{"password":"…"}`; obtain an access cookie for a password-protected shared link |
| `GET /api/shared-links/me` | Get access and link metadata without a password |
| `GET /api/timeline/buckets` | Get all album timeline buckets: `albumId`, `withStacked=false`, `order=asc` |
| `GET /api/timeline/bucket` | Read each bucket, additionally passing `timeBucket`; expects column arrays `id` and `isImage` |
| `GET /api/assets/<id>` | Check `IMAGE`/`VIDEO` type and trash state; get `updatedAt` and the original filename |
| `GET /api/assets/<id>/thumbnail?size=preview` | Download an image preview; conversion to 800×1280 happens locally |
| `GET /api/assets/<id>/video/playback` | Download video for a native frame album using the same key and cookie |

The client checks each bucket's size, ID uniqueness, and the total against `assetCount`. It reads type and revision for every item, including videos. An unknown type or trashed item stops the cycle. Cookies are refreshed before each album read; passwords and keys do not appear in error messages. Service documentation: [Immich API](https://api.immich.app/).

Video is downloaded only when an upload to the frame is needed. `/video/playback` returns Immich's transcoded version when available, otherwise the original file. The client requires `Content-Type: video/*`, nonempty data, and a completed transfer. It reads 1 MiB chunks into a temporary file; an interrupted download is not considered complete. This route uses the `AssetView` permission rather than permission to download the original; see the [Immich implementation](https://github.com/immich-app/immich/blob/main/server/src/services/asset-media.service.ts).

Local FFmpeg converts video to H.264/AAC, applies rotation and `IMAGE_FIT`, and produces an 800×1280 file without shortening its duration. A WebP cover is generated from the first frame. The cloud original is unchanged. Import settings are documented in the [frame API](frame-api.md#3-photolocaladdtoalbum).

## Google Photos

Public `https://photos.app.goo.gl/...` links and full `https://photos.google.com/share/...` URLs are supported. Google login and OAuth are not required. This is the public viewer's undocumented protocol, which can change independently of the official API.

The official Photos Library API no longer provides its previous shared-album access: Google discontinued the relevant operations on March 31, 2025. See [Google's announcement](https://developers.google.com/photos/support/updates).

### Album page

`GET <sharing link>` follows its redirect to `photos.google.com`. JSON is extracted from `AF_initDataCallback` without executing JavaScript:

- `data[1]` — first page of items;
- `data[2]` — next-page token, empty at the end;
- `data[3][0]` — stable album ID;
- `data[3][1]` — album name;
- `data[3][19]` — access key, kept only in memory;
- `data[3][21]` — total item count, including videos.

A login/consent redirect, missing album structure, or malformed data causes an error, not an empty album.

### Subsequent pages

```http
POST https://photos.google.com/_/PhotosUi/data/batchexecute?rpcids=snAcKc
Content-Type: application/x-www-form-urlencoded
```

The `f.req` field contains serialized JSON:

```json
[[["snAcKc", "[\"album-id\",\"page-token\",null,\"share-key\"]", null, "generic"]]]
```

Responses have an anti-XSSI prefix and may include length lines and JSON frames. Only a successful `wrb.fr` entry for RPC `snAcKc` is accepted; its string payload is decoded again as JSON. Pagination continues until the token is empty. Repeated tokens, duplicate IDs, or a count mismatch stop the update.

### Photos

The item ID is `item[0]`; `item[1]` contains the CDN URL, width, and height. The revision uses timestamp fields `item[2]`, `item[5]`, and dimensions. Temporary CDN URLs are excluded from the cache key. For a JPEG, append `=w2048-h2048` to the URL and issue a GET, checking `Content-Type: image/*` and successful decoding.

Key `76647426` in `item[9]` metadata identifies video. The item is returned as `Asset(kind="video")` and included in the complete album listing. This classification uses the page's internal format, not an official API guarantee. An independent primary description is in the author's [google-photos-album-image-url-fetch](https://github.com/vikas5914/google-photos-album-image-url-fetch) code. That project is neither a dependency nor executed by this service.

### Videos

For new or changed video, the CDN URL from `item[1][0]` is used with `=dv`:

```http
GET https://lh3.googleusercontent.com/<temporary-media-key>=dv
Accept-Encoding: identity
```

This redirects to `https://video-downloads.googleusercontent.com/...`, which returns `video/mp4`. Every redirect is checked before sending the request: only HTTPS and `googleusercontent.com` subdomains are allowed, without a username/password in the URL. At most six requests are permitted. The URL belongs to the public album; Google login, OAuth, and a Library API key are not used.

The `dv` parameter requests a transcoded video. Its purpose is described in [Google's media base URL documentation](https://developers.google.com/photos/library/guides/access-media-items#video-base-urls); applying the same parameter to public-viewer URLs is based on that viewer's current protocol. This download is intended for frame playback, not as an original-file backup.

Responses must have status `200` and `Content-Type: video/*`; covers, HTML, and partial `206` responses are rejected. Data is read in 1 MiB chunks into a temporary file. The result must be nonempty; if `Content-Length` is provided without HTTP compression, the byte count must match exactly. A network failure leaves the download incomplete, and the cache layer deletes the partial file. Responses are closed on success and failure.

Temporary URLs live only in memory and are refreshed on every album read. Revisions use the same timestamp fields and dimensions as photos. Subsequent cycles do not redownload unchanged videos. If Google is still processing a video or its URL is unavailable, that destination's cycle fails before removing old items. Videos otherwise follow the shared FFmpeg conversion, cover generation, upload, verification, and local cleanup process.

## iCloud Photos: new shared-album format

Supported links are `https://photos.icloud.com/shared/album/<key>`, using the new CloudKit public viewer. Legacy `icloud.com/sharedalbum/#...` links and temporary `icloud.com/photos/#...` links are not implemented and are explicitly rejected by configuration validation.

The protocol was derived from the network logic of Apple's public client `photos3/2634BuildBeta18`. Primary source: [Apple's official client](https://photos.icloud.com/applications/photos3/2634BuildBeta18/en-us/main.js). Its code is neither included in the project nor executed by the synchronizer.

### Resolving the link

```http
POST https://ckdatabasews.icloud.com/database/1/com.apple.photos.cloud/production/public/records/resolve
Content-Type: text/plain
```

Query: `remapEnums=true`, `getCurrentSyncToken=true`, `sharing_url_key=<key>`.

```json
{"shortGUIDs":[{"value":"<key>"}]}
```

From the single `results` item, the client reads `zoneID`, `databaseScope=SHARED`, the name at `share.fields["cloudkit.title"].value`, and `anonymousPublicAccess`:

- `databasePartition` — regional server, for example `https://p111-ckdatabasews.icloud.com:443`;
- `token` — temporary anonymous read token;
- the server also provides a TTL; the link is resolved again on each cycle.

The token is passed as the `publicAccessAuthToken` query parameter, kept only in memory, and never printed. A private or revoked album without anonymous access causes an error.

### Counts and pagination

All subsequent requests use:

```http
POST <databasePartition>/database/1/com.apple.photos.cloud/production/shared/records/query
Content-Type: text/plain
```

URL parameters are retained and extended with the anonymous token. The body contains `zoneID`, `resultsLimit`, and `query`.

For the count:

```json
{"query":{"recordType":"HyperionIndexCountLookup","filterBy":[{"fieldName":"indexCountID","comparator":"IN","fieldValue":{"value":["CPLAssetByAssetDateWithoutHiddenOrDeleted"],"type":"STRING_LIST"}}]},"zoneID":{"zoneName":"…","ownerRecordName":"…"},"resultsLimit":200}
```

Exactly one `IndexCountResult` record is expected, with integer `fields.itemCount.value`.

For items:

```json
{"query":{"recordType":"CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted","filterBy":[{"fieldName":"direction","comparator":"EQUALS","fieldValue":{"value":"ASCENDING","type":"STRING"}},{"fieldName":"startRank","comparator":"EQUALS","fieldValue":{"value":0,"type":"INT64"}}]},"zoneID":{"zoneName":"…","ownerRecordName":"…"},"resultsLimit":200}
```

Responses contain `CPLAsset` and `CPLMaster` records. The next request's `startRank` equals the number of **CPLAsset** records received, not the total record count. `CPLAsset.fields.masterRef.value.recordName` links the photo to its original. Masters are collected by `recordName`, and photos must not be duplicated. After the full list is read, another request verifies the count and `syncToken`; a changed snapshot stops the cycle.

### Images and videos

The client uses prepared `resJPEGFullRes` or `resJPEGMedRes` resources, preferring `CPLAsset` (rendered edits), then `CPLMaster`. It reads `fileChecksum` and `downloadURL` from the resource, replaces `${f}` with `photo.jpg`, and sends a GET to `*.icloud-content.com`. JPEG versions support HEIC photos without an extra decoder. EXIF orientation is applied during photo preparation.

The photo revision combines the chosen image's checksum and the asset's `recordModificationDate`; refreshing a CDN URL signature does not trigger another upload.

Videos are detected by `itemType`: `public.mpeg-4`, `com.apple.quicktime-movie`, `public.movie`, or `public.video`. The service first looks on `CPLAsset` for rendered edits, then on `CPLMaster`. Within each record it tries `resVidFullRes`, `resVidLargeRes`, `resVidMedRes`, `resVidSmallRes`, and finally `resOriginalRes`. A selected resource must have a nonempty `fileChecksum`, `downloadURL`, and a positive integer `size`. The video revision uses that resource's checksum and the asset's `recordModificationDate`, independently of the preview image and temporary URL signature.

To download video, `${f}` is replaced with `video.mp4` and a streaming GET is sent with `Accept-Encoding: identity`. Every URL and redirect must use HTTPS on a subdomain of `icloud-content.com`, with the default port and no embedded credentials. At most six requests are allowed. Only HTTP 200 with `video/*` or `application/octet-stream` and no content encoding is accepted. `Content-Length`, when present, must equal the resource's `size`; the received byte count must match it even when that header is absent. Partial responses, HTML, expired links, malformed resources, and interrupted downloads fail the sync attempt and preserve existing album items.

The complete video is converted locally to the frame's MP4 format; its WebP cover is generated from the video, so a JPEG preview is optional for video items. Live Photos remain still images. An unknown media type, missing JPEG for a photo, or missing downloadable video fails the entire source, preserving the previous album state. Video preparation, cleanup, mirror/append behavior, and repeat-run deduplication use the same path as other supported video sources.

## Yandex Disk: public photo albums

Supported links are `https://disk.yandex.ru/a/<key>`, also on the `.com` domain. These are photo albums; `/d/` public folders use a different, unsupported protocol. The official public-resource REST API does not expose these albums' contents. The service uses the public gallery protocol checked against the [Yandex client](https://yastatic.net/s3/psf/disk-public/_/public.764d985428246b5e.js).

### Opening an album

`GET <sharing link>` uses a browser-style `User-Agent`. JSON is extracted from the HTML's `store-prefetch` script without running JavaScript. `rootResourceId` identifies the root entry in `resources`, which must have `type=album`. Its `id`, `name`, and `modified` supply identity, name, and a change marker; `path`/`hash` are needed for later requests. `environment.sk` contains the temporary public-session token. Cookies remain in memory only.

Missing JSON, CAPTCHA, blocking, or access errors stop the read. The service does not solve CAPTCHAs or log into an account.

### Listing all photos

```http
POST https://disk.yandex.ru/public/api/fetch-album-list
Content-Type: text/plain
X-Requested-With: XMLHttpRequest
X-Retpath-Y: <sharing link>
```

The body is JSON encoded like `encodeURIComponent`:

```json
{"hash":"<album-path>","sk":"<session-token>","lastItemId":null}
```

On later pages, `lastItemId` is the previous page's last `albumItemId`. Responses contain a `resources` array and boolean `completed`. All pages are read until `completed=true`, even if the HTML already contains photos. Repeated cursors or IDs, empty incomplete pages, and malformed records are errors.

After enumeration, another page GET must return the same `id` and `modified`. A membership change during listing preserves the previous sync state until the next cycle.

### Images

Records use `id`, `name`, `modified`, and `meta`. `meta.mediatype=image` identifies a photo; `video` is skipped. Unknown types or malware flags fail the source. A GET fetches the signed `meta.xxxlPreview` or `meta.original` URL; only HTTPS Yandex CDN resources with `Content-Type: image/*` are accepted.

The revision comprises `modified`, `meta.size`, `meta.file_id`, and `meta.mimetype`; a refreshed temporary URL signature does not cause another download.

## OneDrive: public albums from personal accounts

Supported links are `https://1drv.ms/a/...`, including the modern `/a/c/<cid>/<key>` form. The implementation was checked with personal OneDrive migrated to `my.microsoftpersonalcontent.com`. Work/school accounts, SharePoint, and shared folders are not supported. If an older album returns another API format, the source fails instead of returning a partial list.

The protocol was established from requests made by the official public OneDrive viewer in a separate browser without Microsoft login, then reproduced with ordinary HTTP requests. Playwright was used only for research; the synchronizer neither starts nor depends on a browser. The anonymous `badger` protocol is not a documented Microsoft Graph contract.

### Anonymous token

```http
POST https://api-badgerp.svc.ms/v1.0/token
Content-Type: application/json
```

```json
{"appId":"073204aa-c1e0-4e66-a200-e5815a0aa93d"}
```

`appId` is the viewer application's public identifier, not a user secret. Responses include `authScheme=badger`, `token`, and `expiryTimeUtc`. A new token is requested at the start of every cycle and stored only in memory. User-browser cookies, a Microsoft account, password, and registered OAuth application are not required.

### Resolving a sharing link

The complete sharing link is encoded as UTF-8 → Base64URL without trailing `=`, prefixed with `u!`. This identifier format is described in [Microsoft's documentation](https://learn.microsoft.com/en-us/graph/api/shares-get?view=graph-rest-1.0); the anonymous exchange used here differs from Graph authentication.

```http
POST https://my.microsoftpersonalcontent.com/_api/v2.0/shares/<u!encoded-url>/driveitem?$select=id,parentReference
Authorization: badger <token>
Prefer: autoredeem
```

The body is empty. The request resolves the link for the current anonymous session without editing photos or sharing settings. The response provides the album ID and `parentReference.driveId`. After migration, the ID cannot be calculated from the short link's CID; the returned value must be used.

`Authorization` and `Prefer` are sent only to the API on the exact host `my.microsoftpersonalcontent.com`. These requests do not follow redirects. The link and token are excluded from errors and manifests.

### Metadata and complete listing

```http
GET https://my.microsoftpersonalcontent.com/_api/v2.1/drives/<drive-id>/albums/<album-id>
Authorization: badger <token>
Prefer: autoredeem
```

Expected fields are `id`, `name`, `eTag`, `lastModifiedDateTime`, `mediaAlbum.albumItemCount`, and `folder.childCount`. Both counts must agree. Missing `mediaAlbum` means an unsupported object, not an empty album.

```http
GET https://my.microsoftpersonalcontent.com/_api/v2.1/drives/<drive-id>/albums/<album-id>/children?top=200
Authorization: badger <token>
Prefer: autoredeem
```

The client reads the `value` array. It deliberately omits the viewer's `photo ne null` filter because completeness checks must include videos. If `@odata.nextLink` is present, the request uses that URL without adding the first page's parameters. Only the same API host and this album's `/children` path are allowed. Empty incomplete pages, repeated URLs or IDs, unknown media, and deleted/redirected records stop the cycle.

The unique item count, including both photos and videos, must match the reported count. A final metadata GET must return unchanged ID, count, `eTag`, and modification time.

The standard `value`/`@odata.nextLink` structure is documented in [Microsoft's child-listing reference](https://learn.microsoft.com/en-us/graph/api/driveitem-list-children?view=graph-rest-1.0); the `/albums` route and anonymous authentication come from the current public client.

### Photos and previews

Photos must have `image` and `file.mimeType=image/*`. The client uses `id`, `name`, `cTag`, `lastModifiedDateTime`, and `size`; the last three determine the cache revision. Temporary download URLs do not affect it.

Only for new or changed photos:

```http
GET https://my.microsoftpersonalcontent.com/_api/v2.1/drives/<drive-id>/items/<photo-id>/thumbnails?select=c2048x2048
Authorization: badger <token>
Prefer: autoredeem
```

The signed image URL comes from `value[0].c2048x2048.url`. The next CDN GET is sent **without the Authorization header**. The client checks an HTTPS Microsoft host, `Content-Type: image/*`, and local decoding. The requested size preserves aspect ratio and fits the photo into 2048×2048. Custom sizes are described in the [thumbnail documentation](https://learn.microsoft.com/en-us/graph/api/driveitem-list-thumbnails?view=graph-rest-1.0).

An expired token, unavailable preview, or HTTP error cancels the corresponding destination's update for this cycle. The next cycle obtains a new anonymous token and retries the read.

### Videos

A record with a `video` object is synced as video, even when it has no `image` object. It must contain `file.mimeType=video/*` or `application/octet-stream`, a positive integer `size`, nonempty `cTag` and `lastModifiedDateTime`, and a signed download URL. The public viewer supplies `@content.downloadUrl`; `@microsoft.graph.downloadUrl` is also accepted. An unavailable or malformed video fails the whole source snapshot rather than silently excluding the item.

The revision uses `cTag`, `lastModifiedDateTime`, and `size`, just as for photos. Temporary URLs stay in memory and are refreshed with each complete listing; a new URL signature does not cause another upload.

Only when a video needs downloading:

```http
GET <@content.downloadUrl>
Accept-Encoding: identity
```

This fetches the original video. No `Authorization` or `Prefer` header is attached, even if the download URL uses `my.microsoftpersonalcontent.com`. Microsoft documents the short-lived, preauthenticated URL and the absence of an authorization requirement in its [download reference](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content?view=graph-rest-1.0); the public viewer's anonymous API flow is described above.

The URL and every redirect must use HTTPS with the default port, no embedded credentials, and either the exact host `my.microsoftpersonalcontent.com` or a subdomain of `svc.ms`, `1drv.com`, `livefilestore.com`, or `onedrive.com`. At most six requests are allowed. The response must be HTTP 200 with `video/*` or `application/octet-stream`, without content encoding. `Content-Length`, when present, must match the listed size; the actual streamed byte count must match it regardless of the header. HTML, partial responses, size mismatches, expired links, and interrupted streams stop the attempt without pruning existing album members. The next cycle resolves fresh access and retries.

The downloaded file is converted locally to the frame's MP4 format, with a WebP cover generated from the video. No thumbnail request is needed for that cover. Upload verification, temporary-file cleanup, mirror/append behavior, and deduplication use the common video pipeline.

## General limitations

The Google, Apple, Yandex, and Microsoft public viewers are not stable third-party APIs. A schema change, HTTP 403/429, login page, network error, or expired URL stops the affected destination group; the next cycle retries. The synchronizer does not log into accounts, bypass CAPTCHAs, or automate a browser. It prepares media for display on the frame, not for original-quality backups.
