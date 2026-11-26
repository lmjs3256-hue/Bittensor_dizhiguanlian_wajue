"""清空显示可保存为新事件不再弹回（1.0.30）。

用户口径（本次对话确认）：
- 首页「清空显示」后 SSE 一收到地址类事件就自动恢复，地址转账/交易又弹回；
- 要新增「保存清空状态」按钮把清空锁住，保持期间新事件只计数、不进事件流；
- 本地数据库记录照常写入，取消保持后可查；保持状态要跨标签页与浮窗同步。

本文件用源码静态断言锁定上述接线，防止后续升级把自动恢复或浮窗无条件恢复改回来。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static" / "event-monitor"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
FLOATING_JS = (STATIC / "floating.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
RULES = (ROOT / "docs" / "项目规则.md").read_text(encoding="utf-8")
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


class TestHoldButtonWiring:
    def test_hold_button_exists_in_home_toolbar(self):
        """首页工具栏必须有「保存清空状态」按钮，紧随「清空显示」。"""

        assert 'id="hold-panel-button"' in INDEX_HTML, "首页缺少保存清空状态按钮"
        assert "保存清空状态" in INDEX_HTML
        assert 'id="clear-panel-button"' in INDEX_HTML
        assert INDEX_HTML.index('id="clear-panel-button"') < INDEX_HTML.index('id="hold-panel-button"'), (
            "保存清空状态按钮应放在清空显示按钮之后"
        )
        assert 'aria-pressed="false"' in INDEX_HTML, "按钮应有 aria-pressed 反映保持状态"

    def test_hold_button_toggles_and_persists(self):
        """按钮点击要切换 held、本地存储与文案，取消保持时恢复拉取列表。"""

        match = re.search(r'el\.holdPanelButton\.addEventListener\("click", \(\) => \{(.*?)\n\}\);', APP_JS, re.DOTALL)
        assert match, "找不到保存清空状态按钮的点击事件"
        body = match.group(1)
        assert "state.panelHeld" in body
        assert "setPanelHeld(false)" in body, "再次点击应取消保持"
        assert "setPanelHeld(true)" in body, "首次点击应开启保持"
        assert "loadEvents(" in body, "取消保持后要重新拉取事件流"

    def test_hold_state_is_stored_and_broadcast(self):
        """保持状态与挡下条数写 localStorage 并广播，跨标签页同步。"""

        assert 'panelHeld: "subnetPulse.panelHeld"' in APP_JS
        assert 'heldUpdates: "subnetPulse.heldUpdates"' in APP_JS
        match = re.search(r"function setPanelHeld\(held, options = \{\}\) \{(.*?)\n\}", APP_JS, re.DOTALL)
        assert match, "找不到 setPanelHeld"
        body = match.group(1)
        assert "localStorage.setItem(UI_KEYS.panelHeld" in body
        assert 'broadcastUi("panel-held", state.panelHeld)' in body
        assert "UI_KEYS.panelHeld) applyExternalUiState(\"panel-held\"" in APP_JS, "storage 事件未同步保持状态"
        assert 'type === "panel-held") applyExternalUiState("panel-held"' in APP_JS, "postMessage 未同步保持状态"


class TestSseHeldBranch:
    def test_sse_hold_branch_prevents_auto_restore(self):
        """保持期间 SSE 收到事件只计数，不得撤销清空或重新拉取。"""

        match = re.search(r'stream\.addEventListener\("event", \(message\) => \{(.*?)\n  \}\);', APP_JS, re.DOTALL)
        assert match, "找不到主页 SSE 事件处理"
        body = match.group(1)
        held = re.search(r"if \(state\.panelHeld && state\.panelCleared\) \{(.*?)\} else", body, re.DOTALL)
        assert held, "SSE 处理缺少保持分支"
        assert "countHeldUpdate()" in held.group(1), "保持期间应累计被挡下的条数"
        # 保持分支之后才是原来的自动恢复逻辑；断言自动恢复仍在 else 里，
        # 而不是在保持分支内被执行。
        assert "收到新事件，事件流已自动恢复" in body
        assert body.index("countHeldUpdate()") < body.index("收到新事件，事件流已自动恢复"), (
            "自动恢复必须排在保持分支之后，保持期间不得触发"
        )
        assert "setPanelCleared(false, { render: false })" in body

    def test_external_restore_rejected_while_held(self):
        """保持状态下浮窗/其他标签页发来的恢复显示必须被拒绝。"""

        match = re.search(r"function applyExternalUiState\(type, value\) \{(.*?)\n\}", APP_JS, re.DOTALL)
        assert match, "找不到 applyExternalUiState"
        body = match.group(1)
        cleared = re.search(r'type === "panel-cleared"\) \{(.*?)\} else if', body, re.DOTALL)
        assert cleared
        assert "state.panelHeld && !value" in cleared.group(1), (
            "保持状态下不得接受外部 panel-cleared=false，否则地址事件会弹回"
        )

    def test_initial_state_alignment(self):
        """初始化时 held=true/cleared=false 的异常残留要对齐，否则拦截失效。"""

        assert "if (state.panelHeld && !state.panelCleared)" in APP_JS
        assert "localStorage.setItem(UI_KEYS.panelCleared, \"true\")" in APP_JS

    def test_manual_filters_release_hold(self):
        """主动搜索/筛选/查子网是明确要看事件，应解除保持。"""

        match = re.search(r"const revealPanel = \(\) => \{(.*?)\n\};", APP_JS, re.DOTALL)
        assert match, "找不到 revealPanel"
        assert "setPanelHeld(false" in match.group(1), "主动查询时应解除保持"


class TestFloatingSync:
    def test_floating_reads_and_syncs_hold_state(self):
        """浮窗要认保持状态，且保持期间拒绝外部恢复。"""

        assert 'panelHeld: "subnetPulse.panelHeld"' in FLOATING_JS
        assert "let panelHeld = false;" in FLOATING_JS
        assert "function readPanelHeld()" in FLOATING_JS
        assert "function setPanelHeld(held, options = {})" in FLOATING_JS
        assert 'type === "panel-held") setPanelHeld(' in FLOATING_JS, "BroadcastChannel 未同步保持状态"
        assert 'UI_KEYS.panelHeld) setPanelHeld(' in FLOATING_JS, "storage 事件未同步保持状态"

    def test_floating_rejects_restore_while_held(self):
        """浮窗 setPanelCleared 在保持状态下拒绝恢复，除非显式 release。"""

        match = re.search(r"function setPanelCleared\(value, options = \{\}\) \{(.*?)\n\}", FLOATING_JS, re.DOTALL)
        assert match, "找不到浮窗 setPanelCleared"
        assert "panelHeld && !cleared" in match.group(1), "浮窗保持状态下不得无条件恢复显示"
        assert "options.release !== true" in match.group(1), "显式解除保持时允许恢复"
        restore = re.search(r'restoreHome\.addEventListener\("click", \(\) => \{(.*?)\n\}\);', FLOATING_JS, re.DOTALL)
        assert restore, "找不到浮窗恢复按钮处理"
        assert "setPanelHeld(false)" in restore.group(1)
        assert "release: true" in restore.group(1)


class TestDocsAndVersionSynced:
    def test_static_asset_versions_match_version_file(self):
        """三处静态资源 ?v= 必须跟 VERSION 一致，否则浏览器拿旧缓存。"""

        for name in ("index.html", "floating.html"):
            html = (STATIC / name).read_text(encoding="utf-8")
            tags = re.findall(r"\?v=([0-9][0-9.]*)", html)
            assert tags, f"{name} 没有 ?v= 版本标记"
            assert all(tag == VERSION for tag in tags), f"{name} 的版本 {tags} 与 VERSION {VERSION} 不一致"

        main_html = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
        tags = re.findall(r"/static/(?:styles\.css|app\.js)\?v=([0-9][0-9.]*)", main_html)
        assert tags and all(tag == VERSION for tag in tags), "主页面 ?v= 未跟 VERSION 同步"

        product = (ROOT / "app" / "event_monitor" / "config" / "product_info.json").read_text(encoding="utf-8")
        assert f'"version": "{VERSION}"' in product, "product_info.json 版本未同步"

    def test_rule_document_records_hold_behavior(self):
        """规则文档必须记录新口径，避免下次又把自动恢复改回来。"""

        assert "清空显示可保存" in RULES, "项目规则未记录清空保持口径"
        assert "panelHeld" in RULES, "项目规则未记录保持状态的存储键"
