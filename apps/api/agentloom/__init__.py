"""AgentLoom management API package."""

import sys
from pathlib import Path

_RUNTIME = str(Path(__file__).resolve().parents[3] / "packages/runtime")
if _RUNTIME not in sys.path:
    sys.path.insert(0, _RUNTIME)
