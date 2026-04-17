"""pytest configuration — makes the project root importable as a package root.

Without this, `from hands_free_voice import ...` only works if pytest is run
from the project root. Adding the parent directory to sys.path at collection
time means tests pass whether invoked from the repo root or from tests/.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
