from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

STUDIES_TABLE = """
CREATE TABLE IF NOT EXISTS studies (
  study_instance_uid TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL DEFAULT '', patient_name TEXT NOT NULL DEFAULT '',
  study_date TEXT NOT NULL DEFAULT '', study_time TEXT NOT NULL DEFAULT '',
  accession_number TEXT NOT NULL DEFAULT '', study_description TEXT NOT NULL DEFAULT '',
  modality TEXT NOT NULL DEFAULT '', source_ae TEXT NOT NULL DEFAULT '', destination_ae TEXT NOT NULL DEFAULT '',
  received_at TEXT NOT NULL, last_received_at TEXT NOT NULL, image_count INTEGER NOT NULL DEFAULT 0,
  total_size_bytes INTEGER NOT NULL DEFAULT 0, retention_until TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'receiving' CHECK(status IN ('receiving','packing','ready','deleting')),
  content_revision INTEGER NOT NULL DEFAULT 0, packing_revision INTEGER NOT NULL DEFAULT 0,
  packing_started_at TEXT NOT NULL DEFAULT '', packing_heartbeat_at TEXT NOT NULL DEFAULT '',
  archive_path TEXT NOT NULL DEFAULT '', archive_size_bytes INTEGER NOT NULL DEFAULT 0,
  archive_ready_at TEXT NOT NULL DEFAULT '', archive_retry_after TEXT NOT NULL DEFAULT '',
  zip_attempts INTEGER NOT NULL DEFAULT 0, zip_error TEXT NOT NULL DEFAULT ''
)
"""

SCHEMA = STUDIES_TABLE + ";\n" + """
CREATE TABLE IF NOT EXISTS instances (
  sop_instance_uid TEXT PRIMARY KEY,
  study_instance_uid TEXT NOT NULL REFERENCES studies(study_instance_uid) ON DELETE CASCADE,
  series_instance_uid TEXT NOT NULL, instance_number TEXT NOT NULL DEFAULT '',
  file_path TEXT NOT NULL, file_size INTEGER NOT NULL, received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL, study_instance_uid TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT ''
);
"""


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(value: datetime) -> str:
    return value.isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _rebuild_studies_for_packing(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("DROP INDEX IF EXISTS idx_studies_retention")
        conn.execute("DROP INDEX IF EXISTS idx_studies_archive_queue")
        conn.execute(STUDIES_TABLE.replace("CREATE TABLE IF NOT EXISTS studies", "CREATE TABLE studies_new"))
        conn.execute("""INSERT INTO studies_new (
            study_instance_uid, patient_id, patient_name, study_date, study_time,
            accession_number, study_description, modality, source_ae, destination_ae,
            received_at, last_received_at, image_count, total_size_bytes, retention_until, status
        ) SELECT study_instance_uid, patient_id, patient_name, study_date, study_time,
            accession_number, study_description, modality, source_ae, destination_ae,
            received_at, last_received_at, image_count, total_size_bytes, retention_until,
            CASE WHEN status='deleting' THEN 'deleting' ELSE 'receiving' END FROM studies""")
        conn.execute("DROP TABLE studies")
        conn.execute("ALTER TABLE studies_new RENAME TO studies")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def initialize(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(studies)")}
        if "destination_ae" not in columns:
            conn.execute("ALTER TABLE studies ADD COLUMN destination_ae TEXT NOT NULL DEFAULT ''")
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='studies'").fetchone()["sql"]
        if "'packing'" not in sql:
            _rebuild_studies_for_packing(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(studies)")}
        additions = {
            "content_revision": "INTEGER NOT NULL DEFAULT 0", "packing_revision": "INTEGER NOT NULL DEFAULT 0",
            "packing_started_at": "TEXT NOT NULL DEFAULT ''", "packing_heartbeat_at": "TEXT NOT NULL DEFAULT ''",
            "archive_path": "TEXT NOT NULL DEFAULT ''", "archive_size_bytes": "INTEGER NOT NULL DEFAULT 0",
            "archive_ready_at": "TEXT NOT NULL DEFAULT ''", "archive_retry_after": "TEXT NOT NULL DEFAULT ''",
            "zip_attempts": "INTEGER NOT NULL DEFAULT 0", "zip_error": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE studies ADD COLUMN {name} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_instances_study ON instances(study_instance_uid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_studies_retention ON studies(retention_until, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_studies_archive_queue ON studies(status, last_received_at, archive_retry_after)")


@contextmanager
def transaction(path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def audit(conn: sqlite3.Connection, username: str, action: str, study_uid: str = "", details: str = "") -> None:
    conn.execute("INSERT INTO audit(timestamp, username, action, study_instance_uid, details) VALUES (?, ?, ?, ?, ?)", (iso(now()), username, action, study_uid, details[:500]))


def store_instance(path: Path, metadata: dict[str, str], relative_path: str, file_size: int, retention_days: int) -> bool:
    timestamp = now(); received = iso(timestamp); retention = iso(timestamp + timedelta(days=retention_days))
    study_uid, sop_uid = metadata["study_instance_uid"], metadata["sop_instance_uid"]
    with transaction(path) as conn:
        study = conn.execute("SELECT status, destination_ae FROM studies WHERE study_instance_uid=?", (study_uid,)).fetchone()
        if study and study["status"] == "deleting": raise RuntimeError("study is being deleted")
        if study and study["destination_ae"] and study["destination_ae"] != metadata["destination_ae"]: raise ValueError("study instance UID already belongs to another destination AE")
        if conn.execute("SELECT 1 FROM instances WHERE sop_instance_uid=?", (sop_uid,)).fetchone(): return False
        if not study:
            conn.execute("""INSERT INTO studies (study_instance_uid, patient_id, patient_name, study_date, study_time, accession_number, study_description, modality, source_ae, destination_ae, received_at, last_received_at, image_count, total_size_bytes, retention_until, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 'receiving')""", (study_uid, metadata["patient_id"], metadata["patient_name"], metadata["study_date"], metadata["study_time"], metadata["accession_number"], metadata["study_description"], metadata["modality"], metadata["source_ae"], metadata["destination_ae"], received, received, retention))
        conn.execute("INSERT INTO instances VALUES (?, ?, ?, ?, ?, ?, ?)", (sop_uid, study_uid, metadata["series_instance_uid"], metadata["instance_number"], relative_path, file_size, received))
        conn.execute("""UPDATE studies SET last_received_at=?, retention_until=?, status='receiving', image_count=image_count+1, total_size_bytes=total_size_bytes+?, content_revision=content_revision+1, packing_revision=0, packing_started_at='', packing_heartbeat_at='', archive_path='', archive_size_bytes=0, archive_ready_at='', archive_retry_after='', zip_error='' WHERE study_instance_uid=?""", (received, retention, file_size, study_uid))
    return True


def list_studies(path: Path, query: str = "", destination_ae: str = "") -> list[dict[str, Any]]:
    with connect(path) as conn:
        clauses = ["status != 'deleting'"]; params: list[str] = []
        if destination_ae.strip(): clauses.append("destination_ae = ?"); params.append(destination_ae.strip().upper())
        if query:
            like = f"%{query.strip()}%"; clauses.append("(patient_name LIKE ? OR patient_id LIKE ? OR study_description LIKE ? OR accession_number LIKE ? OR study_date LIKE ? OR destination_ae LIKE ?)"); params.extend((like, like, like, like, like, like))
        rows = conn.execute(f"SELECT * FROM studies WHERE {' AND '.join(clauses)} ORDER BY last_received_at DESC", params).fetchall()
    return [dict(row) for row in rows]


def summary(path: Path, destination_ae: str = "") -> dict[str, int]:
    with connect(path) as conn:
        if destination_ae.strip(): row = conn.execute("SELECT COUNT(*) studies, COALESCE(SUM(image_count),0) images, COALESCE(SUM(total_size_bytes),0) bytes FROM studies WHERE status != 'deleting' AND destination_ae = ?", (destination_ae.strip().upper(),)).fetchone()
        else: row = conn.execute("SELECT COUNT(*) studies, COALESCE(SUM(image_count),0) images, COALESCE(SUM(total_size_bytes),0) bytes FROM studies WHERE status != 'deleting'").fetchone()
    return dict(row)


def get_study(path: Path, study_uid: str) -> dict[str, Any] | None:
    with connect(path) as conn: row = conn.execute("SELECT * FROM studies WHERE study_instance_uid=? AND status != 'deleting'", (study_uid,)).fetchone()
    return dict(row) if row else None


def get_instances(path: Path, study_uid: str) -> list[dict[str, Any]]:
    with connect(path) as conn: rows = conn.execute("SELECT * FROM instances WHERE study_instance_uid=? ORDER BY series_instance_uid, instance_number", (study_uid,)).fetchall()
    return [dict(row) for row in rows]


def claim_next_archive(path: Path, stability_seconds: int, lease_seconds: int) -> dict[str, Any] | None:
    timestamp = now(); stable = iso(timestamp - timedelta(seconds=stability_seconds)); stale = iso(timestamp - timedelta(seconds=lease_seconds)); current = iso(timestamp)
    with transaction(path) as conn:
        conn.execute("UPDATE studies SET status='receiving', packing_revision=0, packing_started_at='', packing_heartbeat_at='', zip_error='previous ZIP worker lease expired' WHERE status='packing' AND packing_heartbeat_at != '' AND packing_heartbeat_at <= ?", (stale,))
        row = conn.execute("SELECT * FROM studies WHERE status='receiving' AND last_received_at <= ? AND (archive_retry_after='' OR archive_retry_after <= ?) ORDER BY last_received_at LIMIT 1", (stable, current)).fetchone()
        if not row: return None
        study = dict(row)
        changed = conn.execute("UPDATE studies SET status='packing', packing_revision=content_revision, packing_started_at=?, packing_heartbeat_at=?, zip_error='' WHERE study_instance_uid=? AND status='receiving' AND content_revision=?", (current, current, study["study_instance_uid"], study["content_revision"])).rowcount
        if not changed: return None
        study["status"] = "packing"; study["packing_revision"] = study["content_revision"]
        return study


def heartbeat_archive(path: Path, study_uid: str, revision: int) -> None:
    with transaction(path) as conn: conn.execute("UPDATE studies SET packing_heartbeat_at=? WHERE study_instance_uid=? AND status='packing' AND packing_revision=?", (iso(now()), study_uid, revision))


def finish_archive(path: Path, study_uid: str, revision: int, archive_name: str, archive_size: int) -> bool:
    with transaction(path) as conn:
        changed = conn.execute("UPDATE studies SET status='ready', archive_path=?, archive_size_bytes=?, archive_ready_at=?, packing_started_at='', packing_heartbeat_at='', archive_retry_after='', zip_error='' WHERE study_instance_uid=? AND status='packing' AND content_revision=? AND packing_revision=?", (archive_name, archive_size, iso(now()), study_uid, revision, revision)).rowcount
    return bool(changed)


def fail_archive(path: Path, study_uid: str, revision: int, error: str) -> None:
    with transaction(path) as conn: conn.execute("UPDATE studies SET status='receiving', packing_revision=0, packing_started_at='', packing_heartbeat_at='', archive_retry_after=?, zip_attempts=zip_attempts+1, zip_error=? WHERE study_instance_uid=? AND status='packing' AND packing_revision=?", (iso(now() + timedelta(seconds=60)), error[:500], study_uid, revision))


def mark_for_deletion(path: Path, study_uids: list[str]) -> list[dict[str, Any]]:
    if not study_uids: return []
    marks = ",".join("?" for _ in study_uids)
    with transaction(path) as conn:
        rows = conn.execute(f"SELECT * FROM studies WHERE study_instance_uid IN ({marks}) AND status != 'deleting'", study_uids).fetchall(); conn.execute(f"UPDATE studies SET status='deleting' WHERE study_instance_uid IN ({marks})", study_uids)
    return [dict(row) for row in rows]


def finalize_delete(path: Path, study_uid: str, username: str, action: str) -> None:
    with transaction(path) as conn: conn.execute("DELETE FROM studies WHERE study_instance_uid=?", (study_uid,)); audit(conn, username, action, study_uid)


def restore_deletion(path: Path, study_uid: str) -> None:
    with transaction(path) as conn: conn.execute("UPDATE studies SET status='receiving', packing_revision=0, packing_started_at='', packing_heartbeat_at='' WHERE study_instance_uid=?", (study_uid,))


def expired_studies(path: Path) -> list[dict[str, Any]]:
    cutoff = iso(now() - timedelta(seconds=60)); current = iso(now())
    with connect(path) as conn: rows = conn.execute("SELECT * FROM studies WHERE retention_until <= ? AND last_received_at <= ? AND status != 'deleting'", (current, cutoff)).fetchall()
    return [dict(row) for row in rows]
