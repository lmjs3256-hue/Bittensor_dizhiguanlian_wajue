"""两地址关联明细检索：地址 × 地址库、地址 × 地址（含中间链路）。

Taostats 全部 mock，不发真实请求；本地索引用真实的 transfers 表。
"""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app import interlink, taostats
from app.db import AddressLibrary, Database, Transfer
from app.inputs import InputError
from app.service import LinkService

A = "5HTLuZKULeBe3PEcXYsL9ABRCf3vJuw6dVS2wTLwyoRuTif7"
B = "5C51bmwMxk9TTswMj2wkiCvdAyWDo3wXw9i4Bcoqf93WjaHP"
C = "5C523K1nqhGWUrpvRtJxSCdiWLiRxR1RefjtcHbQc8fQ84hk"
MID = "5CrmCnoTqxpCuZoSjYRNb3FucZgWhYBCAc6yQcwoVyVT7oQ5"
OTHER = "5H5J2LgZCfodSQhq5W4TFTb3rmQcczrCfGD2DDZSrJcXiihy"


@contextmanager
def build_env(name="interlink.db"):
    """临时库 + 服务；退出时 dispose 引擎，否则 Windows 下删不掉 db 文件。"""

    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / name)
        database.initialize()
        try:
            yield database, LinkService(database, "wss://example.invalid")
        finally:
            database.engine.dispose()


def page(rows, next_page=None):
    return taostats.TransferPage(rows=rows, next_page=next_page)


def row(sender, receiver, amount, block=100, ts="2026-01-01T00:00:00.000Z", tx="0xabc"):
    return {
        "from": sender,
        "to": receiver,
        "amount": str(amount),
        "block_number": block,
        "timestamp": ts,
        "transaction_hash": tx,
        "extrinsic_id": f"{block}-0001",
    }


def seed_group(service, addresses, label="6NCK"):
    with service.database.session() as db:
        for position, address in enumerate(addresses):
            db.add(
                AddressLibrary(
                    address=address,
                    label=label,
                    category="大V",
                    note=f"n{position}",
                    book="",
                )
            )
        db.commit()


def test_group_mode_matches_library_members_from_taostats(monkeypatch):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return page(
            [
                row(A, B, 5_000_000_000, block=101),
                row(C, A, 2_000_000_000, block=102, tx="0xdef"),
                row(A, OTHER, 9_000_000_000, block=103, tx="0x999"),  # 不在组里
            ]
        )

    monkeypatch.setattr(taostats, "get_transfer_page_query", fake)
    with build_env() as (database, service):
        seed_group(service, [A, B, C])
        result = service.interlink_search(source=A, group_name="6NCK", group_kind="label")

    assert result["mode"] == "group"
    assert result["matched_addresses"] == 2
    assert {row["address"] for row in result["summary"]} == {B, C}
    out = next(row for row in result["summary"] if row["address"] == B)
    assert out["out_tao"] == pytest.approx(5.0)
    incoming = next(row for row in result["summary"] if row["address"] == C)
    assert incoming["in_tao"] == pytest.approx(2.0)
    assert len(result["transactions"]) == 2
    assert calls and calls[0]["address"] == A


def test_group_mode_skill_uses_local_index_when_no_api(monkeypatch):
    monkeypatch.setattr(
        taostats,
        "get_transfer_page_query",
        lambda **kwargs: page([]),
    )
    with build_env() as (database, service):
        seed_group(service, [A, B, C])
        with database.session() as db:
            db.add(
                Transfer(
                    event_key="k1",
                    tx_hash="0xlocal",
                    block_number=500,
                    from_address=A,
                    to_address=B,
                    amount_rao=3_000_000_000,
                    amount_tao=3.0,
                    timestamp="2026-02-02T00:00:00.000Z",
                )
            )
            db.commit()
        result = service.interlink_search(source=A, group_name="6NCK", group_kind="label")

    assert result["matched_addresses"] == 1
    assert result["transactions"][0]["source"] == "local"
    assert any("本地索引" in note for note in result["notes"])


def test_address_mode_direct_transfers_both_directions(monkeypatch):
    def fake(**kwargs):
        if kwargs.get("from_address") == A and kwargs.get("to_address") == B:
            return page([row(A, B, 4_000_000_000, block=11, tx="0xout")])
        if kwargs.get("from_address") == B and kwargs.get("to_address") == A:
            return page([row(B, A, 1_500_000_000, block=12, tx="0xin")])
        return page([])

    monkeypatch.setattr(taostats, "get_transfer_page_query", fake)
    with build_env() as (database, service):
        result = service.interlink_search(source=A, target=B)

    assert result["mode"] == "address"
    assert len(result["transactions"]) == 2
    directions = {tx["direction"] for tx in result["transactions"]}
    assert directions == {"out", "in"}
    assert result["summary"][0]["tx_count"] == 2
    assert result["paths"] == []


def test_address_mode_finds_one_hop_bridge(monkeypatch):
    def fake(**kwargs):
        if kwargs.get("from_address") or kwargs.get("to_address"):
            return page([])  # 无直连
        address = kwargs.get("address")
        if address == A:
            return page([row(A, MID, 8_000_000_000, block=21)])
        if address == B:
            return page([row(B, MID, 7_000_000_000, block=22)])
        return page([])

    monkeypatch.setattr(taostats, "get_transfer_page_query", fake)
    with build_env() as (database, service):
        result = service.interlink_search(source=A, target=B, max_hops=2)

    assert result["transactions"] == []
    assert result["paths"], "应当找到共同对手方"
    path = result["paths"][0]
    assert path["nodes"] == [A, MID, B]
    assert path["steps"][0]["counterparty"] == MID


def test_address_mode_two_hop_chain(monkeypatch):
    def fake(**kwargs):
        if kwargs.get("from_address") or kwargs.get("to_address"):
            return page([])
        address = kwargs.get("address")
        if address == A:
            return page([row(A, MID, 8_000_000_000, block=31)])
        if address == B:
            return page([row(B, C, 6_000_000_000, block=32)])
        if address == MID:
            return page([row(C, MID, 5_000_000_000, block=33)])
        return page([])

    monkeypatch.setattr(taostats, "get_transfer_page_query", fake)
    with build_env() as (database, service):
        result = service.interlink_search(source=A, target=B, max_hops=3)

    assert result["paths"], "应当拼出 A → 中间 → 中间 → B"
    path = result["paths"][0]
    assert path["hops"] == 3
    assert path["nodes"][0] == A and path["nodes"][-1] == B


def test_min_tao_filters_small_transfers(monkeypatch):
    monkeypatch.setattr(
        taostats,
        "get_transfer_page_query",
        lambda **kwargs: page([row(A, B, 100_000_000, block=41)]),
    )
    with build_env() as (database, service):
        result = service.interlink_search(source=A, target=B, min_tao=1.0)

    assert result["transactions"] == []


def test_rejects_bad_input():
    with build_env() as (database, service):
        with pytest.raises(InputError):
            service.interlink_search(source="not-an-address", target=B)
        with pytest.raises(InputError):
            service.interlink_search(source=A, target="bad")
        with pytest.raises(InputError):
            service.interlink_search(source=A, target=A)
        with pytest.raises(InputError):
            service.interlink_search(source=A)
        with pytest.raises(InputError):
            service.interlink_search(source=A, group_name="不存在的组", group_kind="label")


def test_rate_limit_stops_gracefully(monkeypatch):
    def boom(**kwargs):
        raise taostats.TaostatsRateLimitError("限流", request_made=True)

    monkeypatch.setattr(taostats, "get_transfer_page_query", boom)
    with build_env() as (database, service):
        result = service.interlink_search(source=A, target=B)

    assert result["stats"]["truncated"] is True
    assert any("限速" in note for note in result["notes"])


def test_interlink_api_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_module

    monkeypatch.setattr(
        taostats,
        "get_transfer_page_query",
        lambda **kwargs: page([row(A, B, 2_000_000_000, block=51)]),
    )
    with TestClient(main_module.app) as client:
        response = client.post("/api/interlink", json={"source": A, "target": B})
        bad = client.post("/api/interlink", json={"source": "bad", "target": B})

    assert response.status_code == 200
    assert response.json()["transactions"][0]["amount_tao"] == pytest.approx(2.0)
    assert bad.status_code == 422
