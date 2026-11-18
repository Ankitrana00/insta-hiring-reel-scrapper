"""Pytest config: make the scraper modules importable from tests/.

Kept deliberately minimal — the tool is a flat folder of modules, so the
tests just need the parent directory on sys.path.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
