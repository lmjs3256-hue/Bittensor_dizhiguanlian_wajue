"""历史记录「查看结果」在隧道/远程访问下必须可用。

用户报障：用远程隧道连接，点「查看结果」**毫无反应**；同一台主机本地打开正常。

根因两条，都要防回归：

1. **返回体积过大**。实测一条 171 个关联地址的记录，payload 5.5MB 里
   5.2MB 是 ``edges``（10,932 条逐跳边，每条含 tx_hash / block_hash /
   timestamp / reason 等十多个字段），而 ``related`` 只有 282KB。
   经 Cloudflare 隧道下载 5.5MB 要 9~22 秒，前端又没有任何加载提示，
   用户看到的就是「点了没反应」。

2. **没有加载态**。`api()` 没有超时、请求会一直挂，期间按钮毫无变化。

修法：
- 后端在**返回时**精简 `edges`（库里 payload 保持原样，仍可完整导出）；
- 前端点按钮立刻变「读取中…」，3 秒后追加「仍在读取…」并提示隧道较慢。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

import app.service as service_module

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")


def _payload(edge_count: int = 40) -> dict:
    edges = [
        {
            "tx_hash": "0x" + "a" * 64,
            "block_hash": "0x" + "b" * 64,
            "block_number": 1000 + i,
            "from_address": "5From" + str(i),
            "to_address": "5To" + str(i),
            "amount_tao": "12.5",
            "timestamp": "2026-11-10T00:00:00",
            "edge_type": "tao_transfer",
            "reason": "某段很长的判定理由说明文字，用于放大体积",
            "netuid": None,
            "hotkey": "",
            "depth": 1,
            "related_address": "5Related",
        }
        for i in range(edge_count)
    ]
    return {
        "seed": "5Seed",
        "edges": edges,
        "related": [{"address": "5Related", "path_transfers": [{"from_address": "a", "to_address": "b"}]}],
        "warnings": [],
    }


# -- 后端精简 ---------------------------------------------------------------
def test_edges_are_slimmed_but_kept():
    payload = _payload()
    slim = service_module._slim_archive_payload(payload)

    # 条数不变（邻接计算依赖全部边）
    assert len(slim["edges"]) == len(payload["edges"])
    # 只留两端地址
    assert set(slim["edges"][0]) == {"from_address", "to_address"}
    assert slim["edges"][0]["from_address"] == "5From0"


def test_related_and_other_fields_untouched():
    payload = _payload()
    slim = service_module._slim_archive_payload(payload)
    assert slim["related"] == payload["related"]
    assert slim["seed"] == payload["seed"]
    assert slim["warnings"] == payload["warnings"]


def test_slim_reduces_size_significantly():
    payload = _payload(200)
    before = len(json.dumps(payload, ensure_ascii=False).encode())
    after = len(json.dumps(service_module._slim_archive_payload(payload), ensure_ascii=False).encode())
    assert after < before * 0.45, f"精简不足：{before} -> {after}"


def test_original_payload_in_db_is_not_modified():
    """精简只发生在返回时；库里必须保留完整 payload（导出还要用）。"""

    db = sqlite3.connect(":memory:")
    db.execute("create table t (payload text)")
    raw = json.dumps(_payload(5), ensure_ascii=False)
    db.execute("insert into t values (?)", (raw,))
    db.commit()

    loaded = json.loads(raw)
    service_module._slim_archive_payload(loaded)
    # 库里那条字符串一字未动
    assert db.execute("select payload from t").fetchone()[0] == raw


def test_payload_without_edges_is_returned_as_is():
    payload = {"seed": "5Seed", "related": []}
    assert service_module._slim_archive_payload(payload) == payload
    assert service_module._slim_archive_payload(None) is None


# -- 前端加载态 -------------------------------------------------------------
def _view_func() -> str:
    start = APP_JS.index("async function viewHistoryRecord(")
    end = APP_JS.index("\nasync function ", start + 10)
    return APP_JS[start:end]


def test_view_button_shows_loading_state():
    body = _view_func()
    assert "读取中…" in body
    assert "button.disabled = true" in body
    # finally 里要恢复，否则一次失败后按钮永久不可点
    assert "button.disabled = false" in body


def test_slow_transfer_hint_after_delay():
    body = _view_func()
    assert "仍在读取…" in body
    assert "3000" in body  # 3 秒后才提示，正常请求不会看到这句


def test_event_handler_passes_button():
    """不把按钮传进函数，加载态就无从设置。"""

    assert "viewHistoryRecord(historyView.dataset.historyView, historyView)" in APP_JS
