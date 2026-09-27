# Kubernetes, Helm, and Argo CD

**English** | [Русский](https://github.com/impishMD/divoom-times-frame-sync/blob/main/docs/ru/helm.md)

The chart deploys one Divoom Times Frame Sync worker, a ConfigMap describing its sources, and a persistent volume claim. It uses the public Docker Hub image for AMD64 and ARM64. Chart `0.1.1` defaults to application `0.7.1`.

The Pod must reach the frame's IP on TCP port `9000` (or your configured port), DNS, and the source services over HTTP/HTTPS. Create ordinary destination albums in the Divoom app first. Stop any other sync service managing the same frame before starting this deployment. Each frame needs its own release and data volume.

## Prepare credentials and values

Create a Secret in the same namespace as the application using your preferred secret manager, External Secrets, Sealed Secrets, or `kubectl`. Its keys become environment variables. It must contain `DIVOOM_TOKEN` and every URL/password variable referenced by the source list. Keep actual values out of public Git and Helm values.

For example, this is the Secret's structure; replace these synthetic values privately:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: tfs-credentials
  namespace: divoom-sync
type: Opaque
stringData:
  DIVOOM_TOKEN: "123456"
  IMMICH_SHARE_URL: "https://immich.example.com/share/your-key"
  IMMICH_SHARE_PASSWORD: "your-album-password"
  GOOGLE_PHOTOS_SHARE_URL: "https://photos.app.goo.gl/your-album"
```

An example `values.yaml`:

```yaml
existingSecret: tfs-credentials
config:
  frame:
    host: "192.168.1.100"
    album: Photos
  syncInterval: 300
  syncMode: mirror
  logLevel: INFO
sources:
  - id: immich-family
    provider: immich
    urlEnv: IMMICH_SHARE_URL
    passwordEnv: IMMICH_SHARE_PASSWORD
    targetAlbum: Photos
  - id: google-family
    provider: google_photos
    urlEnv: GOOGLE_PHOTOS_SHARE_URL
    targetAlbum: Photos
persistence:
  size: 10Gi
  # storageClass: your-storage-class
```

Supported providers are `immich`, `google_photos`, `icloud`, `onedrive`, and `yandex_disk`. Repeat a provider for additional albums. IDs must remain unique and stable; the same target combines sources. Omit `passwordEnv` for password-free albums and `targetAlbum` to use `config.frame.album`. The chart deliberately accepts environment-variable references instead of literal URLs or passwords.

## Install with Helm

Requires Helm 3+ and Kubernetes 1.26+.

```sh
helm repo add divoom https://impishmd.github.io/divoom-times-frame-sync/
helm repo update
helm upgrade --install divoom-sync divoom/divoom-times-frame-sync \
  --version 0.1.1 --namespace divoom-sync --create-namespace \
  --values values.yaml
kubectl -n divoom-sync logs -f deployment/divoom-sync-divoom-times-frame-sync
```

The Secret must already exist before the Pod can start. There is no Service or Ingress: the worker only makes outbound requests. `hostNetwork` is not required when Pod networking can route to the frame.

## Argo CD

Add this Application to your own GitOps repository, adapting the namespace, IP, Secret name, source list, and project. `targetRevision` is the **chart version**, not the application version.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: divoom-sync
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://impishmd.github.io/divoom-times-frame-sync/
    chart: divoom-times-frame-sync
    targetRevision: 0.1.1
    helm:
      valuesObject:
        existingSecret: tfs-credentials
        config:
          frame:
            host: "192.168.1.100"
            album: Photos
        sources:
          - id: immich-family
            provider: immich
            urlEnv: IMMICH_SHARE_URL
            passwordEnv: IMMICH_SHARE_PASSWORD
  destination:
    server: https://kubernetes.default.svc
    namespace: divoom-sync
  syncPolicy:
    syncOptions:
      - CreateNamespace=true
```

This example leaves synchronization manual. You can set an automatic sync policy yourself. The Secret is managed separately; the chart never generates or rotates it. Values may also come from a separate Git source using Argo CD's multiple-source support. See the [Argo CD Helm documentation](https://argo-cd.readthedocs.io/en/latest/user-guide/helm/).

## Storage and updates

Persistence is mandatory. The default 10 GiB PVC holds the journal and temporary media, not a permanent copy of every source album. Size it for the largest source video plus its converted output and cover. Set `persistence.existingClaim` to reuse a PVC; the chart will not create or manage that claim. `storageClass: null` uses the cluster default, `storageClass: ""` disables dynamic class selection, and a name selects that class. Changing an existing claim's class/access modes generally requires a new claim; resizing depends on the storage driver.

The chart runs as UID/GID 1000 with `fsGroup: 1000`. The storage driver must support those permissions and file locking; existing files must be writable by the configured user. Container root storage is read-only, with a separate writable `/tmp`. CPU requests default to 100m and memory requests to 256 MiB, with a 1 GiB memory limit. Adjust resources for large photos and video conversion.

`replicaCount` is fixed at one. Deployment updates use `Recreate` so the old Pod is terminated before its replacement during a normal rollout. Do not run separate releases against the same data or frame. The default termination grace period is 300 seconds; unfinished work is retried from the journal after a forced stop.

Source/config changes trigger a rollout. After changing values inside the external Secret, restart the Deployment or use your cluster's secret-reload controller. Pod readiness means the process is running, not that a sync cycle has succeeded; source/frame outages are retried and reported in application logs. No network liveness probe is used.

`persistence.retain: true` preserves a chart-created PVC on Helm uninstall and Argo CD prune/application deletion. Delete it manually only when its journal is no longer needed. A retained PVC can be reused through `persistence.existingClaim`; deleting the entire namespace also deletes the PVC. To opt into ordinary chart-managed deletion, set `retain: false` and apply that change before uninstalling.

The image tag defaults to `v<Chart.appVersion>`; `image.tag` overrides it and `image.digest` takes precedence over tags. `imagePullSecrets` supports private registries. To update, select a new chart version, review its defaults, and sync/upgrade. See [chart releases](https://github.com/impishMD/divoom-times-frame-sync/blob/main/docs/en/chart-releases/v0.1.1.md).

## Publishing charts

Chart sources live in `charts/divoom-times-frame-sync/`. `Helm CI` lints/renders with Helm 3 and 4, validates configuration with the application parser, and installs/upgrades/uninstalls in an isolated kind cluster. Its fixtures use loopback URLs and never contact a real frame or album.

For a new release, update `Chart.yaml`'s `version`, update `appVersion` when changing the default application, and add EN/RU notes at `docs/<language>/chart-releases/v<VERSION>.md`. Wait for main's Helm CI, then push an annotated `chart-v<VERSION>` tag. `Helm Release` checks the chart, attaches its `.tgz` to a GitHub release, updates `gh-pages/index.yaml` with chart-releaser, and deploys Pages using Actions. Existing chart releases are skipped on retry and published versions must not be overwritten. The application's `v…` releases and Docker tags are independent; chart releases never replace the latest application release.

The repository's Pages build source must be **GitHub Actions**, with a `gh-pages` branch containing `index.html` and the generated index. The `github-pages` environment must allow `chart-v*` tags and `main` for recovery runs. To resume publication after a workflow fix, manually run **Helm Release** from `main` with the existing chart tag; it checks that tag's sources, preserves the published archive, and finishes its index/Pages deployment. The workflow uses `GITHUB_TOKEN`; no registry secret is needed. Chart-only changes run Helm checks and skip the Python/container packaging workflows. [Helm chart repositories](https://helm.sh/docs/topics/chart_repository/) describe the public `index.yaml` format.
