# Divoom Times Frame Sync Helm chart

Deploy one sync worker with persistent storage and credentials from an existing Kubernetes Secret.

- [English installation and Argo CD guide](https://github.com/impishMD/divoom-times-frame-sync/blob/main/docs/en/helm.md)
- [Установка и Argo CD на русском](https://github.com/impishMD/divoom-times-frame-sync/blob/main/docs/ru/helm.md)
- Chart repository: <https://impishmd.github.io/divoom-times-frame-sync/>

Set `config.frame.host` and `existingSecret`, then configure `sources` with URL/password environment variable names. The default source is Immich with `IMMICH_SHARE_URL`. The Secret must exist in the release namespace and contain the referenced keys and `DIVOOM_TOKEN`.

See `values.yaml` for persistence, resource, image, and scheduling options. Chart and application versions are independent. The default image tag is `v<appVersion>` from `Chart.yaml`.

Licensed under Apache-2.0; see LICENSE and NOTICE.
