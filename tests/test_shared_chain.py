from __future__ import annotations

import asyncio
import threading

import pytest

import app.collector as collector_module
import app.event_monitor.substrate_adapter as adapter_module
from app.collector import RPCCollector
from app.event_monitor.substrate_adapter import AsyncSubstrateAdapter
from app.shared_chain import (
    PRIORITY_EVENT,
    PRIORITY_NORMAL,
    SharedChainGateway,
    configure_shared_chain,
    shutdown_shared_chain,
)


class FakeClient:
    def __init__(self):
        self.init_count = 0

    async def initialize(self):
        self.init_count += 1

    async def close(self):
        return None


def test_event_priority_jumps_ahead_of_queued_address_scan():
    order: list[str] = []
    started = threading.Event()
    client = FakeClient()

    async def operation(name: str, delay: float = 0):
        order.append(name)
        if name == "normal-1":
            started.set()
        if delay:
            await asyncio.sleep(delay)

    gateway = SharedChainGateway("fake://chain", client_factory=lambda: client)
    try:
        first = gateway.submit(PRIORITY_NORMAL, lambda _: operation("normal-1", 0.15))
        assert started.wait(timeout=2)
        second = gateway.submit(PRIORITY_NORMAL, lambda _: operation("normal-2"))
        event = gateway.submit(PRIORITY_EVENT, lambda _: operation("event"))
        event.result(timeout=5)
        second.result(timeout=5)
        first.result(timeout=5)
        assert order == ["normal-1", "event", "normal-2"]
        assert client.init_count == 1
        stats = gateway.stats()
        assert stats["event"] == 1
        assert stats["normal"] == 2
    finally:
        gateway.close()


class FakeCollectorGateway:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    async def initialize(self, *, priority):
        self.calls.append(("initialize", priority))

    async def init_runtime(self, *, priority):
        self.calls.append(("init_runtime", priority))

    async def get_block_number(self, *, priority):
        self.calls.append(("head", priority))
        return 10

    async def get_block_with_events(self, block_number, *, priority):
        self.calls.append(("bundle", priority))
        block = {"header": {"hash": "0x" + "a" * 64}, "extrinsics": []}
        return block, [], "0x" + "a" * 64


def test_address_auto_import_uses_normal_priority_shared_gateway(monkeypatch):
    gateway = FakeCollectorGateway()
    monkeypatch.setattr(collector_module, "get_shared_chain", lambda url: gateway)
    batches = []

    async def on_batch(rows, block_number, block_hash):
        batches.append((rows, block_number, block_hash))

    result = asyncio.run(
        RPCCollector("fake://chain").collect_range(start=10, end=10, on_batch=on_batch)
    )
    assert result["scanned_blocks"] == 1
    assert result["head_block"] == 10
    assert batches and batches[0][1] == 10
    assert gateway.calls == [
        ("initialize", PRIORITY_NORMAL),
        ("init_runtime", PRIORITY_NORMAL),
        ("head", PRIORITY_NORMAL),
        ("bundle", PRIORITY_NORMAL),
    ]


class PriorityRecorder:
    def __init__(self):
        self.calls = []

    def rpc_request_sync(self, method, params, *, priority):
        self.calls.append((method, priority))
        return {"result": "0x1"}


def test_event_adapter_uses_shared_gateway_event_priority(monkeypatch):
    gateway = PriorityRecorder()
    monkeypatch.setattr(adapter_module, "get_shared_chain", lambda url: gateway)
    adapter = AsyncSubstrateAdapter("fake://chain")
    assert adapter.rpc_request("chain_getHeader", []) == {"result": "0x1"}
    assert gateway.calls == [("chain_getHeader", PRIORITY_EVENT)]
    adapter.close()
