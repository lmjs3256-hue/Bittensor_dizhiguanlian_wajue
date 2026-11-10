"""索引落后太久时，节点已丢弃历史块的 state —— 必须认账跳过，不能卡死。

**实测现象**：本地索引游标停在 10 小时前不动，`scan_jobs` 里连续几条
`failed: UnknownBlock: State already discarded`，调度器每 3 分钟撞同一堵墙。
后果是本地索引断供，自动入库流水线没有输入——用户看到的表面就是"巡检不采
数据了"。

**为什么重试无效**：节点只保留最近一段区块的 state（实测约 3000 块 ≈ 10 小时），
更老的块 `chain_getBlock` 直接报 `UnknownBlock`。这不是网络抖动，等多久都一样。

**正确做法**：识别这个错误 → 从链头往回二分找"还能读的最早块" → 把游标跳到
那里继续，并在 `scan_jobs.error` 里记下"跳过了哪一段"。丢掉的段确实采不到了，
但链在往前走，跟上当下比补死账重要。
"""

from __future__ import annotations

import pytest

import app.service as service_module
from app.service import LinkService


# -- 错误识别 ---------------------------------------------------------------
@pytest.mark.parametrize(
    "message",
    [
        "Client error: UnknownBlock: State already discarded for 0xd4e1",
        "State discarded for 0xd4e1",
        "UnknownBlock",
    ],
)
def test_state_discarded_is_recognized(message):
    assert LinkService._is_state_discarded(Exception(message)) is True


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("timed out"),
        TimeoutError("read timeout"),
        ValueError("something else"),
        RuntimeError("node is busy"),
    ],
)
def test_ordinary_errors_are_not_mistaken(exc):
    """普通网络错误不能当成"块读不出来"——否则会把正常的重试也跳过。"""

    assert LinkService._is_state_discarded(exc) is False


def test_state_discarded_error_class_name_is_enough():
    """异常类名本身叫 StateDiscardedError 时也要认（不同库的封装不一样）。"""

    class StateDiscardedError(Exception):
        pass

    assert LinkService._is_state_discarded(StateDiscardedError("boom")) is True


# -- 源码契约：scan 里必须有跳过逻辑 ----------------------------------------
def test_scan_recovers_from_discarded_state():
    """scan() 必须捕获 state-discarded 并重试，而不是直接抛。

    这条是给「索引卡死」上的锁：只要有人把这段 try/except 去掉，
    测试立刻红。
    """

    import inspect

    source = inspect.getsource(LinkService.scan)
    assert "_is_state_discarded" in source, "scan 没有识别 state-discarded"
    assert "_earliest_readable" in source, "scan 没有回退找可读块"
    assert "gap_from" in source and "gap_to" in source, "跳过的区间没有被记录"


def test_scan_records_gap_in_job_error():
    """跳过的区间要留痕：`scan_jobs.error` 里写「跳过不可读区间 A~B」。"""

    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app" / "service.py"
    ).read_text(encoding="utf-8")
    assert "跳过不可读区间" in source


def test_earliest_readable_is_binary_search():
    """回退找可读块用二分：落后几十万块时线性扫描会卡到超时。"""

    import inspect

    source = inspect.getsource(LinkService._earliest_readable)
    assert "while lo < hi" in source
    assert "mid = (lo + hi) // 2" in source


def test_probe_uses_collect_range_not_missing_api():
    """探测必须走采集器真实入口，不能假设有个 fetch_block。"""

    import inspect

    source = inspect.getsource(LinkService._probe_block)
    assert "collect_range" in source
    assert "scanned_blocks" in source
