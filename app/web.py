from __future__ import annotations

import os
import re
import secrets
import shutil
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from .cleanup import delete_study
from .config import load_settings
from .db import audit, connect, get_instances, get_study, initialize, list_studies, summary
from .logging_utils import configure_logging

settings = load_settings()
settings.ensure_directories()
initialize(settings.db_path)
logger = configure_logging("web", settings)

app = FastAPI(title="MiniPACS", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, https_only=settings.cookie_secure, same_site="strict")
app.mount("/static", StaticFiles(directory=settings.root / "static"), name="static")
templates = Jinja2Templates(directory=settings.root / "templates")


def current_user(request: Request) -> str:
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="authentication required")
    return str(username)


def csrf(request: Request) -> None:
    supplied = request.headers.get("X-CSRF-Token") or request.query_params.get("csrf")
    if not supplied or not secrets.compare_digest(supplied, str(request.session.get("csrf", ""))):
        raise HTTPException(status_code=403, detail="invalid CSRF token")


def require_mutation(request: Request, username: Annotated[str, Depends(current_user)]) -> str:
    csrf(request)
    return username


def safe_download_name(study: dict) -> str:
    raw = "_".join(part for part in (study["patient_name"], study["study_date"], study["study_description"]) if part) or "study"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")[:100]
    return safe or "study"


def study_files(study_uid: str) -> list[tuple[dict, Path]]:
    files: list[tuple[dict, Path]] = []
    for instance in get_instances(settings.db_path, study_uid):
        candidate = (settings.data_dir / instance["file_path"]).resolve()
        # The database only stores internal relative paths; keep a second guard for corrupted rows.
        if settings.data_dir.resolve() not in candidate.parents or not candidate.is_file():
            raise HTTPException(409, "study files are not currently available")
        files.append((instance, candidate))
    return files


@contextmanager
def download_locks(studies: list[dict]):
    locks: list[Path] = []
    try:
        for study in studies:
            lock = settings.data_dir / study["study_instance_uid"] / ".download.lock"
            try:
                lock.touch(exist_ok=False)
            except FileExistsError:
                raise HTTPException(409, "study is already being downloaded")
            locks.append(lock)
        yield
    finally:
        for lock in locks:
            lock.unlink(missing_ok=True)


def create_zip(studies: list[dict], bulk: bool) -> Path:
    token = secrets.token_urlsafe(16)
    target = settings.tmp_dir / f"download-{token}.zip"
    used: set[str] = set()
    try:
        with download_locks(studies), zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for study in studies:
                base = safe_download_name(study) if bulk else study["study_instance_uid"]
                if bulk:
                    original = base
                    sequence = 2
                    while base in used:
                        base = f"{original}_{sequence}"
                        sequence += 1
                    used.add(base)
                for instance, file_path in study_files(study["study_instance_uid"]):
                    archive_name = f"{base}/{instance['series_instance_uid']}/{instance['sop_instance_uid']}.dcm"
                    archive.write(file_path, archive_name)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def remove_temp(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove completed temporary ZIP")


@app.get("/health")
def health() -> dict[str, str]:
    try:
        with connect(settings.db_path) as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if request.session.get("username"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: Annotated[str, Form()], password: Annotated[str, Form()]):
    valid = False
    try:
        valid = username == settings.admin_username and bool(settings.admin_password_hash) and PasswordHasher().verify(settings.admin_password_hash, password)
    except (VerifyMismatchError, Exception) as exc:
        # Invalid/malformed configured hashes are handled identically, without exposing their nature.
        if not isinstance(exc, VerifyMismatchError):
            logger.warning("Login hash verification failed")
    with connect(settings.db_path) as conn:
        audit(conn, username[:100], "LOGIN_SUCCESS" if valid else "LOGIN_FAILURE")
        conn.commit()
    if not valid:
        return templates.TemplateResponse(request, "login.html", {"error": "Usuário ou senha inválidos."}, status_code=401)
    request.session.clear()
    request.session["username"] = settings.admin_username
    request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request, username: Annotated[str, Depends(require_mutation)]):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, username: Annotated[str, Depends(current_user)]):
    return templates.TemplateResponse(request, "index.html", {"username": username, "csrf_token": request.session["csrf"]})


@app.get("/api/studies")
def studies(request: Request, q: str = "", username: Annotated[str, Depends(current_user)] = ""):
    return {"studies": list_studies(settings.db_path, q[:200]), "summary": summary(settings.db_path)}


@app.delete("/api/studies/{study_uid}")
def delete_one(study_uid: str, request: Request, username: Annotated[str, Depends(require_mutation)]):
    study = get_study(settings.db_path, study_uid)
    if not study:
        raise HTTPException(404, "study not found")
    if not delete_study(settings, study, username, "DELETE", logger):
        raise HTTPException(409, "study could not be deleted")
    return {"deleted": 1}


def validate_uid_list(study_uids: object) -> list[str]:
    if not isinstance(study_uids, list) or not study_uids or len(study_uids) > settings.max_bulk_studies:
        raise HTTPException(400, "invalid number of studies")
    values = [value for value in study_uids if isinstance(value, str) and 0 < len(value) <= 64]
    if len(values) != len(study_uids) or len(set(values)) != len(values):
        raise HTTPException(400, "invalid study identifiers")
    return values


@app.post("/api/studies/delete-bulk")
async def delete_bulk(request: Request, username: Annotated[str, Depends(require_mutation)]):
    body = await request.json()
    uids = validate_uid_list(body.get("study_uids"))
    requested = [get_study(settings.db_path, uid) for uid in uids]
    studies_to_delete = [item for item in requested if item]
    deleted = sum(delete_study(settings, study, username, "BULK_DELETE", logger) for study in studies_to_delete)
    return {"deleted": deleted}


@app.get("/download/study/{study_uid}")
def download_study(study_uid: str, username: Annotated[str, Depends(current_user)]):
    study = get_study(settings.db_path, study_uid)
    if not study:
        raise HTTPException(404, "study not found")
    target = create_zip([study], bulk=False)
    with connect(settings.db_path) as conn:
        audit(conn, username, "DOWNLOAD", study_uid)
        conn.commit()
    return FileResponse(target, media_type="application/zip", filename=f"{safe_download_name(study)}.zip", background=BackgroundTask(remove_temp, target))


@app.post("/download/bulk")
async def download_bulk(request: Request, username: Annotated[str, Depends(require_mutation)]):
    csrf(request)
    body = await request.json()
    uids = validate_uid_list(body.get("study_uids"))
    chosen = [get_study(settings.db_path, uid) for uid in uids]
    studies_to_zip = [item for item in chosen if item]
    total = sum(int(item["total_size_bytes"]) for item in studies_to_zip)
    if not studies_to_zip or total > settings.max_bulk_bytes:
        raise HTTPException(400, "requested studies exceed permitted download limits")
    target = create_zip(studies_to_zip, bulk=True)
    with connect(settings.db_path) as conn:
        audit(conn, username, "BULK_DOWNLOAD", details=f"studies={len(studies_to_zip)}")
        conn.commit()
    return FileResponse(target, media_type="application/zip", filename="minipacs-studies.zip", background=BackgroundTask(remove_temp, target))


def run() -> None:
    import uvicorn
    uvicorn.run("app.web:app", host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    run()
