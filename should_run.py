#!/usr/bin/env python3
"""
Daylight-saving gate.

GitHub cron only understands UTC. 7:00 AM New York is 11:00 UTC in summer (EDT, UTC-4)
but 12:00 UTC in winter (EST, UTC-5). The workflow therefore schedules BOTH UTC times
for every New York time, and this script lets through only the trigger that matches
New York's current offset. The other one exits immediately (takes ~10 seconds).

Usage: python scraper/should_run.py <event_name> "<cron string>"
Reads RUN_TIMES_NY (e.g. "07:00,19:00") from the environment.
Writes run=true/false to $GITHUB_OUTPUT.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


def decide(event, cron, run_times, now=None):
    if event != "schedule":
        return True, f"'{event}' trigger (manual run) - always runs"
    parts = cron.split()
    if len(parts) < 2:
        return True, f"Could not read cron '{cron}' - running to be safe"
    minute, hour = int(parts[0]), int(parts[1])
    now = now or datetime.now(ZoneInfo("America/New_York"))
    offset_h = now.utcoffset().total_seconds() / 3600          # -4 in summer, -5 in winter
    total = (hour * 60 + minute + int(offset_h * 60)) % (24 * 60)
    ny = f"{total // 60:02d}:{total % 60:02d}"
    wanted = [t.strip() for t in run_times.split(",") if t.strip()]
    if ny in wanted:
        return True, f"cron {hour:02d}:{minute:02d} UTC = {ny} New York (UTC{offset_h:+.0f}) - running"
    return False, (f"cron {hour:02d}:{minute:02d} UTC = {ny} New York (UTC{offset_h:+.0f}), "
                   f"not one of {wanted} - skipping (this is the other daylight-saving slot)")


if __name__ == "__main__":
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    cron = sys.argv[2] if len(sys.argv) > 2 else ""
    ok, why = decide(event, cron, os.environ.get("RUN_TIMES_NY", "07:00,19:00"))
    print(why)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"run={'true' if ok else 'false'}\n")
