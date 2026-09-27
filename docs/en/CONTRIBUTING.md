# Contributing to Divoom Times Frame Sync

**English** | [Русский](/docs/ru/CONTRIBUTING.md)

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

A provider must distinguish an accessible empty album from a failed or incomplete listing. IDs and revisions must be stable; temporary CDN URLs must not be part of the cache key. See the [provider guide](/docs/en/sources.md) and [storage guide](/docs/en/storage.md).

## Submitting changes

Describe the problem, the resulting behavior, and how you validated the change. Update both README languages when changing user-facing behavior or setup instructions. Keep the English and Russian versions of these contributor guidelines and the security policy aligned.

Edit documentation in `docs/en/` and `docs/ru/`. The root `README.md`, `CONTRIBUTING.md`, and `SECURITY.md` are symlinks to the English originals. In these three English files, start local Markdown link paths with `/` so GitHub resolves them from the repository root in both locations.

Use synthetic fixtures and examples. Do not add real sharing links, tokens, device dumps, or personal photos to source code, fixtures, logs, or issue reports. Keep `.env`, `sources.toml`, and `data/` local. Report vulnerabilities through the process in [SECURITY.md](/docs/en/SECURITY.md).

Maintainers: see [the release guide](/docs/en/releases.md) for CI, multi-platform image publishing, registry secrets, and tag-based releases.

## Helm chart development

The chart is in `charts/divoom-times-frame-sync/`. With Helm and the Python development environment available:

```sh
.venv/bin/python -m pip install PyYAML==6.0.3
helm lint charts/divoom-times-frame-sync --strict -f charts/divoom-times-frame-sync/ci/test-values.yaml
.venv/bin/python scripts/check-chart.py
```

Helm CI also checks installation, upgrades, and PVC retention in its own kind cluster. `scripts/check-chart-install.sh` refuses any active context other than `kind-tfs-chart`; do not run it against an existing deployment. Keep chart LICENSE and NOTICE copies aligned with the repository originals. See [chart publishing](/docs/en/helm.md#publishing-charts) for independent chart versions and release tags.

## Licensing

Contributions are governed by the contribution terms of the project's [Apache License 2.0](/LICENSE). Preserve existing copyright and license notices, including [NOTICE](/NOTICE). Do not copy third-party code without retaining its required notices and checking license compatibility.
