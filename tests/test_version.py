import re
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.version_info import read_version_info

ROOT = Path(__file__).resolve().parents[1]
CURRENT = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_version_info_reads_current_version_and_release_notes():
    info = read_version_info()

    assert info["version"] == CURRENT
    assert info["entries"][0]["version"] == CURRENT
    assert info["entries"][0]["title"]
    assert info["entries"][0]["changes"]


def test_version_endpoint_exposes_current_version_and_release_notes():
    with TestClient(main_module.app) as client:
        response = client.get("/api/version")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == CURRENT
    assert payload["entries"][0]["title"] == read_version_info()["entries"][0]["title"]


def test_health_reports_service_root_and_pid():
    """启动脚本靠 root/pid 区分“本目录的服务”和“另一个副本”。"""

    with TestClient(main_module.app) as client:
        payload = client.get("/api/health").json()

    assert payload["ok"] is True
    assert payload["root"].endswith(main_module.ROOT_DIR.name)
    assert isinstance(payload["pid"], int) and payload["pid"] > 0


def test_readme_version_badge_matches_version_file():
    """README 顶部的版本徽标必须与 VERSION 一致。

    这一行是手写的、不受任何代码约束，历史上从 0.8.6 一路漏到 0.8.19
    （13 个版本没改）。用户看到的是"版本号每次都要人提醒才改"——根因不是
    忘了改，而是**没有任何机制会因此报错**。这条测试就是那个机制。
    """

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"^版本：\*\*([0-9][^*]*)\*\*\s*$", readme, re.MULTILINE)
    assert match, "README.md 顶部应有一行 `版本：**x.y.z**`"
    assert match.group(1).strip() == version, (
        f"README 版本徽标是 {match.group(1).strip()}，但 VERSION 是 {version}；"
        "升级版本时这两处要一起改"
    )


def test_static_asset_query_matches_version():
    """index.html 里两处 `?v=` 也必须跟着 VERSION 走。

    漏改的后果很隐蔽：浏览器继续用旧 JS/CSS，功能看起来"改了没生效"，
    而页脚版本号却是新的，很容易误判成新版本有 bug。
    """

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    html = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    tags = re.findall(r'(?:styles\.css|app\.js)\?v=([0-9][0-9.]*)', html)
    assert len(tags) == 2, f"应有 styles.css 与 app.js 两处 ?v=，实际找到 {len(tags)} 处"
    for value in tags:
        assert value == version, (
            f"静态资源版本是 {value}，但 VERSION 是 {version}；"
            "漏改会让浏览器继续用旧缓存"
        )
