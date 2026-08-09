"""Automated daily site backup.

WHY THIS EXISTS: until 2026-08-09 this deployment had **no automated backup at
all**, and nobody knew. The scheduler was healthy, but Frappe ships no
backup-creating job — upstream relies on a host crontab written by
``bench setup backups``, which a Docker deployment never gets. The only backup
job present on the site was the hourly one that *deletes* backups down to
``backup_limit``.

Every backup production had ever held was a side effect of
``deploy_backend.ps1`` running ``bench backup`` before a deploy, gated on
``-not $SkipBackup -and $deployRequired``. So backup coverage was a function of
how recently somebody deployed, and it silently dropped to zero the moment
deploys started passing ``-SkipBackup``.

Putting the schedule here rather than in a host crontab is deliberate: this
file is git-tracked and reaches staging and production through the ordinary
deploy pipeline, so the two environments cannot drift, and the schedule cannot
be lost by rebuilding a box.

This is a LOCAL restore point on the same volume as the database. It protects
against bad migrations, bad deploys and human error — not against losing the
volume. Off-instance protection (EBS snapshots / an object-store copy) is a
separate concern and is not something this app can arrange for itself.
"""

import frappe

LOGGER_NAME = "jarz_backup"

# Frappe's own scheduled_backup() no-ops when a backup younger than this many
# hours already exists, so a same-day redeploy cannot spam the backups folder.
# Slightly under 24h so a daily run is never skipped for being a few minutes
# early relative to the previous day's.
_MIN_AGE_HOURS = 20

# Frappe defaults System Settings.backup_limit to 3. With a DAILY schedule that
# is three days of history, which is too short to notice a corruption that
# happened over a weekend. The files are ~52 MB a set against 32 GB free.
_MIN_BACKUP_LIMIT = 7


def _logger():
    # ERROR level so it is actually visible: the default log level off a dev
    # server swallows .info()/.warning() entirely.
    logger = frappe.logger(LOGGER_NAME, allow_site=True)
    try:
        import logging

        logger.setLevel(logging.INFO)
    except Exception:
        pass
    return logger


def ensure_backup_retention():
    """Raise System Settings.backup_limit to a sane floor. Never lowers it."""
    try:
        current = frappe.db.get_single_value("System Settings", "backup_limit")
        current = int(current or 0)
    except Exception:
        return
    if current >= _MIN_BACKUP_LIMIT:
        return
    try:
        frappe.db.set_single_value(
            "System Settings", "backup_limit", _MIN_BACKUP_LIMIT
        )
        frappe.db.commit()
        _logger().info(
            f"backup_limit raised {current} -> {_MIN_BACKUP_LIMIT}"
        )
    except Exception:
        _logger().error("ensure_backup_retention failed", exc_info=True)


def daily_backup():
    """Take a database + files backup. Never raises.

    Files are included because they are trivially small here (~2 MB of public
    and private files against a ~1 GB database), so excluding them would save
    nothing while making a restore incomplete.

    Memory is NOT a concern despite this being a 1.9 GB box: ``take_dump``
    builds a ``mariadb-dump | gzip`` shell pipeline and hands it to the OS with
    ``os.nice(10)``, streaming to disk rather than buffering in Python. The
    dominant cost is the worker that is already running.
    """
    logger = _logger()
    try:
        from frappe.utils.backups import scheduled_backup

        ensure_backup_retention()

        backup = scheduled_backup(
            older_than=_MIN_AGE_HOURS,
            ignore_files=False,
            force=False,
        )
        if backup is None:
            logger.info("daily_backup: skipped, a recent backup already exists")
            return {"taken": False}

        path = getattr(backup, "backup_path_db", None)
        logger.info(f"daily_backup: wrote {path}")
        return {"taken": True, "path": path}
    except Exception:
        # A failed backup must be loud — this is the one job whose silent
        # failure is indistinguishable from success until a restore is needed.
        logger.error("daily_backup FAILED", exc_info=True)
        try:
            frappe.log_error(
                title="jarz_pos daily_backup failed",
                message=frappe.get_traceback(),
            )
        except Exception:
            pass
        return {"taken": False, "error": True}
