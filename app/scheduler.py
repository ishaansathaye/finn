from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    return scheduler


def parse_hour_minute(value: str) -> tuple[int, int]:
    hour_s, minute_s = value.split(":", 1)
    return int(hour_s), int(minute_s)


def configure_default_jobs(scheduler: BackgroundScheduler, premarket_cb, midday_cb, after_close_cb) -> None:
    settings = get_settings()
    pre_h, pre_m = parse_hour_minute(settings.premarket_report_time_pt)
    mid_h, mid_m = parse_hour_minute(settings.midday_report_time_pt)
    close_h, close_m = parse_hour_minute(settings.after_close_report_time_pt)

    scheduler.add_job(premarket_cb, "cron", day_of_week="mon-fri", hour=pre_h, minute=pre_m)
    scheduler.add_job(midday_cb, "cron", day_of_week="mon-fri", hour=mid_h, minute=mid_m)
    scheduler.add_job(after_close_cb, "cron", day_of_week="mon-fri", hour=close_h, minute=close_m)
