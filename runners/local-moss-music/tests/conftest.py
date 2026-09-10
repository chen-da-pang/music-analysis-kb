"""Test bootstrap: put this runner and the plugin package on sys.path."""

from __future__ import annotations

import sys
from pathlib import Path

RUNNER_DIR = Path(__file__).resolve().parents[1]
PLUGIN_SRC = RUNNER_DIR.parents[1] / "plugins" / "music-kb" / "src"
for path in (str(RUNNER_DIR), str(PLUGIN_SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)
