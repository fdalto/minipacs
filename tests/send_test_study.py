#!/usr/bin/env python3
"""Send a completely synthetic DICOM study to a MiniPACS receiver."""
from __future__ import annotations

import argparse
from datetime import datetime
from uuid import uuid4

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
from pynetdicom import AE
from pynetdicom.sop_class import Verification


def dataset(study_uid: str, series_uid: str, number: int) -> Dataset:
    sop_uid = generate_uid()
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset(); ds.file_meta = meta
    ds.SOPClassUID = SecondaryCaptureImageStorage; ds.SOPInstanceUID = sop_uid
    ds.PatientName = "MINIPACS^TEST"; ds.PatientID = "SYNTHETIC-001"
    ds.StudyInstanceUID = study_uid; ds.SeriesInstanceUID = series_uid
    ds.StudyDate = datetime.now().strftime("%Y%m%d"); ds.StudyTime = datetime.now().strftime("%H%M%S")
    ds.StudyDescription = "Synthetic MiniPACS test"; ds.Modality = "OT"; ds.InstanceNumber = number
    ds.Rows = 2; ds.Columns = 2; ds.SamplesPerPixel = 1; ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8; ds.BitsStored = 8; ds.HighBit = 7; ds.PixelRepresentation = 0; ds.PixelData = bytes([0, 64, 128, 255])
    ds.is_little_endian = True; ds.is_implicit_VR = False
    return ds


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=11112)
    parser.add_argument("--calling-ae", default="TESTSCU"); parser.add_argument("--called-ae", default="MINIPACS")
    parser.add_argument("--images", type=int, default=3); args = parser.parse_args()
    ae = AE(ae_title=args.calling_ae); ae.add_requested_context(Verification)
    ae.add_requested_context(SecondaryCaptureImageStorage, ExplicitVRLittleEndian)
    assoc = ae.associate(args.host, args.port, ae_title=args.called_ae)
    if not assoc.is_established: raise SystemExit("Association rejected or unavailable")
    try:
        echo = assoc.send_c_echo(); print(f"C-ECHO status: 0x{echo.Status:04X}")
        study_uid, series_uid = generate_uid(), generate_uid()
        for number in range(1, args.images + 1):
            status = assoc.send_c_store(dataset(study_uid, series_uid, number))
            print(f"C-STORE {number}: 0x{status.Status:04X}")
        print(f"Synthetic StudyInstanceUID: {study_uid}")
    finally: assoc.release()


if __name__ == "__main__": main()
