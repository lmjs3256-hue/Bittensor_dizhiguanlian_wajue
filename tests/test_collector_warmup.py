"""扫描预热超时的回归测试。

背景（2026-11 实际故障）：`init_runtime()` 冷启动要从链上拉 runtime
WASM/metadata，实测 25~72 秒；而 `collect_range()` 用同一个 25 秒超时去
等它，导致每次扫描都在读第一块之前就 TimeoutError，界面只显示
`RPC scan failed:`（TimeoutError 的 str() 是空串），看上去像"扫描功能坏了"。

这里用假 substrate 固化两点：
1. runtime 预热走的是宽超时（warmup_timeout_seconds），不是单块超时；
2. 结果里带 warmup_seconds，便于诊断。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import collector as collector_module
from app.collector import RPCCollector


class _FakeSubstrate:
    """模拟 async_substrate_interface.AsyncSubstrateInterface 的最小行为。"""

    def __init__(self, url: str, *, warmup_delay: float, block_delay: float = 0.0) -> None:
        self.url = url
        self.warmup_delay = warmup_delay
        self.block_delay = block_delay
        self.closed = False

    async def initialize(self) -> None:
        await asyncio.sleep(0)

    async def init_runtime(self):
        # 真实环境里这一步要几十秒，正是它撞上了单块 25 秒超时。
        await asyncio.sleep(self.warmup_delay)
        return object()

    async def get_block_number(self) -> int:
        return 100

    async def get_block(self, block_number: int):
        if self.block_delay:
            await asyncio.sleep(self.block_delay)
        return {"header": {"hash": f"0x{block_number:064x}"}, "extrinsics": []}

    async def get_events(self, block_hash: str):
        return []

    async def close(self) -> None:
        self.closed = True


def _install(monkeypatch, fake_factory):
    monkeypatch.setattr(collector_module, "AsyncSubstrateInterface", fake_factory)


def test_slow_runtime_warmup_survives_tight_block_timeout(monkeypatch):
    """runtime 预热比单块超时慢，扫描也必须成功。"""

    def factory(url):
        return _FakeSubstrate(url, warmup_delay=0.35)

    _install(monkeypatch, factory)
    collector = RPCCollector(
        "wss://example.invalid",
        timeout_seconds=0.15,  # 故意比预热还短，模拟原来的 bug 条件
        warmup_timeout_seconds=3.0,
    )

    async def on_batch(rows, block_number, block_hash):
        return None

    result = asyncio.run(
        collector.collect_range(start=99, end=100, on_batch=on_batch)
    )

    assert result["scanned_blocks"] == 2
    assert result["start_block"] == 99
    assert result["end_block"] == 100
    # 预热耗时被单独记录，方便在扫描结果里诊断慢在哪个阶段。
    assert result["warmup_seconds"] >= 0.3


def test_warmup_timeout_still_applies(monkeypatch):
    """预热超时本身仍然生效，不能无限等下去。"""

    def factory(url):
        return _FakeSubstrate(url, warmup_delay=5.0)

    _install(monkeypatch, factory)
    collector = RPCCollector(
        "wss://example.invalid",
        timeout_seconds=10.0,
        warmup_timeout_seconds=0.2,
    )

    async def on_batch(rows, block_number, block_hash):
        return None

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(collector.collect_range(start=100, end=100, on_batch=on_batch))
    # 确实按 warmup 的 0.2 秒掐断，而不是等满 5 秒。
    assert time.monotonic() - started < 3.0


def test_substrate_closed_even_when_warmup_times_out(monkeypatch):
    """预热失败也必须关闭连接，避免连接泄漏拖垮后续扫描。"""

    holder: dict[str, _FakeSubstrate] = {}

    def factory(url):
        fake = _FakeSubstrate(url, warmup_delay=5.0)
        holder["fake"] = fake
        return fake

    _install(monkeypatch, factory)
    collector = RPCCollector(
        "wss://example.invalid", timeout_seconds=5.0, warmup_timeout_seconds=0.1
    )

    async def on_batch(rows, block_number, block_hash):
        return None

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(collector.collect_range(start=100, end=100, on_batch=on_batch))
    assert holder["fake"].closed is True
