#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

image=${1:?Pass a container image or digest}
docker run --rm --network none "$image" --help
docker run --rm --network none --entrypoint python "$image" -m pip check
docker run --rm --network none --entrypoint sh "$image" -ec '
  ffmpeg -v error -f lavfi -i testsrc2=size=800x1280:rate=30 \
    -f lavfi -i sine=frequency=440 -t 0.2 \
    -c:v libx264 -profile:v main -level:v 4.0 -pix_fmt yuv420p \
    -c:a aac /tmp/smoke.mp4
  ffprobe -v error -show_entries stream=codec_name,width,height \
    -of json /tmp/smoke.mp4 > /tmp/probe.json
  python -c '\''import json; from PIL import features
streams = json.load(open("/tmp/probe.json"))["streams"]
assert any(s.get("codec_name") == "h264" and s.get("width") == 800 and s.get("height") == 1280 for s in streams)
assert any(s.get("codec_name") == "aac" for s in streams)
assert features.check("webp")'\''
'
