from __future__ import annotations

import sys
import time
from pathlib import Path


def is_healthy(heartbeat: Path, now: float, max_age_s: float = 60.0) -> bool:
    try:
        written = float(heartbeat.read_text().strip())
    except (OSError, ValueError):
        return False
    return (now - written) <= max_age_s


if __name__ == "__main__":
    ok = is_healthy(Path("/data/heartbeat"), now=time.time())
    sys.exit(0 if ok else 1)
