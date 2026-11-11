"""查询记录按人隔离；地址库仍全机共享。

**用户要求**：「每个人只能单独看自己的查询记录，但每个人对地址库的维护都会
同步到主机，同一个地址不同人操作标记不一样，以最后标记的为主。」

两条规则要分开实现：

- **查询记录**（`relation_archives`）带 `owner`，列表默认只返回自己的。
  别人查了什么不该被看到——这是个心理边界，不是安全边界（大家都有访问口令）。
- **地址库**（`address_library`）**不分人**：谁改了都落到同一份数据上，
  后写的覆盖先写的。这正是用户要的"以最后标记的为主"。

一个关键兼容点：升级前的记录 `owner` 是空串，**对所有人可见**。否则用户
升级后会发现"我的历史记录全没了"。
"""

from __future__ import annotations

import pytest

from app.db import Database
from app.service import LinkService


@pytest.fixture()
def service(tmp_path):
    db = Database(tmp_path / "owner.db")
    db.initialize()
    yield LinkService(db, "wss://example.invalid")
    db.engine.dispose()


def _archive(service, seed: str, owner: str) -> int:
    return service.archive_relation(
        {"seed": seed, "related": [], "data_source": "local"},
        job_id=f"j-{owner or 'anon'}-{seed[:4]}",
        owner=owner,
    )


# -- 查询记录按人隔离 -------------------------------------------------------
def test_history_only_shows_my_records(service):
    _archive(service, "5AliceSeed", "张三")
    _archive(service, "5BobSeed", "李四")

    mine = service.relation_history(owner="张三")
    assert mine["total"] == 1
    assert mine["items"][0]["seed"].startswith("5Alice")


def test_history_without_owner_sees_everything(service):
    """不传 owner = 不区分人（单人使用、后台维护）。"""

    _archive(service, "5AliceSeed", "张三")
    _archive(service, "5BobSeed", "李四")

    everything = service.relation_history()
    assert everything["total"] == 2


def test_legacy_records_visible_to_everyone(service):
    """升级前的记录 owner 是空串，不能被藏起来。"""

    _archive(service, "5OldSeed", "")  # 老数据
    _archive(service, "5NewSeed", "李四")

    mine = service.relation_history(owner="张三")
    seeds = [item["seed"] for item in mine["items"]]
    assert any(seed.startswith("5Old") for seed in seeds), "老记录应可见"
    assert not any(seed.startswith("5New") for seed in seeds), "别人的新记录不该可见"


def test_history_total_counts_only_visible(service):
    """total 必须是过滤后的数量，否则分页会显示"共 N 条"却翻不到。"""

    for index in range(3):
        _archive(service, f"5Alice{index}", "张三")
    for index in range(5):
        _archive(service, f"5Bob{index}", "李四")

    mine = service.relation_history(owner="张三")
    assert mine["total"] == 3


# -- 地址库不分人 -----------------------------------------------------------
def test_library_is_shared_across_users(service):
    """地址库没有 owner 概念：谁改都落到同一份，后写覆盖先写。"""

    address = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"

    # 张三先标公共地址
    service.decide_public_address(address, True)
    row = service.relation_history  # 只是占位，下面直接查库
    with service.database.session() as db:
        from app.db import AddressLibrary

        current = db.query(AddressLibrary).filter_by(address=address).one()
        assert "PUBLIC" in service_module_categories(current)

    # 李四后标验证者：以最后操作为准
    service.decide_validator_address(address, True)
    with service.database.session() as db:
        from app.db import AddressLibrary

        current = db.query(AddressLibrary).filter_by(address=address).one()
        cats = service_module_categories(current)
        assert "VALIDATOR" in cats
        assert "PUBLIC" not in cats, "后标记的应覆盖先标记的（以最后为准）"


def service_module_categories(row) -> set[str]:
    import app.service as service_module

    return service_module.row_categories(row)


# -- 前端契约 ---------------------------------------------------------------
def test_frontend_sends_client_name_header():
    """请求要带使用者标识，且名字存在 localStorage 里。

    头部名字大小写不敏感（HTTP 本来就不区分），这里统一按小写比对。
    """

    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "x-client-name" in js.lower(), "请求要带使用者标识"
    assert "localStorage" in js
    # 名字要能被清空（清空后回到"不区分人"的行为）
    assert "setClientName" in js


def test_backend_reads_client_name_header():
    """后端从 `X-Client-Name` 读取使用者标识。"""

    from app import access

    assert access.OWNER_HEADER == "x-client-name"


def test_client_owner_sanitizes_input():
    """名字会进数据库、也会显示在界面上，要过滤不可打印字符并限长。"""

    from app.access import client_owner

    class _Req:
        def __init__(self, value):
            self.headers = {"x-client-name": value}

    assert client_owner(_Req("  张三  ")) == "张三"
    assert client_owner(_Req("")) == ""
    assert client_owner(_Req("a" * 100)) == "a" * 32
    # 换行等控制字符被去掉
    assert "\n" not in client_owner(_Req("张\n三"))
