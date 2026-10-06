from datetime import timedelta
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import load_settings
from app.db import connect, get_study, initialize, store_instance
from app.dicom_receiver import _safe_uid, extract_metadata
from app.dicom_uid import validate_uid
from app.web import app


def metadata(study="1.2.3", series="1.2.3.4", sop="1.2.3.4.5", destination="VITOR"):
    return {"patient_id":"P1","patient_name":"Patient Test","study_date":"20260101","study_time":"120000","accession_number":"A1","study_description":"Synthetic","modality":"OT","study_instance_uid":study,"series_instance_uid":series,"sop_instance_uid":sop,"instance_number":"1","source_ae":"TESTSCU","destination_ae":destination}


def fresh_db(tmp_path: Path) -> Path:
    path = tmp_path / "db.sqlite3"; initialize(path); return path


def test_sqlite_initialization(tmp_path):
    path = fresh_db(tmp_path)
    with connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='studies'").fetchone()


def test_study_insert_and_duplicate_sop(tmp_path):
    path = fresh_db(tmp_path)
    assert store_instance(path, metadata(), "1.2.3/1.2.3.4/1.2.3.4.5.dcm", 10, 15)
    assert not store_instance(path, metadata(), "anything", 10, 15)
    study = get_study(path, "1.2.3")
    assert study["image_count"] == 1 and study["total_size_bytes"] == 10
    assert study["destination_ae"] == "VITOR"


def test_study_cannot_cross_destination_ae_boundaries(tmp_path):
    import pytest
    path = fresh_db(tmp_path)
    assert store_instance(path, metadata(), "first", 10, 15)
    with pytest.raises(ValueError, match="destination AE"):
        store_instance(path, metadata(sop="1.2.3.4.6", destination="FELIPE"), "second", 10, 15)


def test_existing_database_is_migrated_with_destination_ae(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with connect(path) as conn:
        conn.execute("""CREATE TABLE studies (
            study_instance_uid TEXT PRIMARY KEY, patient_id TEXT NOT NULL DEFAULT '', patient_name TEXT NOT NULL DEFAULT '',
            study_date TEXT NOT NULL DEFAULT '', study_time TEXT NOT NULL DEFAULT '', accession_number TEXT NOT NULL DEFAULT '',
            study_description TEXT NOT NULL DEFAULT '', modality TEXT NOT NULL DEFAULT '', source_ae TEXT NOT NULL DEFAULT '',
            received_at TEXT NOT NULL, last_received_at TEXT NOT NULL, image_count INTEGER NOT NULL DEFAULT 0,
            total_size_bytes INTEGER NOT NULL DEFAULT 0, retention_until TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'receiving'
        )""")
    initialize(path)
    assert store_instance(path, metadata(), "legacy-path", 10, 15)
    assert get_study(path, "1.2.3")["destination_ae"] == "VITOR"


def test_retention_is_from_receipt_time(tmp_path):
    path = fresh_db(tmp_path); store_instance(path, metadata(), "x", 1, 15)
    study = get_study(path, "1.2.3")
    from datetime import datetime
    assert datetime.fromisoformat(study["retention_until"]) - datetime.fromisoformat(study["last_received_at"]) == timedelta(days=15)


def test_missing_dicom_tags_are_safe():
    from pydicom.dataset import Dataset
    values = extract_metadata(Dataset(), "TESTSCU")
    assert values["patient_name"] == "" and values["study_instance_uid"] == ""


def test_dicom_uid_rejects_path_components():
    import pytest
    for value in (".", "..", ".1", "1.", "1..2", "01.2", "1/2"):
        with pytest.raises(ValueError):
            _safe_uid(value)
    assert _safe_uid("1.2.840.10008.1.2.1") == "1.2.840.10008.1.2.1"
    assert validate_uid("2.25.123") == "2.25.123"


def test_authentication_and_protected_endpoint():
    client = TestClient(app)
    assert client.get("/api/studies").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.post("/login", data={"username":"admin", "password":"wrong"}).status_code == 401
    assert client.post("/login", data={"username":"admin", "password":"correct horse battery staple"}, follow_redirects=False).status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200


def test_external_api_requires_bearer_token_and_limits_cors():
    client = TestClient(app)
    endpoint = "/api/v1/studies"
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers={"Authorization": "Bearer wrong"}).status_code == 401
    valid = client.get(endpoint, headers={"Authorization": "Bearer test-external-api-token", "Origin": "https://portal.example.test"})
    assert valid.status_code == 200
    assert valid.headers["access-control-allow-origin"] == "https://portal.example.test"
    assert "access-control-allow-credentials" not in valid.headers


def test_delete_requires_csrf_and_removes_study():
    # Web routes use the isolated runtime configured in conftest.
    settings = load_settings(); initialize(settings.db_path)
    uid="1.2.840.1"; sop="1.2.840.1.1"; series="1.2.840.1.0"
    directory=settings.data_dir / uid / series; directory.mkdir(parents=True, exist_ok=True)
    target=directory / f"{sop}.dcm"; target.write_bytes(b"DICOM")
    store_instance(settings.db_path, metadata(uid, series, sop), target.relative_to(settings.data_dir).as_posix(), 5, 15)
    with connect(settings.db_path) as conn:
        conn.execute("UPDATE studies SET last_received_at='2020-01-01T00:00:00+00:00' WHERE study_instance_uid=?", (uid,))
        conn.commit()
    client=TestClient(app)
    assert client.delete(f"/api/studies/{uid}").status_code == 401
    client.post("/login", data={"username":"admin", "password":"correct horse battery staple"})
    page = client.get("/").text
    token = re.search(r'data-csrf="([^"]+)"', page).group(1)
    assert client.delete(f"/api/studies/{uid}").status_code == 403
    assert client.delete(f"/api/studies/{uid}", headers={"X-CSRF-Token": token}).status_code == 200
    assert get_study(settings.db_path, uid) is None
    assert not (settings.data_dir / uid).exists()
