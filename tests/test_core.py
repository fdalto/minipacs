from datetime import timedelta
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import load_settings
from app.db import connect, get_study, initialize, store_instance
from app.dicom_receiver import extract_metadata
from app.web import app


def metadata(study="1.2.3", series="1.2.3.4", sop="1.2.3.4.5"):
    return {"patient_id":"P1","patient_name":"Patient Test","study_date":"20260101","study_time":"120000","accession_number":"A1","study_description":"Synthetic","modality":"OT","study_instance_uid":study,"series_instance_uid":series,"sop_instance_uid":sop,"instance_number":"1","source_ae":"TESTSCU"}


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


def test_retention_is_from_receipt_time(tmp_path):
    path = fresh_db(tmp_path); store_instance(path, metadata(), "x", 1, 15)
    study = get_study(path, "1.2.3")
    from datetime import datetime
    assert datetime.fromisoformat(study["retention_until"]) - datetime.fromisoformat(study["last_received_at"]) == timedelta(days=15)


def test_missing_dicom_tags_are_safe():
    from pydicom.dataset import Dataset
    values = extract_metadata(Dataset(), "TESTSCU")
    assert values["patient_name"] == "" and values["study_instance_uid"] == ""


def test_authentication_and_protected_endpoint():
    client = TestClient(app)
    assert client.get("/api/studies").status_code == 401
    assert client.post("/login", data={"username":"admin", "password":"wrong"}).status_code == 401
    assert client.post("/login", data={"username":"admin", "password":"correct horse battery staple"}, follow_redirects=False).status_code == 303


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
