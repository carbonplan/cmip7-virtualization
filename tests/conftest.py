"""Put the repo root on ``sys.path`` so tests can import ``playground.*``.

``playground/`` holds runnable demo scripts rather than an installed package,
so it is not on the path by virtue of ``uv sync``.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
