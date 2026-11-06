"""时间显示：库里读出的 naive UTC 必须标注时区，前端才能换算成北京时间。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.db import Database, iso_utc
from app.service import LinkService


SEED = "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"


@pytest.fixture()
def service():
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "time.db")
        database.initialize()
        try:
            yield LinkService(database, "wss://example.invalid")
        finally:
            # Windows 上不关连接，临时目录就删不掉。
            database.engine.dispose()


def test_iso_utc_marks_naive_datetime_as_utc():
    naive = datetime(2026, 11, 5, 8, 30, 15, 370205)
    assert iso_utc(naive) == "2026-11-05T08:30:15.370205+00:00"


def test_iso_utc_keeps_aware_value():
    aware = datetime(2026, 11, 5, 8, 30, 15, tzinfo=timezone.utc)
    assert iso_utc(aware) == "2026-11-05T08:30:15+00:00"
    assert iso_utc(None) is None


def test_archive_summary_time_has_timezone_offset(service):
    archive_id = service.archive_relation(
        {"seed": SEED, "related": []},
        kind="single",
        status="complete",
        rounds=1,
        requests=0,
    )
    item = service.relation_history(limit=1, offset=0)["items"][0]
    assert item["id"] == archive_id
    assert item["created_at"].endswith("+00:00")
    assert item["finished_at"].endswith("+00:00")
    created = datetime.fromisoformat(item["created_at"])
    beijing = created.astimezone(timezone(timedelta(hours=8)))
    assert beijing.utcoffset() == timedelta(hours=8)


def test_library_row_time_has_timezone_offset(service):
    result = service.import_rows(
        [{"address": SEED, "category": "其他", "label": "测试"}], "manual"
    )
    assert result["inserted"] == 1
    rows = service.library()
    row = next(item for item in rows if item["address"] == SEED)
    assert row["created_at"].endswith("+00:00")
    assert row["updated_at"].endswith("+00:00")
