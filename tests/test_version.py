from fastapi.testclient import TestClient

import app.main as main_module
from app.version_info import read_version_info


def test_version_info_reads_current_version_and_release_notes():
    info = read_version_info()

    assert info["version"] == "0.1.3"
    assert info["entries"][0]["version"] == "0.1.3"
    assert any("页面底部显示当前应用版本" in change for change in info["entries"][0]["changes"])


def test_version_endpoint_exposes_current_version_and_release_notes():
    with TestClient(main_module.app) as client:
        response = client.get("/api/version")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == "0.1.3"
    assert payload["entries"][0]["title"] == "页面版本与更新日志"
