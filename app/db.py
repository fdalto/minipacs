from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS studies (
  study_instance_uid TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL DEFAULT '', patient_name TEXT NOT NULL DEFAULT '',
  study_date TEXT NOT NULL DEFAULT '', study_time TEXT NOT NULL DEFAULT '',
  accession_number TEXT NOT NULL DEFAULT '', study_description TEXT NOT NULL DEFAULT '',
  modality TEXT NOT NULL DEFAULT '', source_ae TEXT NOT NULL DEFAULT '', destination_ae TEXT NOT NULL DEFAULT '',
  received_at TEXT NOT NULL, last_received_at TEXT NOT NULL, image_count INTEGER NOT NULL DEFAULT 0,
  total_size_bytes INTEGER NOT NULL DEFAULT 0, retention_until TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'receiving' CHECK(status IN ('receiving','ready','deleting'))
);
CREATE TABLE IF NOT EXISTS instances (
  sop_instance_uid TEXT PRIMARY KEY,
  study_instance_uid TEXT NOT NULL REFERENCES studies(study_instance_uid) ON DELETE CASCADE,
  series_instance_uid TEXT NOT NULL, instance_number TEXT NOT NULL DEFAULT '',
  file_path TEXT NOT NULL, file_size INTEGER NOT NULL, received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instances_study ON instances(study_instance_uid);
CREATE INDEX IF NOT EXISTS idx_studies_retention ON studies(retention_until, status);
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


def initialize(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        # SQLite does not apply new columns from CREATE TABLE IF NOT EXISTS to
        # installations created by earlier MiniPACS releases.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(studies)")}
        if "destination_ae" not in columns:
            conn.execute("ALTER TABLE studies ADD COLUMN destination_ae TEXT NOT NULL DEFAULT ''")


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
    conn.execute("INSERT INTO audit(timestamp, username, action, study_instance_uid, details) VALUES (?, ?, ?, ?, ?)",
                 (iso(now()), username, action, study_uid, details[:500]))


def store_instance(path: Path, metadata: dict[str, str], relative_path: str, file_size: int, retention_days: int) -> bool:
    """Atomically create/update a study. Returns False for a duplicate SOP UID."""
    timestamp = now()
    received = iso(timestamp)
    retention = iso(timestamp + timedelta(days=retention_days))
    study_uid, sop_uid = metadata["study_instance_uid"], metadata["sop_instance_uid"]
    with transaction(path) as conn:
        study = conn.execute("SELECT status, destination_ae FROM studies WHERE study_instance_uid=?", (study_uid,)).fetchone()
        if study and study["status"] == "deleting":
            raise RuntimeError("study is being deleted")
        if study and study["destination_ae"] and study["destination_ae"] != metadata["destination_ae"]:
            raise ValueError("study instance UID already belongs to another destination AE")
        existing = conn.execute("SELECT 1 FROM instances WHERE sop_instance_uid=?", (sop_uid,)).fetchone()
        if existing:
            return False
        if not study:
            # Name every column: a pre-existing database receives destination_ae
            # through ALTER TABLE, where SQLite appends it after status.
            conn.execute("""INSERT INTO studies (
                study_instance_uid, patient_id, patient_name, study_date, study_time,
                accession_number, study_description, modality, source_ae, destination_ae,
                received_at, last_received_at, image_count, total_size_bytes, retention_until, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 'receiving')""",
                         (study_uid, metadata["patient_id"], metadata["patient_name"], metadata["study_date"],
                          metadata["study_time"], metadata["accession_number"], metadata["study_description"],
                          metadata["modality"], metadata["source_ae"], metadata["destination_ae"], received, received, retention))
        conn.execute("""INSERT INTO instances VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (sop_uid, study_uid, metadata["series_instance_uid"], metadata["instance_number"], relative_path, file_size, received))
        conn.execute("""UPDATE studies SET last_received_at=?, retention_until=?, status='receiving',
                     image_count=image_count+1, total_size_bytes=total_size_bytes+? WHERE study_instance_uid=?""",
                     (received, retention, file_size, study_uid))
    return True


def list_studies(path: Path, query: str = "", destination_ae: str = "") -> list[dict[str, Any]]:
    with connect(path) as conn:
        destination = destination_ae.strip().upper()
        clauses = ["status != 'deleting'"]
        parameters: list[str] = []
        if destination:
            clauses.append("destination_ae = ?")
            parameters.append(destination)
        if query:
            like = f"%{query.strip()}%"
            clauses.append("(patient_name LIKE ? OR patient_id LIKE ? OR study_description LIKE ? OR accession_number LIKE ? OR study_date LIKE ? OR destination_ae LIKE ?)")
            parameters.extend((like, like, like, like, like, like))
        rows = conn.execute(
            f"SELECT * FROM studies WHERE {' AND '.join(clauses)} ORDER BY last_received_at DESC", parameters
        ).fetchall()
    return [dict(row) for row in rows]


def summary(path: Path, destination_ae: str = "") -> dict[str, int]:
    with connect(path) as conn:
        destination = destination_ae.strip().upper()
        if destination:
            row = conn.execute("SELECT COUNT(*) studies, COALESCE(SUM(image_count),0) images, COALESCE(SUM(total_size_bytes),0) bytes FROM studies WHERE status != 'deleting' AND destination_ae = ?", (destination,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) studies, COALESCE(SUM(image_count),0) images, COALESCE(SUM(total_size_bytes),0) bytes FROM studies WHERE status != 'deleting'").fetchone()
    return dict(row)


def get_study(path: Path, study_uid: str) -> dict[str, Any] | None:
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM studies WHERE study_instance_uid=? AND status != 'deleting'", (study_uid,)).fetchone()
    return dict(row) if row else None


def get_instances(path: Path, study_uid: str) -> list[dict[str, Any]]:
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM instances WHERE study_instance_uid=? ORDER BY series_instance_uid, instance_number", (study_uid,)).fetchall()
    return [dict(row) for row in rows]


def mark_for_deletion(path: Path, study_uids: list[str]) -> list[dict[str, Any]]:
    if not study_uids:
        return []
    marks = ",".join("?" for _ in study_uids)
    with transaction(path) as conn:
        rows = conn.execute(f"SELECT * FROM studies WHERE study_instance_uid IN ({marks}) AND status != 'deleting'", study_uids).fetchall()
        conn.execute(f"UPDATE studies SET status='deleting' WHERE study_instance_uid IN ({marks})", study_uids)
    return [dict(r) for r in rows]


def finalize_delete(path: Path, study_uid: str, username: str, action: str) -> None:
    with transaction(path) as conn:
        conn.execute("DELETE FROM studies WHERE study_instance_uid=?", (study_uid,))
        audit(conn, username, action, study_uid)


def restore_deletion(path: Path, study_uid: str) -> None:
    with transaction(path) as conn:
        conn.execute("UPDATE studies SET status='ready' WHERE study_instance_uid=?", (study_uid,))


def expired_studies(path: Path) -> list[dict[str, Any]]:
    cutoff = iso(now() - timedelta(seconds=60))
    current = iso(now())
    with connect(path) as conn:
        rows = conn.execute("""SELECT * FROM studies WHERE retention_until <= ? AND last_received_at <= ? AND status != 'deleting'""", (current, cutoff)).fetchall()
    return [dict(r) for r in rows]


def mark_ready(path: Path) -> None:
    cutoff = iso(now() - timedelta(seconds=60))
    with transaction(path) as conn:
        conn.execute("UPDATE studies SET status='ready' WHERE status='receiving' AND last_received_at <= ?", (cutoff,))
