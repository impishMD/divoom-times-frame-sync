# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

"""Validate a release before publishing any assets or container tags."""

import os
from pathlib import Path
import re
import tomllib

version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
tag = os.environ["RELEASE_TAG"]
if os.environ.get("REF_TYPE") != "tag":
    raise SystemExit("Releases must run from a pushed Git tag")
if not re.fullmatch(r"v\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", tag):
    raise SystemExit("Use vX.Y.Z, or a Python prerelease such as vX.Y.Zrc1")
if tag != f"v{version}":
    raise SystemExit(f"Tag {tag!r} does not match pyproject.toml version {version!r}")
notes = Path("docs/en/releases") / f"{tag}.md"
if not notes.is_file() or not notes.read_text().strip():
    raise SystemExit(f"Missing release notes: {notes}")
print(f"Validated {tag} and {notes}")
