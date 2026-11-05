"""外发预置地址库：app/seed_books/*.txt 第一次启动时装进已知地址库。"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from app.db import AddressBook, AddressLibrary, Database, install_seed_books
from app.service import LinkService

SEED_A = "5H8n3B5dV5z7BxnSehDHLqo7UnGsCxXXJVhenJMTiKZ93Dc4"
SEED_B = "5C51bmwMxk9TTswMj2wkiCvdAyWDo3wXw9i4Bcoqf93WjaHP"
SEED_C = "5C523K1nqhGWUrpvRtJxSCdiWLiRxR1RefjtcHbQc8fQ84hk"


def build_database(directory: str, name: str) -> Database:
    database = Database(Path(directory) / name)
    database.initialize()
    return database


def write_book(folder: Path, name: str, addresses: list[str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    body = "\n".join(addresses) + "\n"
    (folder / f"{name}.txt").write_text(body, encoding="utf-8")


def test_seed_books_install_each_file_as_one_label_group():
    with TemporaryDirectory() as directory:
        database = build_database(directory, "seed.db")
        folder = Path(directory) / "seed_books"
        write_book(folder, "一只羊", [SEED_A, SEED_B])
        write_book(folder, "DSV", [SEED_C])
        try:
            results = install_seed_books(database, folder)

            assert {item["name"] for item in results} == {"一只羊", "DSV"}
            assert sum(item["inserted"] for item in results) == 3
            with database.session() as db:
                rows = db.query(AddressLibrary).all()
                assert {row.address for row in rows} == {SEED_A, SEED_B, SEED_C}
                # 文件名就是标签组名，同时也是自建库名。
                assert {row.label for row in rows if row.address != SEED_C} == {"一只羊"}
                assert {row.book for row in rows} == {"一只羊", "DSV"}
                assert {book.name for book in db.query(AddressBook).all()} == {"一只羊", "DSV"}

            # 第二次启动不再重复导入（解散 / 手删后也不会被装回来）。
            assert install_seed_books(database, folder) == []
            with database.session() as db:
                assert db.query(AddressLibrary).count() == 3
        finally:
            database.engine.dispose()


def test_seed_books_skip_addresses_already_in_library():
    """已在地址库里的地址（人工维护过）保持原样，只补库里没有的。"""

    with TemporaryDirectory() as directory:
        database = build_database(directory, "seed-existing.db")
        service = LinkService(database, "ws://127.0.0.1:1")
        folder = Path(directory) / "seed_books"
        write_book(folder, "埋伏党", [SEED_A, SEED_B])
        try:
            service.create_address_book(
                name="我自己的库", seed="", addresses=[SEED_A], category="大V"
            )
            results = install_seed_books(database, folder)

            assert results[0]["inserted"] == 1
            assert results[0]["skipped"] == 1
            with database.session() as db:
                kept = db.query(AddressLibrary).filter_by(address=SEED_A).one()
                assert kept.book == "我自己的库"
                assert kept.category == "大V"
                added = db.query(AddressLibrary).filter_by(address=SEED_B).one()
                assert added.book == "埋伏党"
        finally:
            database.engine.dispose()


def test_seed_books_do_not_pollute_existing_same_name_book():
    """用户自己已经建了同名库时，预置文件不往里塞地址。"""

    with TemporaryDirectory() as directory:
        database = build_database(directory, "seed-conflict.db")
        service = LinkService(database, "ws://127.0.0.1:1")
        folder = Path(directory) / "seed_books"
        write_book(folder, "taoBot", [SEED_B])
        try:
            service.create_address_book(
                name="taoBot", seed="", addresses=[SEED_A], category="其他"
            )
            results = install_seed_books(database, folder)

            assert results[0]["inserted"] == 0
            with database.session() as db:
                rows = db.query(AddressLibrary).all()
                assert [row.address for row in rows] == [SEED_A]
        finally:
            database.engine.dispose()


def test_bundled_seed_books_are_parseable():
    """随包分发的文件本身要能读：每个文件一个组，地址都是合法 SS58。"""

    from app.db import SEED_BOOK_DIR, read_seed_addresses

    files = sorted(SEED_BOOK_DIR.glob("*.txt"))
    assert files, "外发包里应该带预置地址库文件"
    total = 0
    for path in files:
        addresses = read_seed_addresses(path)
        assert addresses, f"{path.name} 里没有可用地址"
        assert len(addresses) == len(set(addresses))
        total += len(addresses)
    assert total >= 300


def test_reinstall_seed_books_brings_back_dissolved_group(tmp_path, monkeypatch):
    """「重新导入预置库」：解散掉的组连库带地址补回来，已存在的地址不重复加。"""

    from app import db as db_module

    database = build_database(str(tmp_path), "seed-reinstall.db")
    folder = tmp_path / "seed_books"
    write_book(folder, "一只羊", [SEED_A, SEED_B])
    write_book(folder, "DFV", [SEED_C])
    monkeypatch.setattr(db_module, "SEED_BOOK_DIR", folder)
    service = LinkService(database, "ws://127.0.0.1:1")
    try:
        install_seed_books(database, folder)
        assert service.dissolve_address_book("一只羊")["deleted"] == 2

        result = service.reinstall_seed_books()

        assert result["inserted"] == 2
        assert result["created_books"] == ["一只羊"]
        assert {book["name"] for book in service.address_books()} == {"DFV", "一只羊"}
        with database.session() as db:
            assert db.query(AddressLibrary).count() == 3

        # 再点一次：已经补齐，没有缺失地址，不产生新的撤回快照。
        batch = service.last_import_batch()
        install_again = service.reinstall_seed_books()
        assert install_again["inserted"] == 0
        assert service.last_import_batch()["created_at"] == batch["created_at"]
    finally:
        database.engine.dispose()


def test_reinstall_seed_books_keeps_manual_edits_and_can_be_undone(tmp_path, monkeypatch):
    """重新导入只补缺失地址：人工改过的记录原样保留，导入结果可以撤回。"""

    from app import db as db_module

    database = build_database(str(tmp_path), "seed-reinstall-undo.db")
    folder = tmp_path / "seed_books"
    write_book(folder, "埋伏党", [SEED_A, SEED_B])
    monkeypatch.setattr(db_module, "SEED_BOOK_DIR", folder)
    service = LinkService(database, "ws://127.0.0.1:1")
    try:
        install_seed_books(database, folder)
        with database.session() as db:
            row = db.query(AddressLibrary).filter_by(address=SEED_A).one()
            row.category = "大V"
            row.note = "人工备注"
            kept_id = row.id
            # 手动删掉 SEED_B，模拟误删后想用预置库补回来。
            db.delete(db.query(AddressLibrary).filter_by(address=SEED_B).one())

        result = service.reinstall_seed_books()

        assert result["inserted"] == 1
        assert result["created_books"] == []
        with database.session() as db:
            kept = db.get(AddressLibrary, kept_id)
            assert kept.category == "大V" and kept.note == "人工备注"
            added = db.query(AddressLibrary).filter_by(address=SEED_B).one()
            assert added.book == "埋伏党"
        batch = service.last_import_batch()
        assert batch["kind"] == "seed"
        assert batch["inserted"] == 1

        undo = service.undo_last_import()

        assert undo["deleted"] == 1
        with database.session() as db:
            assert db.query(AddressLibrary).filter_by(address=SEED_B).first() is None
            assert db.get(AddressLibrary, kept_id).category == "大V"
    finally:
        database.engine.dispose()


def test_seed_book_status_counts_files():
    from app.db import seed_book_status

    status = seed_book_status()
    assert status["groups"] >= 1
    assert status["addresses"] >= 300


def test_seed_book_endpoints_report_and_reinstall(tmp_path, monkeypatch):
    """GET 概况 + POST 重新导入 两个接口。"""

    from fastapi.testclient import TestClient

    import app.main as main_module
    from app import db as db_module
    from app.jobs import RelationJobManager

    database = build_database(str(tmp_path), "seed-api.db")
    folder = tmp_path / "seed_books"
    write_book(folder, "taoBot", [SEED_A])
    monkeypatch.setattr(db_module, "SEED_BOOK_DIR", folder)
    service = LinkService(database, "ws://127.0.0.1:1")
    monkeypatch.setattr(main_module, "DATABASE", database)
    monkeypatch.setattr(main_module, "SERVICE", service)
    monkeypatch.setattr(main_module, "JOBS", RelationJobManager())
    try:
        with TestClient(main_module.app) as client:
            info = client.get("/api/library/seed-books").json()
            assert info == {"groups": 1, "addresses": 1}

            first = client.post("/api/library/seed-books/reinstall").json()
            assert first["inserted"] == 1
            assert first["created_books"] == ["taoBot"]

            again = client.post("/api/library/seed-books/reinstall").json()
            assert again["inserted"] == 0

            batch = client.get("/api/library/last-import").json()["item"]
            assert batch["kind"] == "seed"
            assert client.post("/api/library/undo-last-import").json()["deleted"] == 1
    finally:
        database.engine.dispose()
