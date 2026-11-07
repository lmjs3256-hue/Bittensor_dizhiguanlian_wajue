# -*- coding: utf-8 -*-
"""环境自检：预置名单文件 / 落库数量 / 版本，以及自检接口本身。"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app import diagnostics
from app.db import Database
from app.service import LinkService


VALIDATOR = "5HK5tp6t2S59DywmHRWPBVJeJ86T61KjurYqeooqj8sREpeN"


def build_service(directory, name="diagnose.db"):
    database = Database(Path(directory) / name)
    database.initialize()
    return database, LinkService(database, "wss://example.invalid")


def report_for(database, **kwargs):
    snapshot_path = kwargs.pop("snapshot_path", None)
    return diagnostics.build_report(
        database,
        version="0.7.8",
        root="E:/fake/project",
        db_path=database.path,
        snapshot_path=snapshot_path,
        **kwargs,
    )


def check(report, ident):
    return next(item for item in report["checks"] if item["id"] == ident)


def test_report_passes_after_builtin_seed():
    diagnostics.clear_cache()
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.ensure_builtin_addresses()
            report = report_for(database, taostats_configured=True)
            assert report["ok"] is True
            assert check(report, "snapshot_file")["status"] == "pass"
            assert check(report, "builtin_rows")["status"] == "pass"
            assert check(report, "version")["detail"].startswith("VERSION 文件与运行进程都是 0.7.8")
        finally:
            database.engine.dispose()


def test_missing_snapshot_file_is_reported_as_fail(tmp_path):
    diagnostics.clear_cache()
    with TemporaryDirectory() as directory:
        database, _service = build_service(directory)
        try:
            report = report_for(database, snapshot_path=tmp_path / "nope.txt")
            assert report["ok"] is False
            item = check(report, "snapshot_file")
            assert item["status"] == "fail"
            assert "git pull" in item["hint"]
        finally:
            database.engine.dispose()


def test_empty_library_fails_builtin_check():
    """库是空的时候必须报「预置名单没进库」，这正是远端看到的现象。"""

    diagnostics.clear_cache()
    with TemporaryDirectory() as directory:
        database, _service = build_service(directory)
        try:
            report = report_for(database)
            assert report["ok"] is False
            item = check(report, "builtin_rows")
            assert item["status"] == "fail"
            assert "停止" in item["hint"] and "启动" in item["hint"]
        finally:
            database.engine.dispose()


def test_truncated_snapshot_warns(tmp_path):
    diagnostics.clear_cache()
    small = tmp_path / "small.txt"
    small.write_text("# 只有两条\n" + VALIDATOR + "\n5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty\n", "utf-8")
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.ensure_builtin_addresses()
            report = report_for(database, snapshot_path=small)
            assert check(report, "snapshot_file")["status"] == "warn"
            assert diagnostics.snapshot_status(small)["count"] == 2
        finally:
            database.engine.dispose()


def test_missing_taostats_key_is_only_a_warning():
    diagnostics.clear_cache()
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.ensure_builtin_addresses()
            report = report_for(database, taostats_configured=False)
            assert report["ok"] is True
            assert check(report, "taostats_key")["status"] == "warn"
            assert "预置验证者名单不受影响" in check(report, "taostats_key")["detail"]
        finally:
            database.engine.dispose()


def test_startup_result_is_surfaced():
    diagnostics.clear_cache()
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            startup = service.ensure_builtin_addresses()
            report = report_for(database, startup_builtin=startup)
            detail = check(report, "startup_write")["detail"]
            assert f"新增 {startup['inserted']} 条" in detail
        finally:
            database.engine.dispose()


def test_library_status_cache_avoids_recount():
    diagnostics.clear_cache()
    with TemporaryDirectory() as directory:
        database, service = build_service(directory)
        try:
            service.ensure_builtin_addresses()
            first = diagnostics.library_status(database, ttl=30)
            # 缓存命中后即使库被清空，短 TTL 内仍返回上一次结果。
            with database.session() as db:
                from sqlalchemy import delete

                from app.db import AddressLibrary

                db.execute(delete(AddressLibrary))
            cached = diagnostics.library_status(database, ttl=30)
            assert cached["total"] == first["total"]
            fresh = diagnostics.library_status(database, ttl=0)
            assert fresh["total"] == 0
        finally:
            database.engine.dispose()


def test_format_report_mentions_failure():
    diagnostics.clear_cache()
    with TemporaryDirectory() as directory:
        database, _service = build_service(directory)
        try:
            text = diagnostics.format_report(report_for(database))
            assert "[FAIL]" in text
            assert "处理建议" in text
            assert "结论：" in text
        finally:
            database.engine.dispose()


def test_diagnose_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_module

    diagnostics.clear_cache()
    # 自检接口会顺带探测出站可达性，那要真的发请求。这个用例只验证自检能
    # 跑通，不打桩的话会在测试机上卡十几秒，而且结果依赖本机网络。
    monkeypatch.setattr(main_module, "_outbound_probe_cached", lambda force=False: None)
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "api-diagnose.db")
        original = getattr(main_module, "SERVICE", None)
        main_module.SERVICE = service
        try:
            main_module.app.dependency_overrides[main_module.service] = lambda: service
            client = TestClient(main_module.app)
            try:
                service.ensure_builtin_addresses()
                payload = client.get("/api/diagnose").json()
                assert payload["ok"] is True
                assert payload["snapshot"]["count"] > 1000
                assert payload["library"]["validators"] > 1000
            finally:
                main_module.app.dependency_overrides.clear()
        finally:
            main_module.SERVICE = original
            database.engine.dispose()


def test_health_exposes_snapshot_count():
    from fastapi.testclient import TestClient

    import app.main as main_module

    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "api-health.db")
        original = getattr(main_module, "SERVICE", None)
        main_module.SERVICE = service
        try:
            main_module.app.dependency_overrides[main_module.service] = lambda: service
            client = TestClient(main_module.app)
            try:
                payload = client.get("/api/health").json()
                assert payload["validator_snapshot"] > 1000
                assert payload["started_at"]
            finally:
                main_module.app.dependency_overrides.clear()
        finally:
            main_module.SERVICE = original
            database.engine.dispose()


def test_version_mismatch_is_reported_as_fail():
    """磁盘 VERSION 比进程新：必须报「代码更新了但服务没重启」。"""

    diagnostics.clear_cache()
    with TemporaryDirectory() as directory:
        database, service = build_service(directory, "stale.db")
        try:
            service.ensure_builtin_addresses()
            report = diagnostics.build_report(
                database,
                version="0.7.8",
                running_version="0.7.7",
                root="E:/fake/project",
            )
            assert report["ok"] is False
            assert report["stale"] is True
            item = check(report, "version")
            assert item["status"] == "fail"
            assert "0.7.7" in item["detail"] and "0.7.8" in item["detail"]
            assert "启动.cmd" in item["hint"]
        finally:
            database.engine.dispose()


def test_version_endpoint_flags_stale_process():
    from fastapi.testclient import TestClient

    import app.main as main_module

    client = TestClient(main_module.app)
    payload = client.get("/api/version").json()
    assert payload["running_version"] == main_module.VERSION_INFO["version"]
    assert payload["stale"] is False


def test_outbound_check_passes_when_active_path_reachable():
    diagnostics.clear_cache()
    with TemporaryDirectory() as directory:
        database, _service = build_service(directory)
        try:
            probe = {
                "active": {"url": "http://127.0.0.1:7890", "source": "系统代理"},
                "results": [
                    {"proxy": "http://127.0.0.1:7890", "source": "系统代理", "ok": True, "status_code": 200},
                    {"proxy": None, "source": "直连", "ok": False, "error": "ConnectTimeout"},
                ],
            }
            report = report_for(database, taostats_configured=True, outbound_probe=probe)
            item = check(report, "outbound")
            assert item["status"] == "pass"
            assert "系统代理" in item["detail"]
        finally:
            database.engine.dispose()


def test_outbound_check_warns_when_active_path_down_but_other_works():
    """本机代理掉线后自动切换成功：只提示，不算故障。"""

    diagnostics.clear_cache()
    with TemporaryDirectory() as directory:
        database, _service = build_service(directory)
        try:
            probe = {
                "active": {"url": "http://127.0.0.1:7890", "source": "系统代理"},
                "results": [
                    {"proxy": "http://127.0.0.1:7890", "source": "系统代理", "ok": False, "error": "ConnectTimeout"},
                    {"proxy": None, "source": "直连", "ok": True, "status_code": 200},
                ],
            }
            report = report_for(database, taostats_configured=True, outbound_probe=probe)
            item = check(report, "outbound")
            assert item["status"] == "warn"
            assert "直连" in item["detail"]
        finally:
            database.engine.dispose()


def test_outbound_check_fails_when_nothing_reachable():
    """所有出站路径都不通 = 查询必然退回本地索引，必须报 fail 并指明去查代理。"""

    diagnostics.clear_cache()
    with TemporaryDirectory() as directory:
        database, _service = build_service(directory)
        try:
            probe = {
                "active": {"url": None, "source": "直连"},
                "results": [
                    {"proxy": None, "source": "直连", "ok": False, "error": "ConnectTimeout: timed out"},
                ],
            }
            report = report_for(database, taostats_configured=True, outbound_probe=probe)
            assert report["ok"] is False
            item = check(report, "outbound")
            assert item["status"] == "fail"
            assert "代理" in item["hint"]
        finally:
            database.engine.dispose()


def test_outbound_check_absent_when_not_probed():
    """没传探测结果就不要凭空生成一条检查项。"""

    diagnostics.clear_cache()
    with TemporaryDirectory() as directory:
        database, _service = build_service(directory)
        try:
            report = report_for(database, taostats_configured=True)
            assert all(item["id"] != "outbound" for item in report["checks"])
        finally:
            database.engine.dispose()
