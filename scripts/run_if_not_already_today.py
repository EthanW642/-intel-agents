"""Entry point for the boot/login LaunchAgent trigger
(scripts/com.intel-agents.middle-east-daily.plist).

Your Mac being fully shut down overnight means there's no reliable way to
fire the pipeline at a fixed clock time (see README) — a fully powered-off
machine can't be woken by software on a schedule. Instead, this runs once
per login/boot, checks whether today's run already happened (by date, in
data/run_log.csv), and skips if so — so logging in more than once in a day
doesn't produce duplicate runs or duplicate emails.
"""
from __future__ import annotations

import csv
import logging
from datetime import date

from agents.middle_east.run import run
from pipeline.run_log import RUN_LOG_PATH

logger = logging.getLogger(__name__)


def already_ran_today(run_date: date | None = None) -> bool:
    run_date = run_date or date.today()
    if not RUN_LOG_PATH.exists():
        return False
    with open(RUN_LOG_PATH, newline="") as f:
        return any(row.get("run_date") == run_date.isoformat() for row in csv.DictReader(f))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if already_ran_today():
        logger.info("Already ran today (%s) — skipping.", date.today().isoformat())
        return
    run()


if __name__ == "__main__":
    main()
