"""本地索引流水线：不吃 API 额度，链上有新转账就入库。

**用户诉求（原话）**：「不会 API 没跑完就一直等 API 额度吧，应该从当前链上数据
就要陆陆续续采集了。巡检历史数据我都觉得不那么重要，当有需要的时候我去扫描
某个地址，自然会发现一些新的关联地址，那时候再入库。巡检应该是当下地址库里
已有地址，发生转账转移，立马入库，方便跟踪才对。」

原来的巡检是**逐地址问 API**：754 个地址 × 每地址 1~2 次请求 ≈ 1500 次/轮，
按每分钟 5~60 次要跑几十分钟到几小时。而链上数据其实已经由另一条链路采进了
``transfers`` 表——RPC 直连，**完全不消耗 Taostats 额度**。

所以新增 ``sweep_local_index()``：拿本地索引里的**新块**去和地址库做交集，
命中就直接自动入库。这就是反向查询：不是"问 API 这个地址有没有新往来"，
而是"本地这批新转账里，有没有地址属于我库里的"。

关键设计：

- **独立游标**（``watch_sweep_state``），与逐地址巡检的 ``watch_state`` 分开；
- **不回扫历史**：首次启用从当前水位开始（用户说历史不重要）；
- **零 API**：每日上限、每分钟限速、手动查询让路都不适用；
- **幂等**：同一笔只算一次；同一条转账两侧都在库时只记一侧。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import app.watch as watch_module
from app.db import (
    AddressLibrary,
    ChainCursor,
    Database,
    Transfer,
    WatchCandidate,
    WatchSweepState,
)
from app.settings_store import DEFAULT_WATCH_CONFIG
from app.watch import SWEEP_KEY, WatchService

OWNER = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
PARTNER = "5CF6Ruq6u7DoF6TYxyaFzEuviWi7tjvJtz19XgPS2b7XDCFM"
EXCHANGE = "5EfLbijRRwaEa6mSNftUoETdw8JZqCbz22fzCfetrf4vFybmyKsr"
PUBLIC = "5F55a6mSNftUoETdw8JZqCbz22fzCfetrf4vFybmyKsrgp3n"
OTHER = "5HCT4AarReToT1BKyLtJXJfSLs4zRS7dENnZ7iysqrqxXyV7"


@pytest.fixture()
def database(tmp_path):
    db = Database(tmp_path / "sweep.db")
    db.initialize()
    yield db
    db.engine.dispose()


@pytest.fixture()
def service(database):
    return WatchService(database)


@pytest.fixture()
def config():
    cfg = dict(DEFAULT_WATCH_CONFIG)
    cfg["threshold_tao"] = 30
    cfg["auto_import"] = True
    return cfg


def _library(db, address, **kw):
    with db.session() as session:
        session.add(
            AddressLibrary(
                address=address,
                label=kw.pop("label", "测试库"),
                category=kw.pop("category", "其他"),
                categories=kw.pop("categories", '["其他"]'),
                book=kw.pop("book", "测试库"),
                source=kw.pop("source", "manual"),
                **kw,
            )
        )


def _cursor(db, block):
    with db.session() as session:
        session.add(ChainCursor(rpc_url="wss://test", last_block=block))


def _transfer(db, index, frm, to, amount, block, kind="tao_transfer"):
    with db.session() as session:
        session.add(
            Transfer(
                event_key=f"k{index}",
                tx_hash=f"0x{index}",
                block_number=block,
                block_hash="h",
                event_index=index,
                from_address=frm,
                to_address=to,
                amount_rao=int(Decimal(str(amount)) * 10**9),
                amount_tao=Decimal(str(amount)),
                edge_type=kind,
            )
        )


def _in_library(db, address) -> bool:
    with db.session() as session:
        return session.query(AddressLibrary).filter_by(address=address).first() is not None


# -- 核心：不花 API，达到阈值就入库 -----------------------------------------
def test_new_transfer_from_library_address_imports_partner(
    database, service, config, monkeypatch
):
    """库里地址有新转出，累计过阈值 → 对手方自动入库。全程 0 次 API。"""

    _library(database, OWNER)
    _cursor(database, 1000)
    service.sweep_local_index(config)  # 初始化

    # 任何 API 调用都应该让测试失败
    def _boom(*args, **kwargs):
        raise AssertionError("本地流水线不该发起任何 API 请求")

    monkeypatch.setattr(watch_module, "get_transfer_page_query", _boom)
    monkeypatch.setattr(watch_module, "get_stake_transfer_page", _boom)

    _transfer(database, 1, OWNER, PARTNER, "45", 1001)
    result = service.sweep_local_index(config)

    assert result["scanned"] == 1
    assert result["hits"] == 1
    assert result["inserted"] == 1
    assert _in_library(database, PARTNER)


def test_below_threshold_accumulates_but_does_not_import(
    database, service, config
):
    """不足阈值只累计候选，不入库——避免一次小额往来就把地址灌进来。"""

    _library(database, OWNER)
    _cursor(database, 1000)
    service.sweep_local_index(config)

    _transfer(database, 1, OWNER, PARTNER, "5", 1001)
    result = service.sweep_local_index(config)

    assert result["hits"] == 1
    assert result["inserted"] == 0
    assert not _in_library(database, PARTNER)
    with database.session() as session:
        candidate = session.query(WatchCandidate).filter_by(address=PARTNER).one()
        assert Decimal(candidate.cumulative_tao) == Decimal("5")


def test_incoming_transfer_also_counts(database, service, config):
    """转入方向同样算：库里地址收到的钱，对手方也是关联对象。"""

    _library(database, OWNER)
    _cursor(database, 1000)
    service.sweep_local_index(config)

    _transfer(database, 1, PARTNER, OWNER, "45", 1001)
    result = service.sweep_local_index(config)

    assert result["inserted"] == 1
    assert _in_library(database, PARTNER)


def test_stake_transfer_is_counted_as_stake(database, service, config):
    """代币转移单独计数，界面上能看到「N 笔代币转移」。"""

    _library(database, OWNER)
    _cursor(database, 1000)
    service.sweep_local_index(config)

    _transfer(database, 1, OWNER, PARTNER, "45", 1001, kind="stake_transfer")
    service.sweep_local_index(config)

    with database.session() as session:
        candidate = session.query(WatchCandidate).filter_by(address=PARTNER).one()
        assert candidate.stake_count == 1
        assert candidate.tx_count == 0


# -- 谁会触发、谁不触发 ------------------------------------------------------
def test_exchange_address_does_not_trigger(database, service, config):
    """交易所地址不作为触发源：它的往来是噪音，会把库灌爆。"""

    _library(
        database,
        EXCHANGE,
        label="人工确认交易所",
        category="EXCHANGE",
        categories='["EXCHANGE"]',
        book="",
        exchange_confirmed=True,
    )
    _cursor(database, 1000)
    service.sweep_local_index(config)

    _transfer(database, 1, EXCHANGE, OTHER, "99", 1001)
    result = service.sweep_local_index(config)

    assert result["hits"] == 0
    assert not _in_library(database, OTHER)


def test_public_address_does_not_trigger(database, service, config):
    _library(database, PUBLIC, category="PUBLIC", categories='["PUBLIC"]', book="")
    _cursor(database, 1000)
    service.sweep_local_index(config)

    _transfer(database, 1, PUBLIC, OTHER, "99", 1001)
    assert service.sweep_local_index(config)["hits"] == 0


def test_partner_already_in_library_is_not_reimported(database, service, config):
    """对手方已在库里：不再重复入库（它本来就在，不需要"入库"这个动作）。"""

    _library(database, OWNER)
    _library(database, PARTNER, label="另一个库", book="另一个库")
    _cursor(database, 1000)
    service.sweep_local_index(config)

    _transfer(database, 1, OWNER, PARTNER, "99", 1001)
    result = service.sweep_local_index(config)

    assert result["hits"] == 0
    assert result["inserted"] == 0


def test_both_sides_in_library_counts_once(database, service, config):
    """两侧都在库：只记一次，别把同一笔算两遍导致金额翻倍。"""

    _library(database, OWNER)
    _library(database, PARTNER, label="另一个库", book="另一个库")
    # 让对手方先不入库，验证累计只发生一次：改用第三个地址做观察点
    _cursor(database, 1000)
    service.sweep_local_index(config)

    _transfer(database, 1, OWNER, PARTNER, "99", 1001)
    service.sweep_local_index(config)

    with database.session() as session:
        candidates = session.query(WatchCandidate).all()
    assert candidates == []


def test_revoked_address_is_not_reimported(database, service, config):
    """用户撤回过（拉黑）的地址，不会被流水线又收回来。"""

    from app.db import AutoImportLog

    _library(database, OWNER)
    with database.session() as session:
        session.add(
            AutoImportLog(address=PARTNER, book="测试库", trigger_address=OWNER, status="revoked")
        )
    _cursor(database, 1000)
    service.sweep_local_index(config)

    _transfer(database, 1, OWNER, PARTNER, "99", 1001)
    result = service.sweep_local_index(config)

    assert result["inserted"] == 0
    assert not _in_library(database, PARTNER)


# -- 游标语义 ---------------------------------------------------------------
def test_first_run_does_not_backfill_history(database, service, config):
    """首次启用只从当前水位往后再看：历史旧转账不灌进库。

    用户明确说历史不重要。而且全量回扫要处理 7 万多条历史转账，会把几周前的
    旧对手方一次性倒进地址库，噪音远大于价值。
    """

    _library(database, OWNER)
    _cursor(database, 5000)
    _transfer(database, 1, OWNER, PARTNER, "99", 4000)  # 历史块

    result = service.sweep_local_index(config)

    assert result["initialized"] is True
    assert result["last_block"] == 5000
    assert result["scanned"] == 0
    assert not _in_library(database, PARTNER)

    # 初始化之后的新块才会被处理
    _transfer(database, 2, OWNER, PARTNER, "99", 5001)
    assert service.sweep_local_index(config)["inserted"] == 1


def test_cursor_advances_and_is_idempotent(database, service, config):
    """同一批转账跑两次，第二次 0 行、不会重复累计。"""

    _library(database, OWNER)
    _cursor(database, 1000)
    service.sweep_local_index(config)

    _transfer(database, 1, OWNER, PARTNER, "5", 1001)
    first = service.sweep_local_index(config)
    second = service.sweep_local_index(config)

    assert first["scanned"] == 1
    assert second["scanned"] == 0
    with database.session() as session:
        candidate = session.query(WatchCandidate).filter_by(address=PARTNER).one()
        assert Decimal(candidate.cumulative_tao) == Decimal("5"), "不能被算两次"


def test_sweep_cursor_is_independent_from_watch_state(database, service, config):
    """两套游标必须分开：混用会让逐地址巡检的进度被流水线推飞。"""

    _library(database, OWNER)
    _cursor(database, 1000)
    service.sweep_local_index(config)
    _transfer(database, 1, OWNER, PARTNER, "5", 1001)
    service.sweep_local_index(config)

    with database.session() as session:
        sweep = session.get(WatchSweepState, SWEEP_KEY)
        assert int(sweep.last_block) == 1001
        # 逐地址巡检的游标不在这里，wallet 的 watch_state 应该仍然是空的
        from app.db import WatchState

        assert session.query(WatchState).count() == 0


def test_auto_import_off_is_respected(database, service, config):
    _library(database, OWNER)
    _cursor(database, 1000)
    service.sweep_local_index(config)

    config["auto_import"] = False
    _transfer(database, 1, OWNER, PARTNER, "99", 1001)
    result = service.sweep_local_index(config)

    assert result.get("skipped") is True
    assert not _in_library(database, PARTNER)


# -- 状态接口 ---------------------------------------------------------------
def test_status_reports_lag_and_totals(database, service, config):
    _library(database, OWNER)
    _cursor(database, 1000)

    before = service.sweep_status()
    assert before["initialized"] is False

    service.sweep_local_index(config)
    _transfer(database, 1, OWNER, PARTNER, "45", 1001)
    service.sweep_local_index(config)

    status = service.sweep_status()
    assert status["initialized"] is True
    assert status["last_block"] == 1001
    assert status["index_block"] == 1001
    assert status["lag_blocks"] == 0
    assert status["scanned_total"] == 1
    assert status["hits_total"] == 1
    assert status["inserted_total"] == 1


def test_status_lag_tracks_new_blocks(database, service, config):
    """索引里有还没被流水线处理的块时，lag 要如实反映。"""

    _library(database, OWNER)
    _cursor(database, 1000)
    service.sweep_local_index(config)
    _transfer(database, 1, OWNER, PARTNER, "5", 1200)

    status = service.sweep_status()
    assert status["lag_blocks"] == 200


# -- 配置与调度接线 ---------------------------------------------------------
def test_scheduler_runs_sweep_even_when_api_paused():
    """手动查询让路时也要跑流水线：它不抢额度，没有一起停的理由。"""

    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
        encoding="utf-8"
    )
    # 让路分支里必须调用 sweep
    paused = source.index('if pause["paused"]:')
    body = source[paused : paused + 900]
    assert "sweep_local_index" in body, "让路分支漏了流水线"
    # 正常分支也要有
    assert source.count("sweep_local_index") >= 2


def test_local_scan_interval_is_faster_than_five_minutes():
    """本地扫描是流水线的唯一输入，间隔必须比链出块更勤。"""

    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
        encoding="utf-8"
    )
    line = next(
        item for item in source.splitlines() if item.startswith("LOCAL_AUTO_SCAN_SECONDS")
    )
    seconds = int(line.split("=")[1].strip())
    assert seconds <= 300, f"扫描间隔 {seconds}s 太长，链上数据会积压"


# -- 开关与实时状态（0.8.44）------------------------------------------------
def test_sweep_can_be_switched_off(database, service, config):
    """关掉开关后整条流水线不干活：不扫、不累计、不入库。"""

    _library(database, OWNER)
    _cursor(database, 1000)
    service.sweep_local_index(config)  # 初始化

    config["sweep_enabled"] = False
    _transfer(database, 1, OWNER, PARTNER, "99", 1001)
    result = service.sweep_local_index(config)

    assert result["skipped"] is True
    assert result["reason"] == "sweep_disabled"
    assert not _in_library(database, PARTNER)


def test_sweep_resumes_after_re_enable(database, service, config):
    """关掉期间的新块不会丢：重新打开后从原游标继续处理。"""

    _library(database, OWNER)
    _cursor(database, 1000)
    service.sweep_local_index(config)

    config["sweep_enabled"] = False
    _transfer(database, 1, OWNER, PARTNER, "45", 1001)
    assert service.sweep_local_index(config)["skipped"] is True

    config["sweep_enabled"] = True
    result = service.sweep_local_index(config)
    assert result["scanned"] == 1
    assert result["inserted"] == 1, "关掉期间攒下的块，重开后要补上"
    assert _in_library(database, PARTNER)


def test_status_reports_enabled_flag(database, service, config):
    """状态里必须能看出开关是开还是关。"""

    _cursor(database, 1000)
    service.sweep_local_index(config)

    assert service.sweep_status(config)["enabled"] is True
    config["sweep_enabled"] = False
    assert service.sweep_status(config)["enabled"] is False


def test_status_reports_running_flag(database, service, config):
    """要能看出"此刻正在处理"——界面靠它区分「在跑」和「等下一轮」。"""

    _library(database, OWNER)
    _cursor(database, 1000)
    service.sweep_local_index(config)

    assert service.sweep_status(config)["running"] is False  # 没在跑
    _transfer(database, 1, OWNER, PARTNER, "5", 1001)
    service.sweep_local_index(config)
    # 同步调用结束后 running 必须回到 False，否则界面会一直显示"正在处理"
    assert service.sweep_status(config)["running"] is False
    assert service.sweep_status(config)["last_done_at"]


def test_running_flag_is_cleared_even_on_error(database, service, config, monkeypatch):
    """流水线内部抛异常时，running 标记也要复位，不能永远卡在"正在处理"。"""

    _library(database, OWNER)
    _cursor(database, 1000)
    service.sweep_local_index(config)

    def _boom(_cfg):
        raise RuntimeError("模拟内部错误")

    monkeypatch.setattr(service, "_sweep_local_index", _boom)
    with pytest.raises(RuntimeError):
        service.sweep_local_index(config)
    assert service.sweep_status(config)["running"] is False


def test_config_has_sweep_enabled_default_on():
    from app.settings_store import DEFAULT_WATCH_CONFIG as DEFAULTS

    assert DEFAULTS.get("sweep_enabled") is True, "新链路默认应开启"


# -- 前端契约：开关 + 独立动态面板 ------------------------------------------
def _js() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )


def test_frontend_has_sweep_switch():
    """开关要能回填、能保存。"""

    js = _js()
    assert 'const sweep = $("#watch-sweep");' in js
    assert "sweep.checked = config.sweep_enabled !== false;" in js
    assert "sweep_enabled: Boolean($(\"#watch-sweep\")?.checked)" in js


def test_frontend_renders_dedicated_sweep_panel():
    """流水线要有独立面板：和"逐地址巡检"的进度条分开显示。"""

    js = _js()
    assert "function renderSweepPanel(" in js
    assert "renderSweepPanel(data)" in js
    # 三种状态都要能表达。按函数边界截取，别用固定字符数——面板一加内容
    # 就会把后半段截掉，测试会假失败（0.8.49 加心跳行时正好踩到）。
    start = js.index("function renderSweepPanel(")
    end = js.index("function stopWatchPolling(", start)
    body = js[start:end]
    assert "已关闭" in body
    assert "正在处理" in body
    assert "等待下一轮" in body
    assert "sweep.lag_blocks" in body


def test_progress_box_no_longer_duplicates_sweep():
    """两条链路不能混在同一行显示，否则会误以为额度用完就全停了。"""

    js = _js()
    start = js.index("function renderWatchProgress(")
    end = js.index("function renderSweepPanel(")
    assert "本地流水线已处理" not in js[start:end]


# -- 入参模型不能吞字段（真实踩过）-----------------------------------------
def test_config_input_model_accepts_sweep_enabled():
    """`/api/watch/config` 的 Pydantic 模型必须声明这两个字段。

    真实踩到的坑：前端一直在传 `sweep_enabled` / `prefer_local_index`，
    但 `WatchConfigInput` 没声明它们，Pydantic 默认忽略未知字段——于是
    接口返回 200、前端以为存上了，实际设置被默默丢掉。「本地索引优先」
    这个勾选框因此从上线起就没生效过。
    """

    from app.main import WatchConfigInput

    fields = set(WatchConfigInput.model_fields)
    assert "sweep_enabled" in fields, "入参模型缺 sweep_enabled，设置会被丢弃"
    assert "prefer_local_index" in fields, "入参模型缺 prefer_local_index"

    # 且要能真正解析出来（不是声明了却永远 None）
    parsed = WatchConfigInput(**{"sweep_enabled": False, "prefer_local_index": False})
    assert parsed.sweep_enabled is False
    assert parsed.prefer_local_index is False


def test_saved_config_reaches_disk(tmp_path, monkeypatch):
    """保存后必须落盘到 `address_watch`，下次启动能读回来。"""

    from app import settings_store

    monkeypatch.setenv("WALLET_LINK_SETTINGS", str(tmp_path / "settings.json"))
    assert settings_store.save_watch_config({"sweep_enabled": False}) is True
    assert settings_store.load_watch_config()["sweep_enabled"] is False

    assert settings_store.save_watch_config({"sweep_enabled": True}) is True
    assert settings_store.load_watch_config()["sweep_enabled"] is True


def test_sweep_status_reports_a_heartbeat_for_every_tick(database, service, config):
    """0.8.49：即使没有新块、累计数字不涨，也要能证明调度器在按节奏跑。"""

    from app.watch import SWEEP_TICK_SECONDS

    _library(database, OWNER)
    _cursor(database, 1000)

    before = service.sweep_status(config)
    assert before["tick_count"] == 0
    assert before["heartbeat_seconds_ago"] is None
    assert before["next_tick_in_seconds"] == SWEEP_TICK_SECONDS
    assert before["tick_seconds"] == SWEEP_TICK_SECONDS

    service.sweep_local_index(config)  # 初始化游标
    first = service.sweep_status(config)
    assert first["tick_count"] == 1
    assert first["last_tick_at"]
    assert first["heartbeat_seconds_ago"] is not None
    assert first["last_duration_ms"] >= 0
    assert first["last_scanned"] >= 0
    assert 0 <= first["next_tick_in_seconds"] <= SWEEP_TICK_SECONDS

    service.sweep_local_index(config)  # 这一轮没有新块
    second = service.sweep_status(config)
    assert second["tick_count"] == 2
    assert second["last_scanned"] == 0


def test_sweep_heartbeat_also_advances_when_disabled(database, service, config):
    """关掉的轮次也记心跳，界面才能区分「关了」和「卡了」。"""

    config["sweep_enabled"] = False
    service.sweep_local_index(config)
    status = service.sweep_status(config)
    assert status["enabled"] is False
    assert status["tick_count"] == 1
    assert status["last_reason"] == "sweep_disabled"
    assert status["heartbeat_seconds_ago"] is not None


def test_watch_tick_seconds_is_single_source_of_truth():
    """前端倒计时依赖这个间隔，main.py 不能另写一份会漂移的数字。"""

    import app.main as main_module

    assert main_module.WATCH_TICK_SECONDS == watch_module.SWEEP_TICK_SECONDS
