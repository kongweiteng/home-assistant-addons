"""Guard management controls and exercise directory behavior without a backend."""

import ast
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import unittest

from codex_controller.controller_shell import build_management_html
from codex_controller.runner_dashboard import DASHBOARD_JS


class Nodes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.append(values["id"])


class ManagementShellTests(unittest.TestCase):
    def test_transform_preserves_audited_controls_and_moves_status_outside_views(self):
        source = Path(__file__).resolve().parents[1] / "codex_controller/codex_controller/api.py"
        original = next(node.value.value for node in ast.parse(source.read_text()).body
                        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                        and any(isinstance(target, ast.Name) and target.id == "DASHBOARD_HTML" for target in node.targets))
        updated = build_management_html(original)
        before, after = Nodes(), Nodes()
        before.feed(original)
        after.feed(updated)
        self.assertFalse(set(before.ids) - set(after.ids))
        self.assertTrue(all(count == 1 for count in Counter(after.ids).values()))
        self.assertLess(updated.index('id="statusStreamState"'), updated.index('<main'))
        self.assertNotIn('class="side-rail"', updated)
        self.assertNotIn('class="stream-state good">状态自动更新', updated)
        self.assertIn('id="toolRows" class="tool-directory"', updated)
        self.assertIn('id="runnerSetup" class="management-disclosure"><summary>', updated)
        self.assertIn('href="desktop/?new=1"', updated)

    @unittest.skipUnless(shutil.which("node"), "Node required for browser logic checks")
    def test_directory_search_paging_and_expansion_survive_unchanged_stream(self):
        # Execute the actual renderer against a minimal DOM, never calling an API.
        logic = DASHBOARD_JS.split("q('runnerForm').onsubmit", 1)[0]
        stub = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.dataset={}; this.value='all'; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children=[]; }
  setAttribute() {}
  focus() {}
}
const nodes = new Map();
const document = {getElementById(id) {if(!nodes.has(id)) nodes.set(id,new Element()); return nodes.get(id);}, createElement(tag) {return new Element(tag);}};
const window = {location:{hash:'',search:'?view=tools'}};
"""
        checks = r"""
assert.equal(selectedView(),'tools');
window.location.hash='#runners'; assert.equal(selectedView(),'runners');
q('toolSearch').value='';
catalog={policy_error:null,tools:Array.from({length:53},(_,i)=>({name:'tool_'+i,display_name:'工具 '+i,service:i<30?'renovation_hub':'ha_operations_broker',risk_type:'read_only',intent_examples:['查询 '+i],configured:true,enabled:true,mcp_published:true,callable:true}))};
const cards=()=>q('toolRows').children.filter(node=>node.tag==='details');
renderTools();
assert.equal(cards().length,12); assert.equal(q('loadMoreTools').hidden,false);
const first=cards()[0]; first.open=true;first.ontoggle();
renderTools(); assert.equal(cards()[0],first);
toolVisibleCount+=12; renderTools(); assert.equal(cards().length,24);assert.equal(cards()[0].open,true);
q('toolSearch').value='查询 52'; toolVisibleCount=12;renderTools();
assert.equal(cards().length,1);assert.equal(cards()[0].dataset.toolName,'tool_52');assert.equal(q('loadMoreTools').hidden,true);
q('toolSearch').value='does-not-exist';renderTools();assert.equal(cards().length,0);
q('toolSearch').value='';q('serviceFilter').value='renovation_hub';q('riskFilter').value='write';renderTools();assert.equal(cards().length,0);
"""
        result = subprocess.run([shutil.which("node"), "-"], input=stub + logic + checks,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
