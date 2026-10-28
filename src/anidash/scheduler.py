"""APScheduler wiring, plus a resume watchdog that replaces v1's systemd kick timer:
when the wall clock jumps (suspend/hibernate), overdue jobs run once."""
import logging
import threading
import time
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db, jobs

log = logging.getLogger("anidash")
sched = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 6 * 3600})
META = {
    "sync_schedule": ("Sync schedule", "Daily 06:00"),
    "poll_releases": ("Poll releases", None),
    "watch_downloads": ("Watch downloads", "Every 1 min"),
    "retry_sync": ("AniList retry", "Every 15 min"),
    "purge": ("Purge", "Daily 04:00"),
    "check_updates": ("Check for updates", "Monthly (daily tick)"),
}
_stop = threading.Event()
paused = threading.Event()  # tray "Pause automation"


def _now():
    return datetime.now().astimezone()


def _guarded(fn):
    def run():
        if not paused.is_set() or fn.__name__ in jobs.MANUAL:  # "Run now" still works while paused
            fn()
    run.__name__ = fn.__name__
    return run


def start() -> None:
    sched.add_job(_guarded(jobs.sync_schedule), CronTrigger(hour=6, jitter=600), id="sync_schedule")
    sched.add_job(_guarded(jobs.poll_releases), IntervalTrigger(minutes=max(5, int(db.setting("poll_minutes")))), id="poll_releases")
    sched.add_job(_guarded(jobs.watch_downloads), IntervalTrigger(minutes=1), id="watch_downloads")
    sched.add_job(_guarded(jobs.retry_sync), IntervalTrigger(minutes=15), id="retry_sync")
    sched.add_job(_guarded(jobs.purge), CronTrigger(hour=4, jitter=600), id="purge")
    sched.add_job(jobs.check_updates, CronTrigger(hour=12, jitter=3600), id="check_updates")  # also while paused
    sched.start()
    _stop.clear()
    threading.Thread(target=_watchdog, name="resume-watchdog", daemon=True).start()
    if db.setting("setup_done"):
        catchup()


def stop() -> None:
    _stop.set()
    if sched.running:
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


def _watchdog(period: float = 30.0) -> None:
    last_wall, last_mono = time.time(), time.monotonic()
    while not _stop.wait(period):
        wall, mono = time.time(), time.monotonic()
        # monotonic stops during suspend on Linux and Windows; the wall clock doesn't
        if (wall - last_wall) - (mono - last_mono) > 120:
            log.info("resume detected (%.0f s asleep), catching up", (wall - last_wall) - (mono - last_mono))
            try:
                catchup()
            except Exception:
                log.exception("catch-up after resume failed")
        last_wall, last_mono = wall, mono


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
        out.append({"name": name, "label": label, "when": f"{cadence} · {extra}" + (" · paused" if paused.is_set() else ""),
                    "next": nxt * 1000 if nxt else None, "running": name in jobs.RUNNING,
                    "finished": end * 1000 if end else None, "error": err})
    return out
