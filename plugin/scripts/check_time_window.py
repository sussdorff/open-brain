"""check_time_window.py — Detect the current memory-heartbeat time window.

Usage:
    uv run python check_time_window.py

Output (JSON):
    {"window": "WORK-HOURS|END-OF-DAY|WEEKLY|QUIET", "hour": N, "dow": N}

Window priority (highest first):
    1. QUIET  — hour < 6 or hour >= 22 (any day)
    2. QUIET  — weekend (dow >= 6)
    3. WEEKLY — Friday (dow == 5) and 17 <= hour < 19
    4. END-OF-DAY — Mon-Thu (dow <= 4) and 17 <= hour < 19
    5. WORK-HOURS — Mon-Fri (dow <= 5) and 6 <= hour < 17
    6. QUIET  — fallthrough (weekday 7PM-10PM)
"""

# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import datetime
import json
import sys


def detect_window(hour: int, dow: int) -> str:
    """Return the time window name for the given hour and day-of-week.

    Args:
        hour: Current hour in 24h format (0-23).
        dow: Day of week per ISO: 1=Mon, 5=Fri, 6=Sat, 7=Sun.

    Returns:
        One of "QUIET", "WEEKLY", "END-OF-DAY", "WORK-HOURS".
    """
    # Rule 1: night hours (any day)
    if hour < 6 or hour >= 22:
        return "QUIET"

    # Rule 2: weekend
    if dow >= 6:
        return "QUIET"

    # Rule 3: Friday EOD → WEEKLY
    if dow == 5 and 17 <= hour < 19:
        return "WEEKLY"

    # Rule 4: Mon-Thu EOD
    if dow <= 4 and 17 <= hour < 19:
        return "END-OF-DAY"

    # Rule 5: work hours Mon-Fri
    if dow <= 5 and 6 <= hour < 17:
        return "WORK-HOURS"

    # Rule 6: fallthrough (weekday 7PM-10PM)
    return "QUIET"


def main() -> None:
    now = datetime.datetime.now()
    hour = now.hour
    dow = now.isoweekday()  # 1=Mon ... 7=Sun

    window = detect_window(hour, dow)
    result = {"window": window, "hour": hour, "dow": dow}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
