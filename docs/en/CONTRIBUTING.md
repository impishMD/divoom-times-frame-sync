# Contributing to Divoom Times Frame Sync

**English** | [Русский](../ru/CONTRIBUTING.md)

## Development setup

Use Python 3.11+ on macOS or Linux. Video integration tests require FFmpeg 6+ with `ffprobe` and the `libx264` encoder. These tests are skipped if the ffmpeg or ffprobe executable is unavailable. CI installs FFmpeg.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]' build
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
.venv/bin/python -m build
```

Tests use local HTTP servers and a simulated frame. They do not access real albums or require credentials. When changing sync behavior, check that manually added items are preserved and cover partial uploads, restarts, lost metadata, and temporary file cleanup.

## Project structure

Paths below are relative to `src/timesframesync/`:

- `providers/` — public album providers.
- `source.py` — the provider contract: a complete `Album`, `preview()` downloads, and optional `download_video()` support.
- `cache.py` — manifests, temporary media preparation, and cleanup.
- `video.py` — local FFmpeg conversion, duration checks, and cover generation.
- `multi_sync.py` — grouping sources by destination album.
- `sync.py` — uploads, verification, the ownership journal, and mirror/append modes.
- `frame.py` — the local Divoom API.
- `cli.py` — the `tfs` and `timesframesync` commands.

A provider must distinguish an accessible empty album from a failed or incomplete listing. IDs and revisions must be stable; temporary CDN URLs must not be part of the cache key. See the [provider guide](sources.md) and [storage guide](storage.md).

## Submitting changes

Describe the problem, the resulting behavior, and how you validated the change. Update both README languages when changing user-facing behavior or setup instructions. Keep the English and Russian versions of these contributor guidelines and the security policy aligned.

Use synthetic fixtures and examples. Do not add real sharing links, tokens, device dumps, or personal photos to source code, fixtures, logs, or issue reports. Keep `.env`, `sources.toml`, and `data/` local. Report vulnerabilities through the process in [SECURITY.md](SECURITY.md).

Maintainers: see [the release guide](releases.md) for CI, multi-platform image publishing, registry secrets, and tag-based releases.

## Licensing

Contributions are governed by the contribution terms of the project's [Apache License 2.0](../../LICENSE). Preserve existing copyright and license notices, including [NOTICE](../../NOTICE). Do not copy third-party code without retaining its required notices and checking license compatibility.
