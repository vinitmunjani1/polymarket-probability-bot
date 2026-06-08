from __future__ import annotations

import math
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def window_start(ts: float, seconds: int) -> int:
    return int(ts // seconds) * seconds
