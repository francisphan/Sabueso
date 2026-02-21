"""CLI entry point for the Sabueso guest report system.

Usage:
    python src/main.py          # Start Monday/Thursday scheduler
    python src/main.py --now    # Run report immediately (for testing)
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
    args = parser.parse_args()

    if args.now:
        run_report()
    else:
        start_scheduler()


if __name__ == "__main__":
    main()
