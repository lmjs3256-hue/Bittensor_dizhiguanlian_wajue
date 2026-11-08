# -*- coding: utf-8 -*-
"""Taostats API key 池：多把 key 各自限速、按空闲度调度（0.8.24）。

背景：单把 key 每分钟只有 5 次额度，且限速器原先是**一个全局 deque**，
多人共用同一窗口——先到的人占满，后到的人直接吃 429。多把 key 是唯一能
线性扩容的办法，但必须做到「各算各的窗口」，否则多买几把 key 也只用到一把
的量。

用户选定的三条口径（改之前先看）：
1. 池化调度：哪把空闲用哪把，不绑定设备；
2. 每日预算按所有 key **合计**计（可配置），多 key 只提速不抬总量；
3. 全部冷却时**直接报错**，不排队、不静默等待。
"""

from __future__ import annotations

import threading

import httpx
import pytest

from app import taostats


@pytest.fixture(autouse=True)
def clean_pool(monkeypatch):
    """每个用例都从干净的池 + 干净的 key 覆盖状态开始。

    ``configure_api_key()`` 会把 ``_key_override_active`` 置位并一直有效，
    跨用例残留会让"单 key 回退"这类用例读到上一条用例留下的 key。
    """

    monkeypatch.delenv("TAOSTATS_API_KEY", raising=False)
    taostats.reset_key_pool_for_tests()
    taostats._request_times.clear()
    taostats._key_override = None
    taostats._key_override_active = False
    yield
    taostats.reset_key_pool_for_tests()
    taostats._request_times.clear()
    taostats._key_override = None
    taostats._key_override_active = False


def make_keys(*labels):
    """造几把假 key，返回 set_key_pool 需要的结构。

    key 值直接用 label 拼，测试里要断言"用的是哪一把"时好认。
    """

    return [
        {"key": f"{label}-secret", "label": label} for label in labels
    ]


class TestPoolBasics:
    def test_pool_starts_empty(self):
        assert taostats.key_pool_status() == []

    def test_set_pool_masks_secrets(self):
        """状态里绝不能出现明文——页面和日志都靠这个结构。"""

        gray = taostats.set_key_pool(make_keys("主号"))
        assert len(gray) == 1
        item = gray[0]
        assert "secret" not in item
        assert item["masked"].startswith("****")
        assert "secret" not in item["masked"]
        assert item["label"] == "主号"

    def test_duplicate_keys_collapse(self):
        """同一把 key 填两次只算一次——否则会凭空多出一个"额度"。"""

        dup = [
            {"key": "same-secret", "label": "a"},
            {"key": "same-secret", "label": "b"},
        ]
        pool = taostats.set_key_pool(dup)
        assert len(pool) == 1

    def test_blank_entries_ignored(self):
        pool = taostats.set_key_pool(
            [{"key": "  ", "label": "空"}, {"key": "real", "label": "真"}]
        )
        assert len(pool) == 1

    def test_resaving_keeps_runtime_state(self):
        """重复保存不能把冷却清掉——否则被限流的 key 会立刻"复活"。"""

        taostats.set_key_pool(make_keys("主号"))
        state = taostats._key_pool[0]
        state.cooldown_until = 10**9  # 很远的未来
        state.requests_total = 7

        taostats.set_key_pool(make_keys("主号"))  # 再存一次
        again = taostats._key_pool[0]
        assert again.cooldown_until == 10**9
        assert again.requests_total == 7


class TestPerKeyRateLimit:
    def test_each_key_has_its_own_window(self):
        """核心：一把 key 用满 5 次，另一把仍然可用。"""

        taostats.set_key_pool(make_keys("A", "B"))
        # 连续取 5 次，全部落在 A 或 B 上，但总量应该能到 10。
        used = []
        for _ in range(10):
            key_id, secret = taostats._acquire_key()
            used.append(secret)
            assert secret
        assert len(set(used)) == 2, "两把 key 都该被用到"
        # 第 11 次应该报限流
        with pytest.raises(taostats.TaostatsRateLimitError):
            taostats._acquire_key()

    def test_single_key_limits_at_five(self):
        taostats.set_key_pool(make_keys("只有一把"))
        for _ in range(5):
            _, secret = taostats._acquire_key()
            assert secret
        with pytest.raises(taostats.TaostatsRateLimitError):
            taostats._acquire_key()

    def test_prefers_key_with_most_remaining(self):
        """按剩余额度取，而不是死盯第一把。"""

        taostats.set_key_pool(make_keys("A", "B"))
        first = taostats._key_pool[0]
        # 把 A 用掉 4 次，B 还是满的 → 接下来应该优先给 B
        now = __import__("time").monotonic()
        for _ in range(4):
            first.reserve(now)
        _, secret = taostats._acquire_key()
        assert secret != first.secret, "B 的剩余额度更多，应优先用 B"


class TestCooldown:
    def test_rate_limited_key_rests_but_others_serve(self):
        """一把 key 吃 429 → 它休息，另一把继续干活。"""

        taostats.set_key_pool(make_keys("A", "B"))
        bad = taostats._key_pool[0]
        taostats._note_key_failure(bad.key_id, taostats.TaostatsRateLimitError("429"))
        assert bad.cooldown_until > 0

        # 冷却中的那把不该再被选中
        for _ in range(5):
            _, secret = taostats._acquire_key()
            assert secret != bad.secret, "冷却中的 key 不该被选中"

    def test_auth_failure_rests_longer_than_rate_limit(self):
        taostats.set_key_pool(make_keys("A"))
        state = taostats._key_pool[0]
        taostats._note_key_failure(state.key_id, taostats.TaostatsAuthError("401"))
        auth_wait = state.cooldown_until

        taostats.set_key_pool(make_keys("A"))
        state = taostats._key_pool[0]
        state.cooldown_until = 0
        taostats._note_key_failure(state.key_id, taostats.TaostatsRateLimitError("429"))
        # key 无效 10 分钟 > 限流 60 秒
        assert auth_wait > state.cooldown_until

    def test_all_cooling_reports_earliest_recovery(self):
        """全冷却时直接报错，且告诉用户大概要等多久（用户要求不排队）。"""

        taostats.set_key_pool(make_keys("A", "B"))
        for state in taostats._key_pool:
            taostats._note_key_failure(state.key_id, taostats.TaostatsRateLimitError("429"))

        with pytest.raises(taostats.TaostatsRateLimitError) as excinfo:
            taostats._acquire_key()
        message = str(excinfo.value)
        assert "所有" in message and "冷却" in message
        assert "秒" in message


class TestSingleKeyFallback:
    def test_no_pool_uses_configured_key(self, monkeypatch):
        """池为空时退回单 key 老路径，现有用法完全不变。"""

        monkeypatch.setenv("TAOSTATS_API_KEY", "env-key")
        taostats.reset_key_pool_for_tests()
        for _ in range(5):
            key_id, secret = taostats._acquire_key()
            assert key_id == ""  # 单 key 路径不产生 key_id
            assert secret == "env-key"
        with pytest.raises(taostats.TaostatsRateLimitError):
            taostats._acquire_key()

    def test_pool_takes_precedence_over_env(self, monkeypatch):
        monkeypatch.setenv("TAOSTATS_API_KEY", "env-key")
        taostats.set_key_pool(make_keys("池里的"))
        _, secret = taostats._acquire_key()
        assert secret == "池里的-secret"


class TestPoolStatusPayload:
    def test_status_reports_remaining_and_cooldown(self):
        taostats.set_key_pool(make_keys("A"))
        state = taostats._key_pool[0]
        now = __import__("time").monotonic()
        state.reserve(now)
        state.reserve(now)

        item = taostats.key_pool_status()[0]
        assert item["remaining"] == 3  # 5 - 2
        assert item["requests_total"] == 2
        assert item["cooldown_seconds"] == 0

    def test_status_clamps_negative_remaining(self):
        """冷却中的 key 剩余额是 -1，对外必须显示 0 而不是负数。"""

        taostats.set_key_pool(make_keys("A"))
        state = taostats._key_pool[0]
        taostats._note_key_failure(state.key_id, taostats.TaostatsRateLimitError("429"))
        item = taostats.key_pool_status()[0]
        assert item["remaining"] == 0
        assert item["cooldown_seconds"] > 0


class TestStorageRoundTrip:
    """key 池的持久化：多把独立加密、老字段兼容。"""

    def test_save_and_load_multiple(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WALLET_LINK_SETTINGS", str(tmp_path / "s.json"))
        from app import settings_store

        storage = settings_store.save_api_keys(
            [{"key": "k1-aaaa", "label": "主号"}, {"key": "k2-bbbb", "label": "备用"}]
        )
        assert storage in {"dpapi", "plain"}

        loaded = settings_store.load_api_keys()
        assert [item["key"] for item in loaded] == ["k1-aaaa", "k2-bbbb"]
        assert [item["label"] for item in loaded] == ["主号", "备用"]

    def test_legacy_single_key_field_is_read_as_pool(self, tmp_path, monkeypatch):
        """升级用户只有老字段时，要当成池里唯一那把，不能显示"未配置"。"""

        monkeypatch.setenv("WALLET_LINK_SETTINGS", str(tmp_path / "s.json"))
        from app import settings_store

        settings_store.save_api_key("legacy-key-1234")
        loaded = settings_store.load_api_keys()
        assert [item["key"] for item in loaded] == ["legacy-key-1234"]

    def test_saving_pool_updates_legacy_field(self, tmp_path, monkeypatch):
        """存池时同步写老字段，让只认单个 key 的代码继续工作。"""

        monkeypatch.setenv("WALLET_LINK_SETTINGS", str(tmp_path / "s.json"))
        from app import settings_store

        settings_store.save_api_keys([{"key": "first-key", "label": ""}])
        assert settings_store.load_api_key() == "first-key"

    def test_clearing_pool_clears_both_fields(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WALLET_LINK_SETTINGS", str(tmp_path / "s.json"))
        from app import settings_store

        settings_store.save_api_keys([{"key": "k1", "label": ""}])
        settings_store.save_api_keys([])
        assert settings_store.load_api_keys() == []
        assert settings_store.load_api_key() is None


class TestConcurrentAcquire:
    def test_parallel_acquire_never_exceeds_per_key_limit(self):
        """并发下每把 key 的实际使用次数不能超过 5（限速器必须线程安全）。"""

        taostats.set_key_pool(make_keys("A", "B"))
        granted: list[str] = []
        lock = threading.Lock()

        def worker():
            for _ in range(5):
                try:
                    _, secret = taostats._acquire_key()
                except taostats.TaostatsRateLimitError:
                    return
                with lock:
                    granted.append(secret)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 两把 key × 5 次 = 最多 10 次，不能超发
        assert len(granted) <= 10
        from collections import Counter

        counts = Counter(granted)
        for secret, count in counts.items():
            assert count <= taostats.LOCAL_REQUESTS_PER_MINUTE


class TestPoolEndpoints:
    """接口层：读池状态绝不回显明文；存池支持 id 引用。"""

    def _client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WALLET_LINK_SETTINGS", str(tmp_path / "s.json"))
        from fastapi.testclient import TestClient

        import app.main as main_module

        return TestClient(main_module.app)

    def test_get_keys_never_returns_plaintext(self, tmp_path, monkeypatch):
        taostats.set_key_pool([{"key": "super-secret-value-9999", "label": "主号"}])
        with self._client(tmp_path, monkeypatch) as client:
            payload = client.get("/api/settings/taostats/keys").json()
        blob = str(payload)
        assert "super-secret-value-9999" not in blob, "接口绝不能让明文出现在响应里"
        assert payload["items"][0]["masked"].endswith("9999")

    def test_save_by_id_keeps_key_without_plaintext(self, tmp_path, monkeypatch):
        """页面拿不到明文，只能按 id 回传——服务端要能从内存取回原文。"""

        with self._client(tmp_path, monkeypatch) as client:
            first = client.post(
                "/api/settings/taostats/keys",
                json={"keys": [{"key": "key-one-secret", "label": "一号"}]},
            ).json()
            key_id = first["items"][0]["id"]

            # 只给 id + 改个备注名，不带任何明文
            second = client.post(
                "/api/settings/taostats/keys",
                json={"keys": [{"id": key_id, "label": "改名了"}]},
            ).json()

        assert second["items"][0]["label"] == "改名了"
        assert second["configured"] is True
        # key 没丢：仍能取到同一把（id 不变即同一把）
        assert second["items"][0]["id"] == key_id

    def test_unknown_id_is_ignored(self, tmp_path, monkeypatch):
        """id 过期/伪造时宁可少存，也不能凭空造出空 key。"""

        with self._client(tmp_path, monkeypatch) as client:
            result = client.post(
                "/api/settings/taostats/keys",
                json={"keys": [{"id": "deadbeef00", "label": "幽灵"}]},
            ).json()
        assert result["items"] == []
        assert result["configured"] is False

    def test_saving_pool_roundtrips_to_disk(self, tmp_path, monkeypatch):
        with self._client(tmp_path, monkeypatch) as client:
            client.post(
                "/api/settings/taostats/keys",
                json={
                    "keys": [
                        {"key": "persist-a", "label": "A"},
                        {"key": "persist-b", "label": "B"},
                    ]
                },
            )
        from app import settings_store

        loaded = settings_store.load_api_keys()
        assert [item["key"] for item in loaded] == ["persist-a", "persist-b"]
