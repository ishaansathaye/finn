from __future__ import annotations

import asyncio
import signal

from app.scheduler import build_scheduler, configure_default_jobs
from app.services.report_runner import ReportRunner


runner = ReportRunner()
scheduler = build_scheduler()


def make_job(command_text: str):
    def _job() -> None:
        asyncio.run(runner.send_batch(command_text))

    return _job


def main() -> None:
    configure_default_jobs(
        scheduler,
        premarket_cb=make_job("report"),
        midday_cb=make_job("report"),
        after_close_cb=make_job("report"),
    )
    scheduler.start()
    print("Finn scheduler started.")
    signal.pause()


if __name__ == "__main__":
    main()
