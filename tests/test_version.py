from fastapi.testclient import TestClient

import app.main as main_module
from app.version_info import read_version_info


def test_version_info_reads_current_version_and_release_notes():
    info = read_version_info()

    assert info["version"] == "0.1.5"
    assert info["entries"][0]["version"] == "0.1.5"
    assert any("扫描结果增加起始地址和关联地址的当前钱包余额" in change for change in info["entries"][0]["changes"])


def test_version_endpoint_exposes_current_version_and_release_notes():
    with TestClient(main_module.app) as client:
        response = client.get("/api/version")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == "0.1.5"
    assert payload["entries"][0]["title"] == "余额与地址显示优化"
