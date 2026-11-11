"""本地索引流水线「看得见在动」的回归测试（0.8.49）。

用户反馈：地址库页的流水线面板完全静止，看不出有没有在工作。
根因是两个，缺一不可：

1. 调度器 30 秒一轮，单轮常常不到 1 秒，而前端 5 秒才轮询一次，
   `running=true` 基本抓不到；一旦追上进度，累计数字也不再变，
   整块面板就彻底死住。
2. 模板里没有倒计时 / 进度条 / 心跳行，后端就算给了字段也没地方显示。

所以这组测试同时盯三处：后端字段、模板元素、样式与轮询频率。
少了任何一处都会退回「静止面板」。
"""

from pathlib import Path

from app.db import Database
from app.watch import WatchService

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")

# 界面靠这几个字段证明「调度器在按节奏跑」，缺一个面板就只能靠累计数字。
LIVE_FIELDS = (
    "last_tick_at",
    "tick_count",
    "last_duration_ms",
    "last_scanned",
    "heartbeat_seconds_ago",
    "next_tick_in_seconds",
    "tick_seconds",
)


def test_sweep_status_payload_carries_every_live_field(tmp_path):
    """`/api/watch/status` 的 sweep 段必须自带心跳，前端不猜。"""

    db = Database(tmp_path / "live.db")
    db.initialize()
    try:
        sweep = WatchService(db).status()["sweep"]
    finally:
        db.engine.dispose()

    missing = [name for name in LIVE_FIELDS if name not in sweep]
    assert not missing, f"缺少心跳字段：{missing}"


def test_panel_exposes_countdown_activity_bar_and_heartbeat_line():
    """模板里得有承载动态的三处元素，否则字段再多也看不见。"""

    assert 'id="sweep-countdown"' in TEMPLATE
    assert 'id="sweep-activity-bar"' in TEMPLATE
    assert 'id="sweep-pulse"' in TEMPLATE


def test_styles_keep_the_panel_moving_between_rounds():
    """两轮之间也要有动静：进度条按秒推进，活着时轨道有流动感。"""

    assert ".sweep-activity-bar" in CSS
    assert ".sweep-panel.is-alive" in CSS
    assert "@keyframes sweepFlow" in CSS


def test_frontend_runs_a_local_ticker_instead_of_waiting_for_polling():
    """倒计时每秒本地推进，不能靠接口轮询撑起来——否则只会一秒一跳。"""

    assert "state.sweepNextAt" in JS
    assert "setInterval(updateSweepTicker, 1000)" in JS
    assert "heartbeat_seconds_ago" in JS
    assert "tick_count" in JS


def test_polling_is_faster_than_one_pipeline_round():
    """30 秒一轮、单轮可能瞬间结束，5 秒轮询根本抓不到 running。"""

    assert "scheduleWatchPolling(3000)" in JS
    assert "scheduleWatchPolling(5000)" not in JS


def test_ticker_stops_when_leaving_the_library_view():
    """本地定时器不能留在别的页面上空转。"""

    assert "stopSweepTicker();" in JS
