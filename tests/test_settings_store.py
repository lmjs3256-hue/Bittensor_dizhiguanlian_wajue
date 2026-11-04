"""本机设置持久化：Taostats API key 的存取与重启恢复。"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

import app.main as main_module
import app.taostats as taostats
from app import settings_store
from app.db import Database
from app.service import LinkService


KEY = "persisted-test-key-0123456789"


def _reset_key(monkeypatch):
    monkeypatch.delenv("TAOSTATS_API_KEY", raising=False)
    taostats.clear_api_key()


def test_save_and_load_roundtrip(monkeypatch):
    _reset_key(monkeypatch)
    storage = settings_store.save_api_key(KEY)

    assert storage in {"dpapi", "plain"}
    assert settings_store.storage_kind() == storage
    assert settings_store.load_api_key() == KEY
    assert settings_store.settings_path().exists()


def test_clear_removes_key_from_disk(monkeypatch):
    _reset_key(monkeypatch)
    settings_store.save_api_key(KEY)

    settings_store.clear_api_key()

    assert settings_store.load_api_key() is None
    assert settings_store.storage_kind() == ""
    assert KEY not in settings_store.settings_path().read_text(encoding="utf-8")


def test_broken_settings_file_is_ignored(monkeypatch, tmp_path):
    _reset_key(monkeypatch)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("WALLET_LINK_SETTINGS", str(broken))

    assert settings_store.load_api_key() is None
    assert settings_store.storage_kind() == ""
    # 写坏文件不应让保存流程抛异常。
    assert settings_store.save_api_key(KEY) in {"dpapi", "plain"}
    assert settings_store.load_api_key() == KEY


def test_key_is_restored_after_service_restart(monkeypatch):
    _reset_key(monkeypatch)
    settings_store.save_api_key(KEY)

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "restore.db")
        try:
            database.initialize()
            monkeypatch.setattr(main_module, "DATABASE", database)
            monkeypatch.setattr(
                main_module, "SERVICE", LinkService(database, "wss://example.invalid")
            )
            assert taostats.is_configured() is False
            with TestClient(main_module.app) as client:
                overview = client.get("/api/overview").json()
                assert overview["taostats_configured"] is True
                assert overview["taostats_key_storage"] in {"dpapi", "plain"}
        finally:
            database.engine.dispose()


def test_taostats_job_requires_api_key(monkeypatch):
    _reset_key(monkeypatch)
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "job-key.db")
        try:
            database.initialize()
            monkeypatch.setattr(main_module, "DATABASE", database)
            monkeypatch.setattr(
                main_module, "SERVICE", LinkService(database, "wss://example.invalid")
            )
            with TestClient(main_module.app) as client:
                response = client.post(
                    "/api/relations/jobs",
                    json={
                        "seed": "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt",
                        "depth": 1,
                    },
                )
            assert response.status_code == 400
            assert "API key" in response.json()["detail"]
        finally:
            database.engine.dispose()
