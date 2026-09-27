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
previous_pod=$(kc get pods -l app.kubernetes.io/instance=test -o jsonpath='{.items[0].metadata.uid}')
helm upgrade test "$chart" --kube-context "$context" --namespace "$namespace" \
  -f "$chart/ci/test-values.yaml" --set config.logLevel=DEBUG --wait --timeout 5m
current_pod=$(kc get pods -l app.kubernetes.io/instance=test -o jsonpath='{.items[0].metadata.uid}')
[[ "$previous_pod" != "$current_pod" ]]
kc exec deployment/tfs-test -- python -c 'from pathlib import Path; assert Path("/app/data/installation-check").read_text() == "persistent"'
helm uninstall test --kube-context "$context" --namespace "$namespace" --wait --timeout 2m
kc get pvc tfs-test
# Reuse the retained journal through the external-PVC option.
helm upgrade --install test "$chart" --kube-context "$context" --namespace "$namespace" \
  -f "$chart/ci/test-values.yaml" --set persistence.existingClaim=tfs-test --wait --timeout 5m
kc exec deployment/tfs-test -- python -c 'from pathlib import Path; assert Path("/app/data/installation-check").read_text() == "persistent"'
helm uninstall test --kube-context "$context" --namespace "$namespace" --wait --timeout 2m
kc get pvc tfs-test
