from __future__ import annotations

import os
import sys
import threading
import time
import zipfile
from pathlib import Path

from .config import load_settings
from .db import claim_next_archive, fail_archive, finish_archive, get_instances, heartbeat_archive, initialize
from .dicom_uid import validate_uid
from .logging_utils import configure_logging
from .zip_queue import ZipBuildQueue


class WorkerHeartbeat:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._last = 0.0
        self._lock = threading.Lock()

    def beat(self, force: bool = False) -> None:
        with self._lock:
            if not force and time.monotonic() - self._last < 10:
                return
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(str(time.time()), encoding="ascii")
            os.replace(temporary, self.path)
            self._last = time.monotonic()


def build_archive(settings, study: dict, heartbeat: WorkerHeartbeat) -> tuple[Path, Path]:
    study_uid = validate_uid(study["study_instance_uid"])
    target = settings.archive_dir / f"{study_uid}.zip"
    partial = settings.archive_dir / f".{study_uid}.zip.part"
    partial.unlink(missing_ok=True)
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for instance in get_instances(settings.db_path, study_uid):
            candidate = (settings.data_dir / instance["file_path"]).resolve()
            if settings.data_dir.resolve() not in candidate.parents or not candidate.is_file():
                raise FileNotFoundError("study files are not currently available")
            archive.write(candidate, f"{study_uid}/{instance['series_instance_uid']}/{instance['sop_instance_uid']}.dcm")
            heartbeat.beat()
    return partial, target


def run_once(settings, logger, queue: ZipBuildQueue, heartbeat: WorkerHeartbeat) -> bool:
    study = claim_next_archive(settings.db_path, settings.zip_stability_seconds, settings.zip_packing_lease_seconds)
    heartbeat.beat()
    if not study:
        return False
    uid, revision = study["study_instance_uid"], study["packing_revision"]
    partial: Path | None = None
    target: Path | None = None
    try:
        with queue.slot(settings.zip_queue_timeout_seconds):
            partial, target = build_archive(settings, study, heartbeat)
        heartbeat_archive(settings.db_path, uid, revision)
        os.replace(partial, target)
        if finish_archive(settings.db_path, uid, revision, target.name, target.stat().st_size):
            logger.info("Prepared ZIP archive for study %s", uid)
        else:
            target.unlink(missing_ok=True)
            logger.info("Discarded stale ZIP archive for study %s", uid)
    except Exception as exc:
        if partial:
            partial.unlink(missing_ok=True)
        if target:
            target.unlink(missing_ok=True)
        fail_archive(settings.db_path, uid, revision, str(exc))
        logger.exception("Could not prepare ZIP archive for study %s", uid)
    finally:
        heartbeat.beat(force=True)
    return True


def healthcheck(settings) -> int:
    path = settings.logs_dir / "zip-worker.heartbeat"
    if not path.exists() or time.time() - path.stat().st_mtime > settings.zip_packing_lease_seconds:
        return 1
    return 0


def run() -> None:
    settings = load_settings()
    settings.ensure_directories()
    initialize(settings.db_path)
    logger = configure_logging("zip-worker", settings)
    heartbeat = WorkerHeartbeat(settings.logs_dir / "zip-worker.heartbeat")
    queue = ZipBuildQueue(1)
    def watchdog() -> None:
        while True:
            time.sleep(30)
            if healthcheck(settings):
                logger.critical("ZIP worker heartbeat timed out; exiting for Docker restart")
                os._exit(1)

    threading.Thread(target=watchdog, daemon=True).start()
    logger.info("ZIP worker started: stability=%ss poll=%ss", settings.zip_stability_seconds, settings.zip_worker_interval_seconds)
    while True:
        try:
            run_once(settings, logger, queue, heartbeat)
        except Exception:
            logger.exception("Unexpected ZIP worker failure")
        time.sleep(settings.zip_worker_interval_seconds)


if __name__ == "__main__":
    if "--healthcheck" in sys.argv:
        sys.exit(healthcheck(load_settings()))
    run()
