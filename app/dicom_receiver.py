from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from pydicom.dataset import Dataset
from pynetdicom import AE, AllStoragePresentationContexts, evt
from pynetdicom.presentation import VerificationPresentationContexts

from .config import Settings, load_settings
from .db import initialize, store_instance
from .dicom_uid import validate_uid
from .logging_utils import configure_logging


def clean_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def extract_metadata(dataset: Dataset, source_ae: str, destination_ae: str = "") -> dict[str, str]:
    get = lambda key: clean_text(getattr(dataset, key, ""))
    return {
        "patient_id": get("PatientID"), "patient_name": get("PatientName"),
        "study_date": get("StudyDate"), "study_time": get("StudyTime"),
        "accession_number": get("AccessionNumber"), "study_description": get("StudyDescription"),
        "modality": get("Modality"), "study_instance_uid": get("StudyInstanceUID"),
        "series_instance_uid": get("SeriesInstanceUID"), "sop_instance_uid": get("SOPInstanceUID"),
        "instance_number": get("InstanceNumber"), "source_ae": source_ae.strip(),
        "destination_ae": destination_ae.strip().upper(),
    }


def _safe_uid(value: str) -> str:
    """Validate a DICOM UID before it is ever used as a filesystem component."""
    return validate_uid(value)


def build_handlers(settings: Settings):
    logger = configure_logging("dicom", settings)

    def handle_requested(event):
        calling = event.assoc.requestor.ae_title.strip().upper()
        called = event.assoc.requestor.primitive.called_ae_title.strip().upper()
        if called not in settings.dicom_destination_aes:
            logger.warning("Rejected DICOM association for unapproved Called AE: %s", called or "<empty>")
            event.assoc.reject(0x01, 0x01, 0x07)
        elif not settings.allowed_calling_aes or calling not in settings.allowed_calling_aes:
            logger.warning("Rejected DICOM association from unapproved Calling AE: %s", calling or "<empty>")
            event.assoc.reject(0x01, 0x01, 0x07)

    def handle_store(event):
        source_ae = event.assoc.requestor.ae_title.strip()
        destination_ae = event.assoc.requestor.primitive.called_ae_title.strip().upper()
        try:
            dataset = event.dataset
            # pynetdicom exposes the negotiated file meta separately; retain it on disk.
            dataset.file_meta = event.file_meta
            metadata = extract_metadata(dataset, source_ae, destination_ae)
            study_uid = _safe_uid(metadata["study_instance_uid"])
            series_uid = _safe_uid(metadata["series_instance_uid"])
            sop_uid = _safe_uid(metadata["sop_instance_uid"])
            study_dir = settings.data_dir / study_uid
            series_dir = study_dir / series_uid
            series_dir.mkdir(parents=True, exist_ok=True)
            target = series_dir / f"{sop_uid}.dcm"
            # Save first to a private temporary file. The database is only updated once a complete file exists.
            with tempfile.NamedTemporaryFile(dir=series_dir, prefix=".incoming-", suffix=".dcm", delete=False) as handle:
                temporary = Path(handle.name)
            try:
                dataset.save_as(temporary, write_like_original=True)
                file_size = temporary.stat().st_size
                if file_size > settings.max_dicom_file_bytes:
                    raise ValueError("DICOM instance exceeds configured size limit")
                relative = target.relative_to(settings.data_dir).as_posix()
                try:
                    # link() is exclusive: a concurrently received duplicate can never overwrite an existing file.
                    os.link(temporary, target)
                    created_target = True
                except FileExistsError:
                    created_target = False
                try:
                    added = store_instance(settings.db_path, metadata, relative, file_size, settings.retention_days) if created_target else False
                except Exception:
                    if created_target:
                        target.unlink(missing_ok=True)
                    raise
                temporary.unlink(missing_ok=True)
                if added:
                    logger.info("Stored DICOM SOP instance for study %s", study_uid)
                else:
                    if created_target:
                        target.unlink(missing_ok=True)
                    logger.info("Ignored duplicate SOP instance for study %s", study_uid)
                return 0x0000
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        except ValueError as exc:
            logger.warning("Rejected invalid C-STORE dataset: %s", exc)
            return 0xC210
        except OSError:
            logger.exception("Filesystem error while handling C-STORE")
            return 0xA700
        except Exception:
            logger.exception("Database or DICOM error while handling C-STORE")
            return 0xA700

    return [(evt.EVT_REQUESTED, handle_requested), (evt.EVT_C_STORE, handle_store)]


def run() -> None:
    settings = load_settings()
    settings.ensure_directories()
    initialize(settings.db_path)
    logger = configure_logging("dicom", settings)
    if not settings.allowed_calling_aes:
        logger.error("ALLOWED_CALLING_AE is empty; refusing to start an unauthenticated DICOM receiver")
        sys.exit(2)
    ae = AE(ae_title=settings.dicom_ae_title)
    # Several approved Called AE Titles share this TCP listener. Validation is
    # performed in EVT_REQUESTED against DICOM_AE_TITLE plus the extra titles.
    ae.require_called_aet = False
    ae.maximum_associations = 10
    ae.acse_timeout = 30
    ae.dimse_timeout = 120
    ae.network_timeout = 30
    for context in AllStoragePresentationContexts:
        ae.add_supported_context(context.abstract_syntax, context.transfer_syntax)
    for context in VerificationPresentationContexts:
        ae.add_supported_context(context.abstract_syntax, context.transfer_syntax)
    logger.info("DICOM SCP listening on 0.0.0.0:%s for Called AEs %s", settings.dicom_port, ", ".join(sorted(settings.dicom_destination_aes)))
    ae.start_server(("0.0.0.0", settings.dicom_port), block=True, evt_handlers=build_handlers(settings))


if __name__ == "__main__":
    run()
