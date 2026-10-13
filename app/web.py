from __future__ import annotations

import re
import secrets
import threading
import time
from pathlib import Path
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .cleanup import delete_study
from .config import load_settings
from .db import audit, connect, get_study, initialize, list_studies, summary
from .dicom_uid import validate_uid
from .logging_utils import configure_logging

settings = load_settings()
settings.ensure_directories()
initialize(settings.db_path)
logger = configure_logging("web", settings)
weasis_link_serializer = URLSafeTimedSerializer(settings.session_secret, salt="minipacs-weasis-download")
WEASIS_LINK_TTL_SECONDS = 60

app = FastAPI(title="MiniPACS", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, https_only=settings.cookie_secure, same_site="strict")
if settings.external_api_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.external_api_allowed_origins),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )
app.mount("/static", StaticFiles(directory=settings.root / "static"), name="static")
templates = Jinja2Templates(directory=settings.root / "templates")
_api_requests: dict[str, list[float]] = {}
_api_rate_lock = threading.Lock()


def current_user(request: Request) -> str:
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="authentication required")
    return str(username)


def _api_rate_limit(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    cutoff = time.monotonic() - 60
    with _api_rate_lock:
        recent = [timestamp for timestamp in _api_requests.get(client, []) if timestamp > cutoff]
        if len(recent) >= settings.external_api_rate_limit_per_minute:
            raise HTTPException(status_code=429, detail="rate limit exceeded", headers={"Retry-After": "60"})
        recent.append(time.monotonic())
        _api_requests[client] = recent


def external_api_principal(request: Request) -> str:
    """Authenticate machine-to-machine requests; browser users must not hold this token."""
    if not settings.external_api_token_hash:
        raise HTTPException(status_code=404, detail="not found")
    _api_rate_limit(request)
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="invalid API credentials", headers={"WWW-Authenticate": "Bearer"})
    try:
        valid = PasswordHasher().verify(settings.external_api_token_hash, token)
    except (VerifyMismatchError, InvalidHashError):
        valid = False
    if not valid:
        raise HTTPException(status_code=401, detail="invalid API credentials", headers={"WWW-Authenticate": "Bearer"})
    return f"api:{settings.external_api_token_name}"


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


def ready_archive(study: dict) -> Path:
    if study["status"] != "ready" or not study.get("archive_path"):
        raise HTTPException(status_code=409, detail="study is still being prepared", headers={"Retry-After": "30"})
    archive_name = str(study["archive_path"])
    target = (settings.archive_dir / archive_name).resolve()
    if target.parent != settings.archive_dir.resolve() or not target.is_file():
        raise HTTPException(status_code=503, detail="study archive is temporarily unavailable", headers={"Retry-After": "30"})
    return target


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
def index(request: Request):
    username = request.session.get("username")
    if not username:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "index.html", {"username": username, "csrf_token": request.session["csrf"]})


@app.get("/api/studies")
def studies(request: Request, q: str = "", destination_ae: str = "", username: Annotated[str, Depends(current_user)] = ""):
    return {"studies": list_studies(settings.db_path, q[:200], destination_ae), "summary": summary(settings.db_path, destination_ae)}


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
    try:
        values = [validate_uid(value) if isinstance(value, str) else "" for value in study_uids]
    except ValueError:
        values = []
    if len(values) != len(study_uids) or not all(values) or len(set(values)) != len(values):
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
    target = ready_archive(study)
    with connect(settings.db_path) as conn:
        audit(conn, username, "DOWNLOAD", study_uid)
        conn.commit()
    return FileResponse(target, media_type="application/zip", filename=f"{safe_download_name(study)}.zip", headers={"Cache-Control": "no-store"})


@app.post("/api/studies/{study_uid}/weasis-link")
def create_weasis_link(study_uid: str, request: Request, username: Annotated[str, Depends(require_mutation)]):
    study = get_study(settings.db_path, study_uid)
    if not study:
        raise HTTPException(404, "study not found")
    ready_archive(study)
    token = weasis_link_serializer.dumps({"study_uid": study_uid, "username": username})
    download_url = str(request.url_for("weasis_download", study_uid=study_uid).include_query_params(token=token))
    with connect(settings.db_path) as conn:
        audit(conn, username, "WEASIS_LINK", study_uid)
        conn.commit()
    return {"download_url": download_url, "expires_in": WEASIS_LINK_TTL_SECONDS}


@app.get("/weasis/studies/{study_uid}/download", name="weasis_download")
def weasis_download(study_uid: str, token: str):
    try:
        payload = weasis_link_serializer.loads(token, max_age=WEASIS_LINK_TTL_SECONDS)
    except SignatureExpired as exc:
        raise HTTPException(401, "Weasis download link has expired") from exc
    except BadSignature as exc:
        raise HTTPException(401, "invalid Weasis download link") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("study_uid") != study_uid
        or not isinstance(payload.get("username"), str)
    ):
        raise HTTPException(403, "Weasis download link does not match this study")
    study = get_study(settings.db_path, study_uid)
    if not study:
        raise HTTPException(404, "study not found")
    target = ready_archive(study)
    with connect(settings.db_path) as conn:
        audit(conn, payload["username"][:100], "WEASIS_DOWNLOAD", study_uid)
        conn.commit()
    return FileResponse(
        target,
        media_type="application/zip",
        filename=f"{safe_download_name(study)}.zip",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/download/bulk")
async def download_bulk(request: Request, username: Annotated[str, Depends(require_mutation)]):
    raise HTTPException(410, "bulk archive generation has been removed; download ready studies individually")


def external_study(study: dict) -> dict[str, object]:
    """Deliberately expose only fields needed by a portal; never leak paths or operational metadata."""
    return {
        "study_instance_uid": study["study_instance_uid"],
        "patient_id": study["patient_id"],
        "patient_name": study["patient_name"],
        "study_date": study["study_date"],
        "study_time": study["study_time"],
        "accession_number": study["accession_number"],
        "study_description": study["study_description"],
        "modality": study["modality"],
        "destination_ae": study["destination_ae"],
        "received_at": study["received_at"],
        "last_received_at": study["last_received_at"],
        "image_count": study["image_count"],
        "total_size_bytes": study["total_size_bytes"],
        "status": study["status"],
    }


@app.get("/api/v1/studies")
def external_studies(request: Request, q: str = "", destination_ae: str = "", principal: Annotated[str, Depends(external_api_principal)] = ""):
    studies = [external_study(study) for study in list_studies(settings.db_path, q[:200], destination_ae)]
    return {"studies": studies, "summary": summary(settings.db_path, destination_ae)}


@app.get("/api/v1/studies/{study_uid}")
def external_study_detail(study_uid: str, principal: Annotated[str, Depends(external_api_principal)] = ""):
    study = get_study(settings.db_path, study_uid)
    if not study:
        raise HTTPException(404, "study not found")
    return external_study(study)


@app.get("/api/v1/studies/{study_uid}/download")
def external_download_study(study_uid: str, principal: Annotated[str, Depends(external_api_principal)] = ""):
    study = get_study(settings.db_path, study_uid)
    if not study:
        raise HTTPException(404, "study not found")
    target = ready_archive(study)
    with connect(settings.db_path) as conn:
        audit(conn, principal, "API_DOWNLOAD", study_uid)
        conn.commit()
    return FileResponse(target, media_type="application/zip", filename=f"{safe_download_name(study)}.zip", headers={"Cache-Control": "no-store"})


def run() -> None:
    import uvicorn
    uvicorn.run("app.web:app", host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    run()
