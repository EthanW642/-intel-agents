"""Runs each agent's pipeline on its own daily schedule (spec section 1).
Phase 1: Middle East agent only.
"""
from __future__ import annotations

import logging

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler

from agents.middle_east.run import run as run_middle_east

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = yaml.safe_load(open("config/watchlists.yaml"))["pipeline"]

    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_middle_east,
        "cron",
        hour=cfg["run_hour"],
        minute=cfg["run_minute"],
        id="middle_east_daily",
    )
    logger.info(
        "Scheduler started. Middle East agent runs daily at %02d:%02d.",
        cfg["run_hour"],
        cfg["run_minute"],
    )
    scheduler.start()


if __name__ == "__main__":
    main()
