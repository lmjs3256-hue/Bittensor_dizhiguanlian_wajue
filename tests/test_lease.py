"""Regression tests for scan lease and SS58 validation."""

import asyncio
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event
from tempfile import TemporaryDirectory

import pytest

from app.db import Database, ScanJob, ScanLock
from app.service import LinkService, ScanBusyError
from app.validation import is_valid_ss58


PUBLIC_RPC_ADDRESS = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"
RPC_URL = "wss://entrypoint-finney.opentensor.ai:443"


def install_fake_scan(monkeypatch, *, entered=None, release=None):
    import httpx
    import app.service as service_module

    class Response:
        def json(self):
            return {"result": {"number": "0x20"}}

    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: Response())

    class FakeCollector:
        def __init__(self, _url):
            pass

        async def collect_range(self, *, start, end, on_batch):
            if entered is not None:
                entered.set()
            if release is not None:
                await asyncio.to_thread(release.wait, 5)
                if not release.is_set():
                    raise TimeoutError("test scan was not released")
            await on_batch([], start, f"0x{start:x}")
            return {
                "head_block": 32,
                "start_block": start,
                "end_block": end,
                "scanned_blocks": 1,
                "transfer_count": 0,
                "seconds": 0,
            }

    monkeypatch.setattr(service_module, "RPCCollector", FakeCollector)


def test_public_ss58_address_is_valid():
    assert is_valid_ss58(PUBLIC_RPC_ADDRESS)
    assert not is_valid_ss58("A")
    assert not is_valid_ss58(PUBLIC_RPC_ADDRESS[:-1] + "x")


def test_active_database_lease_prevents_scan():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "lease.db")
        try:
            database.initialize()
            with database.session() as db:
                db.add(
                    ScanLock(
                        lock_name="chain-scan",
                        lease_until=datetime.now(timezone.utc) + timedelta(minutes=3),
                    )
                )
            service = LinkService(database, RPC_URL)
            with pytest.raises(ScanBusyError):
                service.scan(blocks=1)
        finally:
            database.engine.dispose()


def test_expired_lease_is_reclaimed_and_released(monkeypatch):
    install_fake_scan(monkeypatch)
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "lease.db")
        try:
            database.initialize()
            with database.session() as db:
                db.add(
                    ScanLock(
                        lock_name="chain-scan",
                        owner_job=999,
                        lease_until=datetime.now(timezone.utc) - timedelta(seconds=1),
                    )
                )

            result = LinkService(database, RPC_URL).scan(blocks=1)

            assert result["status"] == "complete"
            with database.session() as db:
                lock = db.get(ScanLock, "chain-scan")
                job = db.get(ScanJob, result["id"])
                assert lock is not None
                assert lock.owner_job is None
                assert lock.lease_until.replace(tzinfo=timezone.utc) <= datetime.now(
                    timezone.utc
                )
                assert job is not None and job.status == "complete"
        finally:
            database.engine.dispose()


def test_first_concurrent_scans_are_serialized(monkeypatch):
    scan_entered = Event()
    release_scan = Event()
    start_together = Barrier(2)
    install_fake_scan(monkeypatch, entered=scan_entered, release=release_scan)

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "lease.db")
        try:
            database.initialize()
            service = LinkService(database, RPC_URL)

            def run_scan():
                start_together.wait(timeout=5)
                return service.scan(blocks=1)

            with ThreadPoolExecutor(max_workers=2) as pool:
                scans = [pool.submit(run_scan) for _ in range(2)]
                assert scan_entered.wait(5), "neither scan reached the collector"
                completed, pending = wait(
                    scans, timeout=5, return_when=FIRST_COMPLETED
                )
                assert completed, "competing scan did not exit while lease was active"
                loser = next(iter(completed))
                with pytest.raises(ScanBusyError):
                    loser.result()
                release_scan.set()
                winner = next(future for future in pending)
                assert winner.result(timeout=5)["status"] == "complete"
        finally:
            release_scan.set()
            database.engine.dispose()
