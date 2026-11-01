"""Application version and release-note loading."""

from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSION_PATH = PROJECT_ROOT / "VERSION"
CHANGELOG_PATH = PROJECT_ROOT / "CHANGELOG.md"
ENTRY_PATTERN = re.compile(
    r"^##\s+(?P<version>[^·]+?)\s+·\s+(?P<title>[^·]+?)\s+·\s+(?P<date>\S+)\s*$"
)


def read_version_info() -> dict:
    """Read the version and bullet-point release notes from project files."""

    version = VERSION_PATH.read_text(encoding="utf-8").strip()
    entries: list[dict] = []
    current: dict | None = None
    for raw_line in CHANGELOG_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        match = ENTRY_PATTERN.match(line)
        if match:
            current = {
                "version": match.group("version").strip(),
                "title": match.group("title").strip(),
                "date": match.group("date").strip(),
                "changes": [],
            }
            entries.append(current)
            continue
        if current is not None and line.startswith("- "):
            current["changes"].append(line[2:].strip())
    return {"version": version, "entries": entries}
