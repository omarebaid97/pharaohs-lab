"""Container entrypoint: run the pipeline daily at 04:00 (container TZ), plus once at startup
if data/public/meta.json does not exist yet.

    python -m etl.scheduler

A failed run is logged and retried at the next slot; the scheduler itself never exits.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
META = ROOT / "data" / "public" / "meta.json"
HOUR, MINUTE = 4, 0


def log(msg: str) -> None:
    print(f"[scheduler {datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}", flush=True)


def run_pipeline() -> bool:
    log("pipeline start")
    ok = True
    for mod in ("etl.run", "etl.publish"):
        r = subprocess.run([sys.executable, "-m", mod], cwd=ROOT)
        if r.returncode != 0:
            log(f"{mod} exited {r.returncode}")
            ok = False
            if mod == "etl.run":
                continue   # still publish whatever exports exist (failed models keep their previous export)
    log("pipeline done" if ok else "pipeline finished with errors")
    return ok


def next_slot(now: datetime) -> datetime:
    slot = now.replace(hour=HOUR, minute=MINUTE, second=0, microsecond=0)
    return slot if slot > now else slot + timedelta(days=1)


def main() -> None:
    if hasattr(time, "tzset"):
        time.tzset()
    log(f"TZ={os.environ.get('TZ', 'unset (UTC)')}; daily run at {HOUR:02d}:{MINUTE:02d}")
    if not META.exists():
        log("data/public/meta.json missing: running once at startup")
        try:
            run_pipeline()
        except Exception as e:   # never die
            log(f"startup run failed: {type(e).__name__}: {e}")
    while True:
        now = datetime.now().astimezone()
        target = next_slot(now)
        log(f"next run {target.isoformat(timespec='minutes')}")
        while True:   # sleep in short steps so clock changes / suspend are handled
            left = (target - datetime.now().astimezone()).total_seconds()
            if left <= 0:
                break
            time.sleep(min(left, 60))
        try:
            run_pipeline()
        except Exception as e:
            log(f"run failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
