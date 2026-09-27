# Kubernetes, Helm и Argo CD

[English](https://github.com/impishMD/divoom-times-frame-sync/blob/main/docs/en/helm.md) | **Русский**

Чарт создаёт один процесс Divoom Times Frame Sync, ConfigMap с описанием источников и постоянный том. Используется публичный образ Docker Hub для AMD64 и ARM64. Чарт `0.1.0` по умолчанию запускает приложение `0.7.1`.

Из Pod должны быть доступны IP рамки по TCP/9000 (или настроенному порту), DNS и фотосервисы по HTTP/HTTPS. Заранее создайте обычные целевые альбомы в приложении Divoom. Перед запуском остановите другие экземпляры синхронизатора, работающие с этой рамкой. Для каждой рамки нужны отдельный релиз и том данных.

## Секреты и настройки

Создайте Secret в том же namespace, что и приложение: через свой менеджер секретов, External Secrets, Sealed Secrets или `kubectl`. Его ключи станут переменными окружения. Нужны `DIVOOM_TOKEN` и все переменные ссылок/паролей из списка источников. Не сохраняйте реальные значения в публичном Git или Helm values.

Пример структуры Secret с вымышленными данными; реальные значения подставьте приватно:

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

Пример `values.yaml`:

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

Поддерживаются `immich`, `google_photos`, `icloud`, `onedrive` и `yandex_disk`. Для дополнительных альбомов повторяйте провайдера. ID должны быть уникальными и постоянными; одинаковая цель объединяет источники. Уберите `passwordEnv` для альбома без пароля, а `targetAlbum` — чтобы использовать `config.frame.album`. Чарт принимает имена переменных окружения вместо самих ссылок и паролей.

## Установка через Helm

Нужны Helm 3+ и Kubernetes 1.26+.

```sh
helm repo add divoom https://impishmd.github.io/divoom-times-frame-sync/
helm repo update
helm upgrade --install divoom-sync divoom/divoom-times-frame-sync \
  --version 0.1.0 --namespace divoom-sync --create-namespace \
  --values values.yaml
kubectl -n divoom-sync logs -f deployment/divoom-sync-divoom-times-frame-sync
```

Secret должен существовать до запуска Pod. Service и Ingress не создаются: сервис выполняет только исходящие запросы. `hostNetwork` не нужен, если сеть Pod маршрутизируется до рамки.

## Argo CD

Добавьте Application в свой GitOps-репозиторий, указав нужные namespace, IP, имя Secret, источники и project. `targetRevision` — **версия чарта**, а не приложения.

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
    targetRevision: 0.1.0
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

В примере синхронизация запускается вручную; автоматическую политику можно настроить самостоятельно. Secret управляется отдельно: чарт его не создаёт и не обновляет. Values можно брать из отдельного Git-источника через multiple sources. Подробнее — в [документации Argo CD](https://argo-cd.readthedocs.io/en/latest/user-guide/helm/).

## Хранилище и обновления

Постоянное хранилище обязательно. PVC на 10 GiB по умолчанию хранит журнал и временные медиа, а не копию всех альбомов. Объём выбирайте под самое большое исходное видео вместе с преобразованным файлом и обложкой. `persistence.existingClaim` подключает готовый PVC: чарт его не создаёт и не управляет им. `storageClass: null` выбирает класс по умолчанию в кластере, `storageClass: ""` отключает выбор динамического класса, имя указывает конкретный класс. Смена класса/режимов доступа существующего PVC обычно требует нового тома; расширение зависит от драйвера.

Процесс работает с UID/GID 1000 и `fsGroup: 1000`. Драйвер хранилища должен поддерживать эти права и блокировку файлов; существующие файлы должны быть доступны для записи выбранному пользователю. Корневая файловая система контейнера доступна только для чтения, `/tmp` вынесен в отдельный записываемый том. По умолчанию запрашиваются 100m CPU и 256 MiB памяти, лимит памяти — 1 GiB. Для больших фотографий и конвертации видео ресурсы можно увеличить.

`replicaCount` зафиксирован на единице. Обновление Deployment использует `Recreate`: при обычном rollout старый Pod завершается до запуска нового. Не запускайте отдельные релизы с общими данными или одной рамкой. По умолчанию на завершение отводится 300 секунд; после принудительной остановки незавершённые операции повторяются с учётом журнала.

Изменение источников/настроек вызывает rollout. После замены значений во внешнем Secret перезапустите Deployment или используйте принятый в кластере контроллер перезагрузки секретов. Готовность Pod означает запуск процесса, а не успешную синхронизацию; недоступность источников/рамки отражается в логах и повторяется следующим циклом. Сетевая liveness-проба не используется.

`persistence.retain: true` сохраняет созданный чартом PVC при Helm uninstall, Argo CD prune и удалении Application. Удаляйте его вручную, когда журнал больше не нужен. Сохранённый том можно подключить через `persistence.existingClaim`; удаление всего namespace удаляет и PVC. Чтобы разрешить обычное удаление вместе с чартом, установите `retain: false` и примените настройку до uninstall.

Тег образа по умолчанию — `v<Chart.appVersion>`. Его переопределяет `image.tag`, а `image.digest` имеет приоритет над тегами. Для приватных реестров предусмотрен `imagePullSecrets`. Для обновления выберите новую версию чарта, проверьте её настройки и выполните sync/upgrade. [Заметки о выпуске чарта](https://github.com/impishMD/divoom-times-frame-sync/blob/main/docs/ru/chart-releases/v0.1.0.md).

## Публикация чартов

Исходники находятся в `charts/divoom-times-frame-sync/`. `Helm CI` проверяет lint и рендеринг в Helm 3 и 4, совместимость настроек с парсером приложения, установку, обновление и удаление во временном кластере kind. Используются адреса loopback без обращений к реальной рамке и альбомам.

Для релиза обновите `version` в `Chart.yaml`, при смене приложения по умолчанию — `appVersion`, и добавьте заметки EN/RU в `docs/<язык>/chart-releases/v<VERSION>.md`. После успешного Helm CI в main отправьте аннотированный тег `chart-v<VERSION>`. `Helm Release` проверяет чарт, прикладывает `.tgz` к GitHub Release, обновляет `gh-pages/index.yaml` через chart-releaser и публикует Pages через Actions. При повторном запуске существующие релизы пропускаются; опубликованные версии не перезаписываются. Релизы приложения `v…` и Docker-теги независимы; выпуск чарта не заменяет последний релиз приложения.

В настройках Pages источником сборки должен быть **GitHub Actions**, а в ветке `gh-pages` — `index.html` и сгенерированный индекс. Environment `github-pages` должен разрешать теги чарта, используемые workflow публикации. Используется `GITHUB_TOKEN`, секреты реестров не нужны. Изменения только чарта запускают Helm-проверки и пропускают сборку Python/контейнеров. Формат публичного `index.yaml` описан в [документации Helm](https://helm.sh/docs/topics/chart_repository/).
