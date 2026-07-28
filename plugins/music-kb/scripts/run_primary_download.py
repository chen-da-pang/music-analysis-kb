#!/usr/bin/env python3
"""Thin wrapper alias for run_claude_download.py (Grok primary download compatibility)."""

import subprocess
import sys
from pathlib import Path

def main() -> None:
    script = Path(__file__).parent / "run_claude_download.py"
    subprocess.run([sys.executable, str(script)] + sys.argv[1:], check=True)

if __name__ == "__main__":
    main()
