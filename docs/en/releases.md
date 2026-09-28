# Releases and container publishing

**English** | [Русский](../ru/releases.md)

## Workflows

- Helm charts have their own **Helm CI** and **Helm Release** workflows, `chart-v…` tags, and [publishing guide](helm.md#publishing-charts). Chart-only changes skip the application CI and Packages workflows.
- **CI** runs on pushes to `main`, pull requests, manual dispatch, and as part of a release. It runs Python tests on 3.11 and 3.13 and builds containers on native AMD64 and ARM64 runners. Container checks cover the CLI, Python dependencies, WebP support, and a short H.264/AAC conversion without network access.
- **Packages** runs on the same events and builds a Python wheel, an sdist, and a complete source archive from the Git commit. It verifies installation from all three archives and uploads them with `checksums.txt` as an Actions artifact.
- **Release** runs when a tag matching `v[0-9]*` is pushed. It calls both workflows, creates a draft GitHub release, builds and checks images in both registries on each native architecture, then publishes multi-platform tags and the release. A failed run leaves the release as a draft; the `latest` tags are updated only after both version manifests have been verified.

CI and Packages skip push and pull-request events when all changed files are under `docs/` or are Markdown files in the repository root. Changes to source code, dependencies, build files, or workflows still trigger both workflows, including commits that also update documentation. Manual runs and checks invoked by Release always run.

Published containers support `linux/amd64` and `linux/arm64`:

```text
docker.io/impishmd/divoom-times-frame-sync:v0.7.2
ghcr.io/impishmd/divoom-times-frame-sync:v0.7.2
```

Stable versions also update `latest` in both registries. Prereleases do not update `latest`. The service version remains in `pyproject.toml`; container tags add the `v` prefix. There are no mutable major/minor aliases. Releases are serialized to avoid concurrent updates to `latest`; do not publish an older stable version after a newer one unless you intend to roll back `latest`.

## Repository configuration

1. Enable GitHub Actions and allow the actions referenced by the workflows.
2. Create the **`docker_hub`** GitHub environment with these **secrets**:
   - `DOCKERHUB_USR`: Docker Hub login with push access to `impishmd/divoom-times-frame-sync`.
   - `DOCKERHUB_TOKEN`: Docker Hub access token with read/write permissions for that repository.
3. If you restrict environment deployment branches/tags, allow release tags. Environment approvals, if configured, apply to image publishing jobs.
4. GHCR uses the workflow's `GITHUB_TOKEN` with `packages: write`; no additional token is needed. If the package already exists, grant this repository Actions access to it. The image includes an OCI source label linking it to this repository.

Repository and package visibility are separate settings. A new GHCR package is private by default; change its package visibility to public if anonymous pulls are intended. Configure Docker Hub repository visibility independently. The workflow does not change repository or package visibility.

## Publish a version

1. Update `project.version` in `pyproject.toml`.
2. Add English release notes at `docs/en/releases/v<VERSION>.md` and their Russian translation at `docs/ru/releases/v<VERSION>.md`. Update version examples in both READMEs as needed. GitHub Releases use the English notes. Use absolute links to repository files in release notes so they also work on the GitHub release page.
3. Commit and push to `main`, then wait for CI and Packages to pass.
4. Create and push an annotated tag matching the package version:

   ```sh
   git tag -a v0.7.2 -m 'v0.7.2'
   git push origin v0.7.2
   ```

Use Python-compatible versions: `0.7.2` for a stable release, or `0.7.0rc1`, `0.7.0a1`, `0.7.0b1` for prereleases. The corresponding tags are `v0.7.2`, `v0.7.0rc1`, etc. Invalid tags, mismatched versions, and missing or empty release notes stop the pipeline before publication.

Published releases are not overwritten. Fix a transient registry failure by rerunning failed jobs in the same Actions run. If source or workflow changes are needed after a release is published, create a new version instead of moving its tag. During a registry outage, one version tag or `latest` alias can be updated before the other; rerun the failed publish job to finish both updates. Publishing across two registries is not atomic.

## Release assets and verification

Each GitHub release includes:

- `divoom-times-frame-sync-v<VERSION>-source.tar.gz`: complete tracked source tree, including workflows and documentation, without Git history.
- `timesframesync-<VERSION>.tar.gz`: Python source distribution.
- `timesframesync-<VERSION>-py3-none-any.whl`: Python wheel.
- `checksums.txt`: SHA-256 checksums for the three archives.
- `container-digests.txt`: immutable manifest references for both registries.

GitHub also provides its automatic source ZIP and tar.gz links. The wheel and sdist keep the Python distribution name `timesframesync`; the CLI is `tfs`.

```sh
sha256sum -c checksums.txt
docker buildx imagetools inspect impishmd/divoom-times-frame-sync:v0.7.2
docker buildx imagetools inspect ghcr.io/impishmd/divoom-times-frame-sync:v0.7.2
```

On macOS, use `shasum -a 256 -c checksums.txt`. Authenticate to a registry before inspecting or pulling a private image. The Actions summary and `container-digests.txt` identify the exact image manifests; use an `image@sha256:...` reference to pin a deployment immutably.

## Release notes

- [v0.7.2](releases/v0.7.2.md)
- [v0.7.1](releases/v0.7.1.md)
- [v0.7.0](releases/v0.7.0.md)
- [v0.6.0](releases/v0.6.0.md)
- [v0.5.0](releases/v0.5.0.md)
- [v0.4.0](releases/v0.4.0.md)
