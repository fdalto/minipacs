from __future__ import annotations

import shutil
import time
from datetime import datetime, UTC
from pathlib import Path

from .config import load_settings
from .db import expired_studies, finalize_delete, initialize, mark_for_deletion, mark_ready, restore_deletion
from .logging_utils import configure_logging


def delete_study(settings, study: dict, username: str, action: str, logger) -> bool:
    uid = study["study_instance_uid"]
    # Never race a receiver that has written an instance in the previous minute.
    try:
        last_received = datetime.fromisoformat(study["last_received_at"])
        if (datetime.now(UTC) - last_received).total_seconds() < 60:
            logger.info("Postponed deletion of active study %s", uid)
            return False
    except (KeyError, ValueError):
        return False
    if (settings.data_dir / uid / ".download.lock").exists():
        logger.info("Postponed deletion of study being downloaded %s", uid)
        return False
    marked = mark_for_deletion(settings.db_path, [uid])
    if not marked:
        return False
    try:
        directory = settings.data_dir / uid
        if directory.exists():
            shutil.rmtree(directory)
        finalize_delete(settings.db_path, uid, username, action)
        logger.info("Removed study %s (%s)", uid, action)
        return True
    except OSError:
        restore_deletion(settings.db_path, uid)
        logger.exception("Could not remove study directory for %s", uid)
        return False


def remove_abandoned_zips(settings, logger) -> None:
    cutoff = time.time() - 24 * 3600
    for path in settings.tmp_dir.glob("*.zip"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                logger.info("Removed abandoned temporary ZIP")
        except OSError:
            logger.warning("Unable to remove temporary ZIP")


def run_once(settings, logger) -> None:
    mark_ready(settings.db_path)
    for study in expired_studies(settings.db_path):
        delete_study(settings, study, "system", "AUTO_DELETE", logger)
    remove_abandoned_zips(settings, logger)


def run() -> None:
    settings = load_settings()
    settings.ensure_directories()
    initialize(settings.db_path)
    logger = configure_logging("cleanup", settings)
    logger.info("Retention cleanup started")
    while True:
        try:
            run_once(settings, logger)
        except Exception:
            logger.exception("Unexpected cleanup error")
        time.sleep(3600)


if __name__ == "__main__":
    run()
