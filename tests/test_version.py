from fastapi.testclient import TestClient

import app.main as main_module
from app.version_info import read_version_info


def test_version_info_reads_current_version_and_release_notes():
    info = read_version_info()

    assert info["version"] == "0.6.1"
    assert info["entries"][0]["version"] == "0.6.1"
    assert any("授权" in change or "机器码" in change for change in info["entries"][0]["changes"])


def test_version_endpoint_exposes_current_version_and_release_notes():
    with TestClient(main_module.app) as client:
        response = client.get("/api/version")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == "0.6.1"
    assert payload["entries"][0]["title"] == "双击签发授权"
