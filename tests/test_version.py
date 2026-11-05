from fastapi.testclient import TestClient

import app.main as main_module
from app.version_info import read_version_info


def test_version_info_reads_current_version_and_release_notes():
    info = read_version_info()

    assert info["version"] == "0.6.21"
    assert info["entries"][0]["version"] == "0.6.21"
    assert any("改判" in change for change in info["entries"][0]["changes"])


def test_version_endpoint_exposes_current_version_and_release_notes():
    with TestClient(main_module.app) as client:
        response = client.get("/api/version")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == "0.6.21"
    assert payload["entries"][0]["title"] == "交易所结论可随时改判 + 组内手动加地址"


def test_health_reports_service_root_and_pid():
    """启动脚本靠 root/pid 区分“本目录的服务”和“另一个副本”。"""

    with TestClient(main_module.app) as client:
        payload = client.get("/api/health").json()

    assert payload["ok"] is True
    assert payload["root"].endswith(main_module.ROOT_DIR.name)
    assert isinstance(payload["pid"], int) and payload["pid"] > 0
