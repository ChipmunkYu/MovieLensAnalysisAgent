from __future__ import annotations

import argparse
import time

from backend.db import SessionLocal, init_db
from backend.service import work_once
from backend.analysis.service import work_analysis_once


def main() -> None:
    parser = argparse.ArgumentParser(description="MovieLens database worker")
    parser.add_argument("--once", action="store_true", help="process at most one task and exit")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    init_db()
    while True:
        with SessionLocal() as session:
            task = work_once(session)
            analysis_task = None if task else work_analysis_once(session)
            if task:
                print(f"{task.id} {task.status.value}" + (f": {task.error}" if task.error else ""), flush=True)
            elif analysis_task:
                print(f"{analysis_task.id} {analysis_task.status.value}" + (f": {analysis_task.error}" if analysis_task.error else ""), flush=True)
        if args.once:
            return
        if not task and not analysis_task:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
