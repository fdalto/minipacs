from __future__ import annotations

import re


UID_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*\Z")


def validate_uid(value: str) -> str:
    """Return a valid DICOM UID or raise before it can affect a filesystem path."""
    if not value or len(value) > 64 or not UID_PATTERN.fullmatch(value):
        raise ValueError("invalid DICOM UID")
    return value
