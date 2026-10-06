from __future__ import annotations

import os
import string
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str, default: bool = False) -> bool:
    return value.lower() in {"1", "true", "yes", "on"} if value else default


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    db_path: Path
    tmp_dir: Path
    logs_dir: Path
    dicom_ae_title: str
    dicom_destination_aes: frozenset[str]
    dicom_port: int
    allowed_calling_aes: frozenset[str]
    retention_days: int
    admin_username: str
    admin_password_hash: str
    session_secret: str
    cookie_secure: bool
    max_bulk_studies: int
    max_bulk_bytes: int
    max_dicom_file_bytes: int
    external_api_token_hash: str
    external_api_token_name: str
    external_api_allowed_origins: tuple[str, ...]
    external_api_rate_limit_per_minute: int

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.db_path.parent, self.tmp_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    root = Path(os.getenv("APP_ROOT", Path(__file__).resolve().parent.parent)).resolve()
    state_root = Path(os.getenv("MINIPACS_STATE_ROOT", root)).resolve()
    allowed = frozenset(
        item.strip().upper() for item in os.getenv("ALLOWED_CALLING_AE", "").split(",") if item.strip()
    )
    primary_ae = os.getenv("DICOM_AE_TITLE", "MINIPACS").strip().upper()
    extra_destination_aes = {
        item.strip().upper()
        for item in os.getenv("DICOM_EXTRA_AE_TITLES", "").split(",")
        if item.strip()
    }
    destination_aes = frozenset({primary_ae, *extra_destination_aes})
    for ae_title in destination_aes:
        if not ae_title or len(ae_title) > 16 or any(char not in string.printable or char.isspace() for char in ae_title):
            raise ValueError("DICOM AE Titles must be 1-16 printable non-space ASCII characters")
    return Settings(
        root=root,
        data_dir=state_root / "data",
        db_path=state_root / "db" / "minipacs.sqlite3",
        tmp_dir=state_root / "tmp",
        logs_dir=state_root / "logs",
        dicom_ae_title=primary_ae,
        dicom_destination_aes=destination_aes,
        dicom_port=int(os.getenv("DICOM_PORT", "11112")),
        allowed_calling_aes=allowed,
        retention_days=max(1, int(os.getenv("RETENTION_DAYS", "15"))),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password_hash=os.getenv("ADMIN_PASSWORD_HASH", ""),
        session_secret=os.getenv("SESSION_SECRET", "development-secret-change-me"),
        cookie_secure=_bool(os.getenv("COOKIE_SECURE", "false")),
        max_bulk_studies=max(1, int(os.getenv("MAX_BULK_STUDIES", "20"))),
        max_bulk_bytes=max(1, int(os.getenv("MAX_BULK_BYTES", str(20 * 1024**3)))),
        max_dicom_file_bytes=max(1, int(os.getenv("MAX_DICOM_FILE_BYTES", str(512 * 1024**2)))),
        external_api_token_hash=os.getenv("EXTERNAL_API_TOKEN_HASH", "").strip(),
        external_api_token_name=os.getenv("EXTERNAL_API_TOKEN_NAME", "external-api").strip()[:100] or "external-api",
        external_api_allowed_origins=tuple(
            origin.strip().rstrip("/") for origin in os.getenv("EXTERNAL_API_ALLOWED_ORIGINS", "").split(",") if origin.strip()
        ),
        external_api_rate_limit_per_minute=max(1, int(os.getenv("EXTERNAL_API_RATE_LIMIT_PER_MINUTE", "60"))),
    )
