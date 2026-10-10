"""APScheduler wiring. The systemd kick timer calls /api/jobs/catchup/run so runs missed during sleep fire."""
import time
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db, jobs

sched = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 6 * 3600})
META = {
    "sync_schedule": ("Sync schedule", "Daily 06:00"),
    "poll_releases": ("Poll releases", None),
    "watch_downloads": ("Watch downloads", "Every 1 min"),
    "retry_sync": ("AniList retry", "Every 15 min"),
    "purge": ("Purge", "Daily 04:00"),
}


def _now():
    return datetime.now().astimezone()


def start() -> None:
    sched.add_job(jobs.sync_schedule, CronTrigger(hour=6), id="sync_schedule")
    sched.add_job(jobs.poll_releases, IntervalTrigger(minutes=int(db.setting("poll_minutes"))), id="poll_releases")
    sched.add_job(jobs.watch_downloads, IntervalTrigger(minutes=1), id="watch_downloads")
    sched.add_job(jobs.retry_sync, IntervalTrigger(minutes=15), id="retry_sync")
    sched.add_job(jobs.purge, CronTrigger(hour=4), id="purge")
    sched.start()
    catchup()


def stop() -> None:
    sched.shutdown(wait=False)


def run_now(name: str) -> None:
    if name == "catchup":
        return catchup()
    j = sched.get_job(name)
    if not j:
        raise KeyError(name)
    j.modify(next_run_time=_now())


def catchup() -> None:
    """Fire anything overdue (after suspend or a restart), then poll."""
    for name in ("sync_schedule", "purge"):
        if time.time() - jobs.last_run(name) > 23 * 3600:
            run_now(name)
    run_now("poll_releases")


def set_poll_minutes(m: int) -> None:
    sched.reschedule_job("poll_releases", trigger=IntervalTrigger(minutes=max(5, int(m))))


def describe() -> list[dict]:
    out = []
    for name, (label, cadence) in META.items():
        j = sched.get_job(name)
        nxt = j.next_run_time.timestamp() if j and j.next_run_time else None
        cadence = cadence or f"Every {db.setting('poll_minutes')} min"
        extra = f"next {jobs.stamp(nxt)}" if name == "poll_releases" else f"ran {jobs.stamp(jobs.last_run(name))}"
        if name == "retry_sync":
            extra = f"{db.one('SELECT COUNT(*) n FROM episode WHERE state=? AND anilist_synced=0', 'done')['n']} queued"
        if name == "watch_downloads":
            extra = f"{db.one('SELECT COUNT(*) n FROM episode WHERE state IN (?,?)', 'queued', 'downloading')['n']} active"
        end, err = jobs.FINISHED.get(name, (None, None))
        out.append({"name": name, "label": label, "when": f"{cadence} · {extra}", "next": nxt * 1000 if nxt else None,
                    "running": name in jobs.RUNNING, "finished": end * 1000 if end else None, "error": err})
    return out
