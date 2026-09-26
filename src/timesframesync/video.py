# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

"""Prepare native MP4 video and its WebP cover using local FFmpeg."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess

from .config import SyncError
from .frame import device_photo

VIDEO_REVISION = "800x1280-h264-main4-30-aac-v1"


def file_digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def require_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SyncError("Video support requires FFmpeg and ffprobe; install FFmpeg and retry")


def run_media(command: list[str], *, timeout: int = 3600) -> bytes:
    try:
        return subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout).stdout
    except FileNotFoundError:
        raise SyncError("Video support requires FFmpeg and ffprobe") from None
    except (subprocess.SubprocessError, OSError):
        # FFmpeg stderr may include paths/metadata. Do not expose source details.
        raise SyncError("Video processing failed or timed out; temporary input retained for retry") from None


def probe(path: Path) -> dict:
    try:
        data = json.loads(run_media(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                                    "-of", "json", str(path)], timeout=90))
        video = next(s for s in data["streams"] if s.get("codec_type") == "video")
        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0 or video["width"] <= 0 or video["height"] <= 0:
            raise ValueError
        return {"duration": duration, "video": video, "streams": data["streams"]}
    except (ValueError, TypeError, KeyError, StopIteration):
        raise SyncError("Unable to read a complete video stream") from None


def transcode(source: Path, destination: Path, fit: str) -> dict:
    require_ffmpeg()
    original = probe(source)
    if fit == "cover":
        scale = "scale=800:1280:force_original_aspect_ratio=increase:force_divisible_by=2,crop=800:1280"
    else:
        scale = "scale=800:1280:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=800:1280:(ow-iw)/2:(oh-ih)/2"
    # FFmpeg applies rotation/display-matrix metadata before these filters.
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
               "-map", "0:v:0", "-map", "0:a:0?", "-vf", scale + ",setsar=1",
               "-r", "30", "-c:v", "libx264", "-profile:v", "main", "-level:v", "4.0",
               "-preset", "medium", "-crf", "23", "-maxrate", "4M", "-bufsize", "8M",
               "-pix_fmt", "yuv420p", "-threads", "2", "-c:a", "aac", "-b:a", "128k",
               "-ac", "2", "-ar", "48000", "-map_metadata", "-1", "-map_chapters", "-1",
               "-metadata:s:v:0", "rotate=0", "-movflags", "+faststart", str(destination)]
    run_media(command)
    prepared = probe(destination)
    stream = prepared["video"]
    if (stream["codec_name"] != "h264" or stream["width"] != 800 or stream["height"] != 1280
            or stream.get("pix_fmt") != "yuv420p"
            or abs(prepared["duration"] - original["duration"]) > max(0.5, original["duration"] * 0.01)):
        raise SyncError("Prepared video is incomplete or has an unsupported format")
    return {"duration": prepared["duration"], "width": 800, "height": 1280}


def make_cover(video: Path) -> bytes:
    # Use FFmpeg's built-in PNG encoder; WebP support need not be compiled into it.
    png = run_media(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-frames:v", "1",
                     "-f", "image2pipe", "-c:v", "png", "pipe:1"], timeout=90)
    return device_photo(png)[1]
