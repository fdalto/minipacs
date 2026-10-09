from __future__ import annotations

import threading

import pytest

from app.zip_queue import ZipBuildQueue, ZipQueueTimeout


def test_zip_queue_runs_jobs_in_fifo_order() -> None:
    queue = ZipBuildQueue(1)
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def first_job() -> None:
        with queue.slot(1):
            first_started.set()
            assert release_first.wait(1)

    def second_job() -> None:
        with queue.slot(1):
            second_started.set()

    first = threading.Thread(target=first_job)
    second = threading.Thread(target=second_job)
    first.start()
    assert first_started.wait(1)
    second.start()
    assert not second_started.wait(0.05)
    release_first.set()
    first.join(1)
    second.join(1)
    assert second_started.is_set()


def test_zip_queue_times_out_when_no_slot_is_available() -> None:
    queue = ZipBuildQueue(1)
    with queue.slot(1), pytest.raises(ZipQueueTimeout):
        with queue.slot(0.01):
            pass
