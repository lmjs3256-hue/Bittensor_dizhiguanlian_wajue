"""离线授权：机器码、签名校验、激活流程、未授权拦截与水印。"""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import license as license_mod
from app.license import LicenseError, machine_code, normalize_machine, verify_token


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_token(
    private_key: Ed25519PrivateKey,
    *,
    machines: list[str] | None = None,
    expires: str = "",
    team: str = "测试团队",
    version: int = 1,
) -> str:
    payload = {
        "v": version,
        "team": team,
        "machines": machines if machines is not None else [machine_code()],
        "expires": expires,
        "issued": date.today().isoformat(),
        "note": "",
    }
    body = b64(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    signature = private_key.sign(f"WL1.{body}".encode("ascii"))
    return f"WL1.{body}.{b64(signature)}"


@pytest.fixture()
def signing(monkeypatch):
    """生成一对临时密钥，并把程序里的公钥换成它。"""

    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    monkeypatch.setattr(
        license_mod, "_public_key", lambda: Ed25519PublicKey.from_public_bytes(public)
    )
    return key


@pytest.fixture()
def license_home(tmp_path, monkeypatch):
    path = tmp_path / "license.key"
    monkeypatch.setenv("WALLET_LINK_LICENSE", str(path))
    monkeypatch.setattr(license_mod, "ROOT_DIR", tmp_path)
    return path


def test_machine_code_is_stable_and_readable():
    first = machine_code()
    assert first == machine_code()
    assert len(first) == 19  # 4-4-4-4 加三个连字符
    assert len([part for part in first.split("-") if part]) == 4
    assert normalize_machine(first.lower()) == normalize_machine(first)


def test_token_roundtrip_and_watermark(signing, license_home):
    token = make_token(signing, expires=(date.today() + timedelta(days=30)).isoformat())
    row = license_mod.save_token(token)
    assert row.team == "测试团队"

    loaded = license_mod.load()
    assert loaded is not None and loaded.team == "测试团队"
    assert license_home.exists()

    status = license_mod.status()
    assert status["licensed"] is True
    assert status["days_left"] == 30
    assert status["watermark"].startswith("测试团队 · ")
    assert license_mod.is_licensed() is True


def test_token_rejects_tampering(signing, license_home):
    token = make_token(signing)
    header, body, signature = token.split(".")
    forged_body = b64(
        json.dumps(
            {"v": 1, "team": "伪造团队", "machines": ["*"], "expires": ""},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    with pytest.raises(LicenseError, match="签名"):
        verify_token(f"{header}.{forged_body}.{signature}")


def test_token_rejects_other_machine(signing):
    token = make_token(signing, machines=["AAAA-BBBB-CCCC-DDDD"])
    with pytest.raises(LicenseError, match="机器码"):
        verify_token(token)


def test_token_rejects_expired(signing):
    token = make_token(signing, expires=(date.today() - timedelta(days=1)).isoformat())
    with pytest.raises(LicenseError, match="到期"):
        verify_token(token)


def test_permanent_and_wildcard_license(signing):
    token = make_token(signing, machines=["*"], expires="")
    row = verify_token(token)
    assert row.expires is None
    assert row.days_left is None
    assert row.covers("任意机器-0000")


def real_gate() -> bool:
    """未打桩的真实授权判断，用于验证 403 拦截。"""

    try:
        return license_mod.load() is not None
    except LicenseError:
        return False


def test_missing_license_blocks_api(license_home, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_module

    monkeypatch.setattr("app.license.is_licensed", real_gate)
    with TestClient(main_module.app) as client:
        status = client.get("/api/license").json()
        assert status["licensed"] is False
        assert status["machine"] == machine_code()
        assert "license.key" in status["reason"]

        blocked = client.get("/api/library")
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "unlicensed"

        # 首页仍然能打开，否则用户看不到机器码
        assert client.get("/").status_code == 200


def test_activate_endpoint_opens_the_tool(signing, license_home, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_module

    token = make_token(signing, expires=(date.today() + timedelta(days=10)).isoformat())
    monkeypatch.setattr("app.license.is_licensed", real_gate)
    with TestClient(main_module.app) as client:
        assert client.get("/api/library").status_code == 403
        bad = client.post("/api/license/activate", json={"token": "WL1.xx.yy"})
        assert bad.status_code == 422

        ok = client.post("/api/license/activate", json={"token": token})
        assert ok.status_code == 200
        assert ok.json()["team"] == "测试团队"

        assert client.get("/api/library").status_code == 200
        assert client.get("/api/health").json()["licensed"] is True


def test_export_carries_team_watermark(monkeypatch, tmp_path):
    from app.db import Database
    from app.service import LinkService

    addresses = [
        "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt",
        "5DceuTr7XPqw67PNHW9ewzzo2cZ7Cm8HgfjwUqEpd8Mh8QNg",
    ]
    monkeypatch.setattr(license_mod, "watermark", lambda: "测试团队 · ABC123")
    database = Database(Path(tmp_path) / "watermark.db")
    database.initialize()
    service = LinkService(database, "wss://example.invalid")
    service.create_address_book(name="水印库", seed="", addresses=addresses)

    text, _, _ = service.export_address_book("水印库", "txt")
    assert text.splitlines()[0] == "# Wallet Link · 授权给 测试团队 · ABC123"

    # 带水印的 TXT 仍然能被自己的导入逻辑正确解析
    from app.inputs import parse_plain_addresses

    assert parse_plain_addresses(text) == addresses


def test_unlicensed_export_has_no_header(monkeypatch, tmp_path):
    from app.db import Database
    from app.service import LinkService

    monkeypatch.setattr(license_mod, "watermark", lambda: "")
    database = Database(Path(tmp_path) / "plain.db")
    database.initialize()
    service = LinkService(database, "wss://example.invalid")
    service.create_address_book(
        name="无水印库", seed="", addresses=["5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"]
    )
    text, _, _ = service.export_address_book("无水印库", "txt")
    assert text.startswith("5Gy8")
