"""「目标地址库不存在」的回归测试（0.9.6）。

真实工单：用户在一台通过隧道访问的远端机器上查询 `5Ebftb…`，地址库里明明
有「110项目方」这个库（19 个库、117 条地址），点「同步全部关联地址」却报
「目标地址库不存在；要新建地址库请用『新建地址库并导入』」。

原因是**两套数据来源不一致**：

- 库清单（`address_books()`）把「台账 ``address_books`` 里的库名」和「地址行
  里出现过的 ``book`` 值」**并集**展示——所以界面、下拉框里都看得见这个库；
- 而同步校验 `sync_relation_addresses()` 只查**台账**。

远端机器的库是通过「备份还原 / 跨机同步包」带过来的：那条路径走
`import_rows()`，历史上只把 ``book`` 写进地址行、**从不登记台账**。于是库
只在地址行里活着，看着存在、一用就报不存在。本机因为当初是「新建地址库」
建的，台账行在，所以同一操作在本机是好的——这就是"远端电脑一样失败、
主机的库看着好好的"的由来。

修法两条，都在这份文件里锁住：

1. `import_rows()` 把导入过程中出现过的库名补进台账（根治来源）；
2. `sync_relation_addresses()` 遇到「台账缺行但地址确实挂着这个库」时就地
   补登记，不打断用户（已经分裂的机器自动愈合）；真正的空库名仍然报错。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import AddressBook, AddressLibrary, Database
from app.service import InputError, LinkService

SEED = "5EbftbT2P2JEEQMNRRPerAtNS7ccRto19nMCKggQpFTWkQ9r"
PEER = "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg"
OTHER = "5GHtTroZhAxDFaEKR8HAdQRfvzdwqSr3YkbPvAi3hLktSxsN"
BOOK = "110项目方"


@pytest.fixture()
def service(tmp_path):
    database = Database(tmp_path / "books.db")
    database.initialize()
    instance = LinkService(database, "wss://example.invalid")
    try:
        yield instance, database
    finally:
        database.engine.dispose()


def _book_row(database: Database, name: str) -> AddressLibrary:
    """造一条「库只存在于地址行、台账里没有」的脏状态。"""

    with database.session() as db:
        row = AddressLibrary(
            address=PEER,
            label=name,
            category="其他",
            note="",
            source="relation-sync",
            book=name,
        )
        db.add(row)
        db.flush()
        db.expunge(row)
    return row


def _registry_names(database: Database) -> set[str]:
    with database.session() as db:
        return set(db.scalars(select(AddressBook.name)).all())


def test_import_rows_registers_books_carried_by_addresses(service):
    """备份/同步包里的地址带着库名 → 导入后台账必须有这些库。

    这条是根因：远端机器就是这样把「110项目方」导进来的。
    """

    instance, database = service
    instance.import_rows(
        [
            {"address": PEER, "category": "其他", "book": BOOK},
            {"address": OTHER, "category": "其他", "book": "53项目方"},
        ],
        "wallet-link-backup.json",
    )
    assert _registry_names(database) == {BOOK, "53项目方"}


def test_import_rows_keeps_existing_registry_note(service):
    """台账已存在时不覆盖——备注是人工写的，导入不该抹掉。"""

    instance, database = service
    with database.session() as db:
        db.add(AddressBook(name=BOOK, note="2026-11 从群公告整理"))
    instance.import_rows([{"address": PEER, "category": "其他", "book": BOOK}], "b.json")
    with database.session() as db:
        row = db.scalar(select(AddressBook).where(AddressBook.name == BOOK))
        assert row is not None
        assert row.note == "2026-11 从群公告整理"


def test_sync_into_book_that_only_lives_in_address_rows(service):
    """核心回归：库在地址行里、台账缺行时，同步要成功而不是报「不存在」。"""

    instance, database = service
    _book_row(database, BOOK)
    assert _registry_names(database) == set(), "前提：台账确实是空的"

    result = instance.sync_relation_addresses(
        seed=SEED,
        addresses=[OTHER],
        include_seed=True,
        category="其他",
        book_name=BOOK,
    )
    # 发起地址 + 关联地址都进了这个库
    assert result["book"] == BOOK
    assert result["inserted"] == 2
    # 就地补登记，下次不用再愈合
    assert _registry_names(database) == {BOOK}
    # 原有的那条地址没被改动（book / 分类保持）
    with database.session() as db:
        row = db.scalar(select(AddressLibrary).where(AddressLibrary.address == PEER))
        assert row.book == BOOK
        assert row.category == "其他"


def test_sync_into_truly_unknown_book_still_errors(service):
    """地址行里也没有这个库 → 仍然是「不存在」，不能把错别字当成新库。"""

    instance, _database = service
    with pytest.raises(InputError) as excinfo:
        instance.sync_relation_addresses(
            seed=SEED,
            addresses=[OTHER],
            include_seed=True,
            category="其他",
            book_name="打错名字的库",
        )
    assert "目标地址库不存在" in str(excinfo.value)


def test_sync_self_heal_does_not_duplicate_registry(service):
    """愈合只补一次；重复同步不会把台账撑出多行（name 唯一）。"""

    instance, database = service
    _book_row(database, BOOK)
    for _ in range(3):
        instance.sync_relation_addresses(
            seed=SEED, addresses=[OTHER], include_seed=True, category="其他", book_name=BOOK
        )
    with database.session() as db:
        total = len(db.scalars(select(AddressBook)).all())
    assert total == 1
