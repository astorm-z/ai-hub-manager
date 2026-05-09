from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.database import SessionLocal
from app.models import Channel
from app.services.monitoring import run_channel_monitor


scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


async def monitor_all_channels() -> None:
    db = SessionLocal()
    try:
        channels = db.query(Channel).filter(Channel.enabled.is_(True)).all()
        for channel in channels:
            await run_channel_monitor(db, channel)
    finally:
        db.close()


def start_scheduler() -> None:
    if not settings.scheduler_enabled or scheduler.running:
        return
    scheduler.add_job(monitor_all_channels, "interval", minutes=1, id="monitor_all_channels", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
