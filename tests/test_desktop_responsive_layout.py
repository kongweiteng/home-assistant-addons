"""Preserve interactive controls while composing the responsive task shell."""

from collections import Counter
from html.parser import HTMLParser
import unittest

from codex_controller.desktop_dashboard import DESKTOP_DASHBOARD_HTML


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
                       "conversationView", "connectionState", "taskMenu", "threadList"):
            self.assertIn(target, self.nodes.attributes)
        self.assertEqual(len(self.nodes.mobile_navigation), 5)
        hrefs = {attributes.get("href") for _, attributes in self.nodes.mobile_navigation}
        self.assertTrue({"./", "../?view=tools", "../?view=overview"}.issubset(hrefs))
        self.assertIn("../?view=runners", DESKTOP_DASHBOARD_HTML)

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


if __name__ == "__main__":
    unittest.main()
