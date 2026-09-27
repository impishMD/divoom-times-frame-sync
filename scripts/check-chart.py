# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

"""Validate rendered Kubernetes objects and their contract with the application."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib
from unittest.mock import patch

import yaml

from timesframesync.config import Config
from timesframesync.sources_config import load_sources

CHART = Path("charts/divoom-times-frame-sync")


class UniqueKeysLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueKeysLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def render(values, *, valid=True):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "values.json"
        path.write_text(json.dumps(values))
        result = subprocess.run(["helm", "template", "test", str(CHART), "-f", str(path)],
                                capture_output=True, text=True)
    if not valid:
        assert result.returncode != 0, "Invalid values were accepted"
        return
    assert result.returncode == 0, result.stderr
    objects = list(yaml.load_all(result.stdout, Loader=UniqueKeysLoader))
    return {item["kind"]: item for item in objects if item}


def load_rendered_config(objects):
    container = objects["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert all(isinstance(value, str) for value in env.values()), "Container env must contain strings"
    with tempfile.TemporaryDirectory() as directory:
        source_path = Path(directory) / "sources.toml"
        source_path.write_text(objects["ConfigMap"]["data"]["sources.toml"])
        env["SOURCES_FILE"] = str(source_path)
        # Do not inherit the developer's credentials or read their local .env.
        with patch.dict(os.environ, env, clear=True):
            return Config.load(env_file=str(Path(directory) / ".env"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-tag")
    args = parser.parse_args()
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    if args.release_tag:
        assert re.fullmatch(r"chart-v\d+\.\d+\.\d+", args.release_tag), "Expected chart-vX.Y.Z"
        assert args.release_tag == "chart-v" + chart["version"], "Tag and chart version differ"
        for language in ("en", "ru"):
            assert (Path("docs") / language / "chart-releases" / f"v{chart['version']}.md").read_text().strip()

    values = yaml.safe_load((CHART / "ci/test-values.yaml").read_text())
    objects = render(values)
    load_rendered_config(objects)
    assert set(objects) == {"Deployment", "ConfigMap", "PersistentVolumeClaim"}
    deployment = objects["Deployment"]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert not pod["automountServiceAccountToken"]
    assert pod["securityContext"]["runAsNonRoot"]
    assert "initContainers" not in pod
    assert container["securityContext"]["readOnlyRootFilesystem"]
    assert container["image"].endswith(":v" + chart["appVersion"])
    assert container["envFrom"] == [{"secretRef": {"name": "tfs-test"}}]
    claim = objects["PersistentVolumeClaim"]
    assert "storageClassName" not in claim["spec"]
    assert claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    assert "Prune=false" in claim["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]

    permissions = copy.deepcopy(values)
    permissions["volumePermissions"] = {"enabled": True}
    permissions["podSecurityContext"] = {"fsGroup": None}
    permissions["persistence"] = {"existingClaim": "imported-journal"}
    rendered = render(permissions)
    permission_pod = rendered["Deployment"]["spec"]["template"]["spec"]
    init = permission_pod["initContainers"][0]
    assert len(permission_pod["initContainers"]) == 1
    assert init["image"] == permission_pod["containers"][0]["image"]
    assert init["command"] == ["/bin/sh", "-ec"]
    assert init["args"] == ["chown -R --no-dereference 1000:1000 /app/data && chmod -R u+rwX /app/data"]
    assert init["volumeMounts"] == [{"name": "data", "mountPath": "/app/data"}]
    assert "env" not in init and "envFrom" not in init
    assert init["securityContext"]["runAsUser"] == 0
    assert not init["securityContext"]["runAsNonRoot"]
    assert not init["securityContext"]["allowPrivilegeEscalation"]
    assert init["securityContext"]["readOnlyRootFilesystem"]
    assert init["securityContext"]["capabilities"] == {
        "drop": ["ALL"], "add": ["CHOWN", "FOWNER", "DAC_OVERRIDE"]}
    assert permission_pod["securityContext"]["runAsUser"] == 1000
    assert permission_pod["containers"][0]["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert "PersistentVolumeClaim" not in rendered
    for overrides in ({"podSecurityContext": {"runAsUser": 2000, "runAsGroup": 3000}},
                      {"securityContext": {"runAsUser": 2000, "runAsGroup": 3000}}):
        custom = render({**permissions, **overrides})
        init = custom["Deployment"]["spec"]["template"]["spec"]["initContainers"][0]
        assert " 2000:3000 " in init["args"][0]
    for overrides in ({"securityContext": {"runAsUser": 0}},
                      {"podSecurityContext": {"runAsGroup": None}},
                      {"securityContext": {"runAsUser": "1000"}}):
        render({**permissions, **overrides}, valid=False)

    # Large YAML numbers must reach Python's int() as decimal strings, not
    # scientific notation. Cover a typical user ID, bounds, and a long interval.
    for port, user_id, interval in ((9000, 123456789, 30),
                                   (65535, 2147483647, 1000000), (1, 0, 10)):
        numeric = copy.deepcopy(values)
        numeric["config"]["frame"].update(port=port, userId=user_id)
        numeric["config"]["syncInterval"] = interval
        rendered = render(numeric)
        config = load_rendered_config(rendered)
        assert (config.port, config.user_id, config.sync_interval) == (port, user_id, interval)
        env = {item["name"]: item["value"] for item in
               rendered["Deployment"]["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["DIVOOM_PORT"] == str(port)
        assert env["DIVOOM_USER_ID"] == str(user_id)
        assert env["SYNC_INTERVAL"] == str(interval)

    # Exercise escaping and repeated providers using the real source parser.
    examples = {
        "immich": "https://immich.example.com/share/example",
        "google_photos": "https://photos.app.goo.gl/example",
        "icloud": "https://photos.icloud.com/shared/album/example",
        "onedrive": "https://1drv.ms/a/example",
        "yandex_disk": "https://disk.yandex.ru/a/example",
    }
    multiple = copy.deepcopy(values)
    multiple["sources"] = [dict(id=f"source-{i}", provider=provider, urlEnv=f"SOURCE_{i}",
                                targetAlbum='Family "Фото" \\ memories')
                           for i, provider in enumerate([*examples, "immich"])]
    multiple["sources"][0]["passwordEnv"] = "SOURCE_PASSWORD"
    rendered = render(multiple)
    assert rendered["Deployment"]["spec"]["template"]["metadata"]["annotations"]["checksum/config"] != deployment["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sources.toml"
        path.write_text(rendered["ConfigMap"]["data"]["sources.toml"])
        assert len(tomllib.loads(path.read_text())["sources"]) == 6
        env = {item["urlEnv"]: examples[item["provider"]] for item in multiple["sources"]}
        env["SOURCE_PASSWORD"] = "fixture-only"
        with patch.dict(os.environ, env):
            sources = load_sources(Config(sources_file=path))
        assert len(sources) == 6 and sources[0].password == "fixture-only"
        assert all(s.target_album == 'Family "Фото" \\ memories' for s in sources)

    existing = copy.deepcopy(values)
    existing["persistence"] = {"existingClaim": "saved-journal"}
    existing["image"] = {"digest": "sha256:" + "a" * 64}
    existing["config"]["logLevel"] = "DEBUG"
    objects = render(existing)
    assert "PersistentVolumeClaim" not in objects
    pod = objects["Deployment"]["spec"]["template"]["spec"]
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "saved-journal"
    assert "@sha256:" in pod["containers"][0]["image"]
    assert pod["containers"][0]["args"] == ["--log-level", "DEBUG", "run"]
    for storage_class in ("", "fast-storage"):
        custom = copy.deepcopy(values)
        custom["persistence"] = {"storageClass": storage_class, "retain": False}
        claim = render(custom)["PersistentVolumeClaim"]
        assert claim["spec"]["storageClassName"] == storage_class
        assert "annotations" not in claim["metadata"]

    # Reject unsafe concurrency, plaintext credentials, and malformed configuration.
    render({}, valid=False)
    for overrides in ({"replicaCount": 2}, {"sources": []}, {"existingSecret": ""},
                      {"config": {"syncInterval": 0}}, {"config": {"syncMode": "unknown"}},
                      {"sources": [{"id": "a", "provider": "other", "urlEnv": "URL"}]},
                      {"sources": [{"id": "a", "provider": "immich", "url": "secret"}]},
                      {"sources": [{"id": "A", "provider": "immich", "urlEnv": "URL"},
                                   {"id": "a", "provider": "immich", "urlEnv": "URL"}]}):
        render({**values, **overrides}, valid=False)
    print("Chart rendering, source configuration, persistence, and validation checks passed")


if __name__ == "__main__":
    main()
