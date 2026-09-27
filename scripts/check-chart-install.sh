#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

# Never run this smoke test against the user's active cluster.
context=kind-tfs-chart
[[ "$(kubectl config current-context)" == "$context" ]]
namespace=tfs-chart-test
chart=charts/divoom-times-frame-sync
kc() { kubectl --context "$context" --namespace "$namespace" "$@"; }
on_exit() {
  code=$?
  if [[ "$code" != 0 ]]; then
    kc get pods,pvc
    kc describe pods
    kc logs deployment/tfs-test --all-containers=true || true
  fi
  exit "$code"
}
trap on_exit EXIT

kubectl --context "$context" create namespace "$namespace"
# Loopback URLs cannot contact real albums or frames. The worker should stay
# running and retry this intentionally unavailable source.
kc create secret generic tfs-test --from-literal=DIVOOM_TOKEN=123456 \
  --from-literal=IMMICH_SHARE_URL=http://127.0.0.1:9/share/fixture
helm upgrade --install test "$chart" --kube-context "$context" --namespace "$namespace" \
  -f "$chart/ci/test-values.yaml" --wait --timeout 5m
kc exec deployment/tfs-test -- python -c '
import os
import subprocess
from pathlib import Path
from timesframesync.config import Config
from timesframesync.sources_config import load_sources
from timesframesync.video import transcode, make_cover
assert os.getuid() == 1000
config = Config.load()
assert config.host == "127.0.0.1"
assert load_sources(config)[0].url == "http://127.0.0.1:9/share/fixture"
assert config.token == "123456"
Path("/app/data/installation-check").write_text("persistent")
assert Path("/app/data/.lock").exists()
Path("/tmp/write-check").write_text("temporary")
source = Path("/tmp/chart-video.mp4")
target = Path("/app/data/chart-video.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:size=64x64:rate=30",
                "-t", "0.5", "-c:v", "libx264", str(source)], check=True)
assert transcode(source, target, "contain")["width"] == 800
assert make_cover(target)
source.unlink()
target.unlink()
'
kc logs deployment/tfs-test | grep -F 'Service started'
image=$(kc get deployment tfs-test -o jsonpath='{.spec.template.spec.containers[0].image}')
previous_pod=$(kc get pods -l app.kubernetes.io/instance=test -o jsonpath='{.items[0].metadata.uid}')
helm upgrade test "$chart" --kube-context "$context" --namespace "$namespace" \
  -f "$chart/ci/test-values.yaml" --set config.logLevel=DEBUG --wait --timeout 5m
current_pod=$(kc get pods -l app.kubernetes.io/instance=test -o jsonpath='{.items[0].metadata.uid}')
[[ "$previous_pod" != "$current_pod" ]]
kc exec deployment/tfs-test -- python -c 'from pathlib import Path; assert Path("/app/data/installation-check").read_text() == "persistent"'
helm uninstall test --kube-context "$context" --namespace "$namespace" --wait --timeout 2m
kc get pvc tfs-test

# Simulate a driver that leaves the PVC owned by root and imported private files.
# All previous worker Pods are gone before changing permissions on this test PVC.
kc apply -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: seed-root-owned-data
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext:
        runAsUser: 0
        runAsGroup: 0
      containers:
        - name: seed
          image: "$image"
          command: [python, -c]
          args:
            - |
              import os
              from pathlib import Path
              root = Path('/app/data')
              journal = root / 'imported-state'
              journal.mkdir()
              saved = journal / 'journal.json'
              saved.write_text('{"preserved": true}')
              os.chown(saved, 0, 0)
              saved.chmod(0o400)
              os.chown(journal, 0, 0)
              journal.chmod(0o700)
              lock = root / '.lock'
              os.chown(lock, 0, 0)
              lock.chmod(0o600)
              os.chown(root, 0, 0)
              root.chmod(0o755)
          volumeMounts:
            - name: data
              mountPath: /app/data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: tfs-test
YAML
kc wait --for=condition=complete job/seed-root-owned-data --timeout=2m
kc delete job seed-root-owned-data --wait=true
helm upgrade --install test "$chart" --kube-context "$context" --namespace "$namespace" \
  -f "$chart/ci/test-values.yaml" --set persistence.existingClaim=tfs-test \
  --set volumePermissions.enabled=true --set podSecurityContext.fsGroup=null --wait --timeout 5m
kc exec deployment/tfs-test -c sync -- python -c '
import os
from pathlib import Path
assert os.getuid() == 1000
root = Path("/app/data")
assert root.stat().st_uid == 1000
assert (root / "installation-check").read_text() == "persistent"
with (root / ".lock").open("a"):
    pass
saved = root / "imported-state/journal.json"
assert saved.read_text() == "{\"preserved\": true}"
assert saved.stat().st_uid == 1000 and saved.stat().st_gid == 1000
saved.write_text("writable")
assert (root / ".lock").exists()
'
kc logs deployment/tfs-test -c sync | grep -F 'Service started'
helm uninstall test --kube-context "$context" --namespace "$namespace" --wait --timeout 2m
kc get pvc tfs-test
# Reuse the retained journal through the external-PVC option.
helm upgrade --install test "$chart" --kube-context "$context" --namespace "$namespace" \
  -f "$chart/ci/test-values.yaml" --set persistence.existingClaim=tfs-test --wait --timeout 5m
kc exec deployment/tfs-test -- python -c 'from pathlib import Path; assert Path("/app/data/installation-check").read_text() == "persistent"'
helm uninstall test --kube-context "$context" --namespace "$namespace" --wait --timeout 2m
kc get pvc tfs-test
