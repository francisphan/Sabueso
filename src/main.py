"""CLI entry point for the Sabueso guest report system.

Usage:
    python src/main.py                        # Start Monday/Thursday scheduler
    python src/main.py --now                  # Run report immediately
    python src/main.py --now --to me@ex.com   # Override recipients
    python src/main.py --now --runs 5 --to me@ex.com  # 5 runs, cached SF data
"""

import argparse
import sys
from pathlib import Path

# Allow imports from src/ regardless of working directory
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv()

from scheduler import run_report, start_scheduler


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sabueso — Anticipatory Guest Report System"
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Run the report immediately instead of starting the scheduler.",
    )
    parser.add_argument(
        "--to",
        metavar="EMAIL",
        help="Override email recipients for this run (comma-separated). Ignores REPORT_SUBSCRIBERS.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        metavar="N",
        help="Run the report N times. Salesforce results are cached after the first run.",
    )
    args = parser.parse_args()

    recipients = [r.strip() for r in args.to.split(",") if r.strip()] if args.to else None

    if args.now or args.runs > 1:
        cache_dir = None
        if args.runs > 1:
            cache_dir = Path(__file__).parent.parent / ".cache"
            cache_dir.mkdir(exist_ok=True)

        n = args.runs
        for i in range(1, n + 1):
            if n > 1:
                print(f"\n{'='*60}\nRun {i} of {n}\n{'='*60}")
            run_report(
                recipients_override=recipients,
                cache_dir=cache_dir,
                run_id=i if n > 1 else None,
            )
    else:
        start_scheduler()


if __name__ == "__main__":
    main()
