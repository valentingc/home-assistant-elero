"""Keep the integration's version numbers consistent.

HACS shows the release tag, Home Assistant shows manifest.json; both should
say the same, and the changelog should describe the version being shipped.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import custom_components.elero as integration

ROOT = Path(__file__).parent.parent
MANIFEST = json.loads(
    (ROOT / "custom_components/elero/manifest.json").read_text(encoding="utf-8")
)


def test_manifest_matches_module_version() -> None:
    assert MANIFEST["version"] == integration.__version__


def test_changelog_starts_with_current_version() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = readme[readme.index("## Changelog") :]
    latest = re.search(r"- \*\*([\d.]+)\*\*", changelog).group(1)
    assert latest == MANIFEST["version"]
