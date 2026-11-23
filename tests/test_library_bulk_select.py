"""选择地址库弹窗：一键全选 / 一键清除 / 项目方库默认预选可取消（1.0.28）。

用户要求（已确认口径）：
- 弹窗要能一键全选所有库、一键清除所有勾选；
- 项目方（SN1–SN160）库打开时默认预选，但不能写死，用户可取消后不监控；
- 后端不再强制把项目方库写进监控名单，改由弹窗预选 + 用户确认。

本文件锁定前端静态接线与后端默认开关，防止后续升级把这些约定改回去。
"""

from __future__ import annotations

import re
from pathlib import Path

import app.main as main_module
from app.event_monitor import address_monitor
from app.event_monitor.default_watch import MAX_DEFAULT_SUBNET, eligible_default_subnet_items

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app" / "static" / "event-monitor" / "app.js").read_text(encoding="utf-8")
RULES = (ROOT / "docs" / "项目规则.md").read_text(encoding="utf-8")


class TestSharedLibraryBulkActions:
    def test_bulk_action_buttons_exist(self):
        """弹窗工具栏必须有全选所有库与一键清除两个按钮。"""

        assert '"全选所有库"' in APP_JS, "弹窗缺少「全选所有库」按钮"
        assert '"一键清除"' in APP_JS, "弹窗缺少「一键清除」按钮"

    def test_select_all_uses_all_groups_not_filtered(self):
        """一键全选要作用于所有库，不能用 visibleGroups()（那是筛选结果）。"""

        match = re.search(r"selectAll\.addEventListener\(\"click\", \(\) => \{(.*?)\n  \}\);", APP_JS, re.DOTALL)
        assert match, "找不到 selectAll 的点击事件处理"
        body = match.group(1)
        assert "groups.reduce(" in body, "一键全选应遍历全部 groups，而不是筛选后的可见库"
        assert "visibleGroups" not in body, "一键全选不能用 visibleGroups（那只是筛选结果）"
        assert "window.confirm" in body, "批量全选前必须确认库数与地址数（项目规则21）"

    def test_clear_resets_all_selection(self):
        """一键清除要清空 selected，包括项目方库的默认预选。"""

        match = re.search(r"clear\.addEventListener\(\"click\", \(\) => \{(.*?)\}\);", APP_JS, re.DOTALL)
        assert match, "找不到 clear 的点击事件处理"
        assert "selected.clear()" in match.group(1), "一键清除没有调用 selected.clear()"

    def test_project_books_default_preselected_by_book_name(self):
        """SN1–SN160 库默认预选整库未监控地址，按库名判定。"""

        assert "subnetBookNumber(group.name)" in APP_JS, "项目方预选应按库名解析 SN 编号"
        assert "bookNumber > 160" in APP_JS, "项目方预选应限制在 SN1–SN160"
        assert "!watched.has(String(item.address))" in APP_JS, "默认预选只能包含尚未监控的地址"

    def test_fully_watched_books_do_not_show_as_checked(self):
        """全库已监控的 checkbox 不得显示 checked，否则一键清除后仍有 100+ 勾选框。

        1.0.29 线上现象：点「一键清除」后统计显示「已选 0」，但 120 个全监控库
        的 checkbox 依然是勾选态（旧逻辑 selectable.length===0 时强制 checked=true），
        用户以为清除没生效。已监控状态改由文字和 aria-label 表达。
        """

        assert "const fullyWatched = state.selectable.length === 0" in APP_JS
        assert "checkbox.checked = !fullyWatched && state.selectedCount === state.selectable.length" in APP_JS, (
            "全库已监控时 checkbox 必须 unchecked，不能借用 checked 表达「已完成」"
        )
        assert "shared-book-fully-watched" in APP_JS, "全监控库应有独立 class 用于样式区分"

    def test_only_selected_addresses_are_submitted(self):
        """取消勾选的地址不在 selected 里，确认时不会被提交（= 不会被监控）。"""

        assert "items.filter((entry) => selected.has(entry.address))" in APP_JS, (
            "确认加入监控时只应提交 selected 里的地址，取消的地址不得写入"
        )


class TestBackendSeedDisabledByDefault:
    def test_seed_switch_defaults_to_false(self):
        """后端强制 seed 默认关闭（1.0.28）：项目方库改由弹窗预选。"""

        assert main_module.SEED_SUBNET_WATCH_ENABLED is False, (
            "SEED_SUBNET_WATCH_ENABLED 默认为 True 会把用户取消的监控又写回去"
        )

    def test_seed_function_short_circuits_when_disabled(self, monkeypatch):
        """开关关闭时 seed 直接返回 disabled，不触碰监控库。"""

        monkeypatch.setattr(main_module, "SEED_SUBNET_WATCH_ENABLED", False)
        monkeypatch.setattr(main_module, "EVENT_MONITOR", type("M", (), {"running": True, "context": object()})())
        result = main_module._seed_default_event_subnet_watch()
        assert result == {"seeded": False, "reason": "disabled_by_default"}


class TestRuleDocumentSynced:
    def test_rule_15_updated_to_preselect_model(self):
        """项目规则第15条必须反映新口径，避免后续实现又回到强制 seed。"""

        assert "默认预选、可取消" in RULES or "默认预选" in RULES, "项目规则未记录默认预选口径"
        assert "不再由后端强制写入监控名单" in RULES, "项目规则未记录后端不再强制 seed"


class TestSubnetLimit160:
    """1.0.29：子网上限从 128/127 统一提高到 160。"""

    def test_max_default_subnet_is_160(self):
        assert MAX_DEFAULT_SUBNET == 160
        assert address_monitor.PROJECT_OWNER_MAX_NETUID == 160, (
            "默认监控预选上限与项目方尾注上限必须一致，否则 SN128-160 的事件没有项目方尾注"
        )

    def test_eligible_items_boundary(self):
        def item(name: str) -> dict:
            return {"book": name, "address": "5Gy8difKxYXC32zqk7kufGHpxsSBuGozbZ7Zo1iySsQCjEbt"}

        eligible = eligible_default_subnet_items([item("SN1 Apex"), item("SN160 Edge"), item("SN161 TooHigh")])
        names = [x["book"] for x in eligible]
        assert names == ["SN1 Apex", "SN160 Edge"], "SN160 应入选、SN161 应被忽略"

    def test_project_owner_netuid_boundary(self):
        owner = {"address_role": "project_owner"}
        assert address_monitor._project_owner_netuid({**owner, "library_book": "SN160 Edge"}) == 160
        assert address_monitor._project_owner_netuid({**owner, "library_book": "SN161 TooHigh"}) is None
