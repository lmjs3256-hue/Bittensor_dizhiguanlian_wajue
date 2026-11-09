"""手动「立即巡检一轮」这条入口的回归测试。

背景：``WatchService.run()` 的参数是 keyword-only，而 ``_watch_manual_run``
曾经按位置传 ``WATCH.run, True, limit, None``——每点一次都抛
``TypeError: takes 1 positional argument but 4 were given``，被 except 吞成
一行「手动巡检失败」日志。界面上看不出任何异常，只是永远不动。

这里直接调用真实协程，把「参数签名」钉死；再验证手动推进量的边界。
"""

from __future__ import annotations

import asyncio

import pytest

import app.watch as watch_module
from app.db import AddressLibrary, Database, WatchState
from app.watch import WatchService


def _build(db: Database, address: str, book: str = "6NCK") -> None:
    with db.session() as session:
        session.add(
            AddressLibrary(
                address=address,
                label="",
                category="其他",
                note="",
                source="manual",
                book=book,
                categories="[]",
            )
        )


@pytest.fixture()
def database(tmp_path):
    db = Database(tmp_path / "manual.db")
    db.initialize()
    yield db
    db.engine.dispose()


def test_manual_run_entry_does_not_raise_typeerror(monkeypatch, database):
    """手动入口必须能真的调通 run()，不能只留一行失败日志。"""

    import app.main as main_module

    addresses = [f"5Manual{index}".ljust(48, "m") for index in range(3)]
    for address in addresses:
        _build(database, address)

    calls: list[dict] = []

    def fake_run(*, manual=False, max_addresses=None, config=None):
        calls.append(
            {"manual": manual, "max_addresses": max_addresses, "config": config}
        )
        return {"status": "running", "addresses_checked": 0}

    # 把 main 里引用的 WATCH 换成不碰网络的替身，只验证调用签名与限额。
    class FakeWatch:
        addresses_per_tick = staticmethod(lambda config=None: 4)
        run = staticmethod(fake_run)

    class FakeLock:
        def locked(self):
            return False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(main_module, "WATCH", FakeWatch())
    monkeypatch.setattr(main_module, "WATCH_LOCK", FakeLock())

    asyncio.run(main_module._watch_manual_run())

    assert len(calls) == 1, "手动入口没有真正调用 run()"
    assert calls[0]["manual"] is True
    # 只推进一小段（每分钟额度能覆盖的量），不占锁几小时
    assert calls[0]["max_addresses"] == 8


def test_manual_run_is_bounded_not_whole_library(monkeypatch, database):
    """手动点一次不能把整轮跑完：limit 必须存在且远小于库规模。"""

    import app.main as main_module

    for index in range(50):
        _build(database, f"5Bound{index}".ljust(48, "n"))

    captured = {}

    def fake_run(*, manual=False, max_addresses=None, config=None):
        captured["max_addresses"] = max_addresses
        return {"status": "running"}

    class FakeWatch:
        addresses_per_tick = staticmethod(lambda config=None: 10)
        run = staticmethod(fake_run)

    class FakeLock:
        def locked(self):
            return False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(main_module, "WATCH", FakeWatch())
    monkeypatch.setattr(main_module, "WATCH_LOCK", FakeLock())

    asyncio.run(main_module._watch_manual_run())

    assert captured["max_addresses"] is not None
    assert captured["max_addresses"] <= 20
    assert captured["max_addresses"] < 50


def test_cursor_survives_interruption(database, monkeypatch):
    """断点续跑的依据是 watch_state 游标；它必须落库、重启后仍在。"""

    address = "5CursorTest".ljust(48, "c")
    _build(database, address)

    with database.session() as session:
        session.add(WatchState(address=address, last_block=1234, checked_count=1))
        session.flush()

    # 新建一个 Database 句柄，等价于「服务重启后再读」
    reopened = Database(database.path)
    service = WatchService(reopened)
    with reopened.session() as session:
        state = session.get(WatchState, address)
        assert state is not None
        assert state.last_block == 1234
        assert state.checked_count == 1
    reopened.engine.dispose()
