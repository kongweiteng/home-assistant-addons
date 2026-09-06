"""Preserve interactive controls while composing the responsive task shell."""

from collections import Counter
from html.parser import HTMLParser
import json
import re
import subprocess
import unittest

from codex_controller.desktop_dashboard import DESKTOP_DASHBOARD_HTML, DESKTOP_DASHBOARD_JS


def javascript_function(source, name):
    start = source.index(f"function {name}(")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"JavaScript function {name} is incomplete")


def media_blocks(css, query):
    marker = f"@media({query})"
    offset = 0
    blocks = []
    while True:
        start = css.find(marker, offset)
        if start < 0:
            return blocks
        opening = css.index("{", start + len(marker))
        depth = 0
        for index in range(opening, len(css)):
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(css[opening + 1 : index])
                    offset = index + 1
                    break
        else:
            raise AssertionError(f"CSS media block {query} is incomplete")


class LayoutNodes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.attributes = {}
        self.mobile_navigation = []
        self.in_mobile_navigation = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.append(values["id"])
            self.attributes[values["id"]] = (tag, values)
        if tag == "nav" and values.get("class") == "mobile-nav":
            self.in_mobile_navigation = True
        elif self.in_mobile_navigation and tag in {"a", "button"}:
            self.mobile_navigation.append((tag, values))

    def handle_endtag(self, tag):
        if tag == "nav":
            self.in_mobile_navigation = False


class DesktopResponsiveLayoutTests(unittest.TestCase):
    def setUp(self):
        self.nodes = LayoutNodes()
        self.nodes.feed(DESKTOP_DASHBOARD_HTML)

    def test_navigation_transform_keeps_unique_live_control_targets(self):
        self.assertTrue(all(count == 1 for count in Counter(self.nodes.ids).values()))
        for target in ("mobileProjects", "projectScope", "projectPanel", "closeProjects",
                       "mobileNewTask", "mobileConnection", "composer", "composerInput",
                       "conversationView", "connectionState", "taskMenu", "threadList",
                       "connectionBridgeHealth", "connectionErrorPanel",
                       "connectionErrorCode", "copyConnectionError"):
            self.assertIn(target, self.nodes.attributes)
        self.assertEqual(len(self.nodes.mobile_navigation), 5)
        hrefs = {attributes.get("href") for _, attributes in self.nodes.mobile_navigation}
        self.assertTrue({"./", "../?view=tools", "../?view=overview"}.issubset(hrefs))
        self.assertIn("../?view=runners", DESKTOP_DASHBOARD_HTML)
        self.assertIn('href="../?view=errors#errors"', DESKTOP_DASHBOARD_HTML)

    def test_connection_sheet_distinguishes_activation_from_health(self):
        self.assertIn("最近激活尝试", DESKTOP_DASHBOARD_HTML)
        self.assertIn("最近交给系统", DESKTOP_DASHBOARD_HTML)
        self.assertIn("最近健康确认", DESKTOP_DASHBOARD_HTML)
        self.assertNotIn("Bridge 最近成功", DESKTOP_DASHBOARD_HTML)

    def test_bridge_error_code_is_visible_and_copyable(self):
        script = "\n".join(
            (
                "Object.defineProperty(globalThis, 'navigator', {value: {onLine: true, clipboard: {writeText: async value => { globalThis.copied = value; }}}, configurable: true});",
                "const state = {overviewStreamState: 'open', streamFailures: new Set(), selectedThread: '', lastOverviewFrameAt: Date.now() - 1000, overviewError: '', overviewReconnectAttempt: 0};",
                "const host = {online: true, connection_observed_at: '2026-09-07T00:00:00Z', data_synced_at: '2026-09-07T00:00:00Z', app_bridge: {ready: false, recovery_attempt_count: 2, retry_seconds: 3, last_attempt: '2026-09-07T00:00:00Z', last_activation_success: '2026-09-07T00:00:00Z', last_success: null, last_error_code: 'bridge_unavailable', supervisor_error_code: 'bridge_activation_failed'}};",
                "const nodes = {};",
                "function q(id) { return nodes[id] ||= {className: '', textContent: '', disabled: false, focus() {}}; }",
                "function formatTime(value) { return value ? '已记录' : '时间未知'; }",
                "function currentHost() { return host; }",
                "function estimatedServerNow() { return Date.parse('2026-09-07T00:00:05Z'); }",
                "const document = {createRange() { throw new Error('fallback should not run'); }};",
                "const window = {};",
                javascript_function(DESKTOP_DASHBOARD_JS, "setConnection"),
                javascript_function(DESKTOP_DASHBOARD_JS, "freshness"),
                javascript_function(DESKTOP_DASHBOARD_JS, "renderFreshness"),
                javascript_function(DESKTOP_DASHBOARD_JS, "copyConnectionError"),
                "(async () => { renderFreshness(); await copyConnectionError(); process.stdout.write(JSON.stringify({bridge: nodes.connectionBridge.textContent, panel: nodes.connectionErrorPanel.className, code: nodes.connectionErrorCode.textContent, copied: globalThis.copied, activation: nodes.connectionBridgeSuccess.textContent, health: nodes.connectionBridgeHealth.textContent, copyState: nodes.connectionErrorCopyState.textContent})); })();",
            )
        )
        completed = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertIn("激活尝试 2 次", result["bridge"])
        self.assertNotIn("已恢复", result["bridge"])
        self.assertEqual(result["panel"], "connection-error")
        self.assertEqual(
            result["code"],
            "bridge_unavailable\nbridge_activation_failed",
        )
        self.assertEqual(result["copied"], result["code"])
        self.assertEqual(result["activation"], "已记录")
        self.assertEqual(result["health"], "时间未知")
        self.assertEqual(result["copyState"], "错误码已复制。")

    def test_image_selection_and_preview_have_separate_non_submit_controls(self):
        for target in ("addImage", "closeImage"):
            tag, attrs = self.nodes.attributes[target]
            self.assertEqual((tag, attrs.get("type")), ("button", "button"))
        _, picker = self.nodes.attributes["imageInput"]
        self.assertEqual(picker["type"], "file")
        self.assertIn("multiple", picker)
        self.assertEqual(set(picker["accept"].split(",")), {"image/png", "image/jpeg", "image/webp"})
        _, preview = self.nodes.attributes["imageDialog"]
        self.assertEqual(preview.get("role"), "dialog")
        self.assertEqual(preview.get("aria-modal"), "true")

    def test_responsive_override_follows_legacy_grid_rules(self):
        # The two-column override must win over the old three-column layout at
        # tablet widths; the old 920px mobile switch must not remain active.
        css = DESKTOP_DASHBOARD_HTML.split("<style>", 1)[1].split("</style>", 1)[0]
        self.assertNotIn("max-width:920px", css)
        self.assertGreater(css.rindex("grid-template-columns:350px minmax(0,1fr)"),
                           css.index("grid-template-columns:220px 330px minmax(420px,1fr)"))
        self.assertIn("@media(min-width:760px)", css)
        self.assertIn("@media(max-width:759px)", css)

    def test_mobile_task_menu_stays_above_conversation_and_targets_are_touch_safe(self):
        css = DESKTOP_DASHBOARD_HTML.split("<style>", 1)[1].split("</style>", 1)[0]
        self.assertIn(".detail-head{z-index:40;overflow:visible}", css)
        self.assertIn(".task-menu[open]{z-index:110}", css)
        self.assertIn(".task-menu-popover{z-index:111}", css)
        self.assertIn(".top-actions .connection,.project-trigger,.task-menu summary,.task-menu-popover button", css)
        self.assertIn(".sheet-actions button,.resource-tabs button", css)
        self.assertIn("{min-height:44px}", css)

    def test_open_realtime_link_does_not_mask_stale_task_inventory(self):
        script = "\n".join(
            (
                "Object.defineProperty(globalThis, 'navigator', {value: {onLine: true}, configurable: true});",
                "const state = {overviewStreamState: 'open', streamFailures: new Set(), selectedThread: '', lastOverviewFrameAt: Date.now() - 1000, overviewError: '', overviewReconnectAttempt: 0};",
                "const host = {online: true, capabilities: ['desktop_host_v1'], connection_observed_at: '2026-09-07T00:00:00Z', data_synced_at: '2026-09-06T23:50:00Z', data_age_seconds: 605, data_freshness_state: 'stale'};",
                "const nodes = {};",
                "function q(id) { return nodes[id] ||= {className: '', textContent: ''}; }",
                "function formatTime(value) { return value ? '已记录' : '未知'; }",
                "function currentHost() { return host; }",
                "function estimatedServerNow() { return Date.parse('2026-09-07T00:00:05Z'); }",
                javascript_function(DESKTOP_DASHBOARD_JS, "setConnection"),
                javascript_function(DESKTOP_DASHBOARD_JS, "freshness"),
                javascript_function(DESKTOP_DASHBOARD_JS, "renderFreshness"),
                "renderFreshness();",
                "process.stdout.write(JSON.stringify({freshness: freshness(), connection: nodes.connectionState, hostMeta: nodes.hostMeta, detail: nodes.detailSyncState, note: nodes.connectionNote}));",
            )
        )
        completed = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertIn("任务目录同步过期", result["freshness"]["label"])
        self.assertEqual(result["freshness"]["kind"], "bad")
        self.assertGreater(result["freshness"]["dataSeconds"], 30)
        self.assertIn("任务目录同步过期", result["connection"]["textContent"])
        self.assertIn("bad", result["connection"]["className"])
        self.assertIn("任务目录同步过期", result["detail"]["textContent"])
        self.assertIn("任务目录同步", result["hostMeta"]["textContent"])
        self.assertIn("任务目录恢复", result["note"]["textContent"])

    def test_host_v1_without_inventory_watermark_is_not_shown_as_fresh(self):
        script = "\n".join(
            (
                "Object.defineProperty(globalThis, 'navigator', {value: {onLine: true}, configurable: true});",
                "const state = {overviewStreamState: 'open', streamFailures: new Set(), selectedThread: '', lastOverviewFrameAt: Date.now() - 1000};",
                "const host = {online: true, capabilities: ['desktop_host_v1'], connection_observed_at: '2026-09-07T00:00:00Z', synced_at: '2026-09-07T00:00:00Z', data_synced_at: null, data_age_seconds: null, data_freshness_state: 'unknown'};",
                "function currentHost() { return host; }",
                "function estimatedServerNow() { return Date.parse('2026-09-07T00:00:05Z'); }",
                javascript_function(DESKTOP_DASHBOARD_JS, "freshness"),
                "process.stdout.write(JSON.stringify(freshness()));",
            )
        )
        completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["label"], "链路在线 · 任务目录尚未同步")
        self.assertEqual(result["kind"], "bad")

    def test_390px_load_more_threads_is_a_44px_touch_target(self):
        css = DESKTOP_DASHBOARD_HTML.split("<style>", 1)[1].split("</style>", 1)[0]
        mobile_css = "\n".join(media_blocks(css, "max-width:759px"))
        self.assertRegex(
            mobile_css,
            re.compile(
                r"[^{}]*(?:#loadMoreThreads|\.thread-list-footer\s+button)[^{}]*"
                r"\{[^{}]*min-height:\s*44px"
            ),
        )


if __name__ == "__main__":
    unittest.main()
