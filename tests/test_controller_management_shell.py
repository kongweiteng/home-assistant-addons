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
        # Actual browser logic, deterministic fetch promises and no external services.
        logic = DASHBOARD_JS.split("q('runnerForm').onsubmit", 1)[0]
        stub = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.dataset={}; this.value='all'; this.classList={toggle(){}}; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children=[]; }
  setAttribute() {}
  focus() {}
}
const nodes = new Map();
const document = {visibilityState:'visible',querySelectorAll(){return [];},getElementById(id) {if(!nodes.has(id)) nodes.set(id,new Element()); return nodes.get(id);}, createElement(tag) {return new Element(tag);}};
const window = {location:{hash:'',search:'?view=tools'},clearTimeout,setTimeout,scrollTo(){}};
const crypto = require('node:crypto').webcrypto;
const requests=[];
let fetch = (path,options={})=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject}));
const respond=(request,result)=>request.resolve({ok:true,json:async()=>({result})});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
"""
        checks = r"""
(async()=>{
assert.equal(selectedView(),'tools');
window.location.hash='#runners'; assert.equal(selectedView(),'runners');
q('toolSearch').value='';
const all=Array.from({length:53},(_,i)=>({name:'tool_'+i,display_name:'工具 '+i,service:i<30?'renovation_hub':'ha_operations_broker',risk_type:'read_only',intent_examples:['查询 '+i],configured:true,enabled:true,mcp_published:true,callable:true}));
const page=(offset=0,tools=all.slice(offset,offset+12),total=53)=>({revision:17,catalog_revision:'revA',policy_error:null,summary:{known:53,published:53},tools,page:{offset,limit:12,total,callable:total,previous_offset:offset?offset-12:null,next_offset:offset+12<total?offset+12:null}});
const cards=()=>q('toolRows').children.filter(node=>node.tag==='details');
observeToolRevision({tools:{catalog_revision:'revA'}});
await refreshTools();assert.equal(requests.length,0); // inactive view never prefetches
window.location.hash='#tools';
const firstLoad=refreshTools();assert.equal(requests.length,1);
assert.match(requests[0].path,/api\/tools\?limit=12&offset=0/);
const samePending=refreshTools();assert.equal(requests.length,1);
respond(requests[0],page());await firstLoad;await samePending;
assert.equal(cards().length,12); assert.equal(q('loadMoreTools').hidden,false);
const first=cards()[0]; first.open=true;first.ontoggle();
observeToolRevision({tools:{catalog_revision:'revA',mcp:{observed_at:'later'}}});
await refreshTools();renderTools();assert.equal(requests.length,1);assert.equal(cards()[0],first);
const next=refreshTools({offset:12});assert.match(requests[1].path,/offset=12/);
respond(requests[1],page(12));await next;assert.equal(cards().length,12);assert.equal(cards()[0].dataset.toolName,'tool_12');
const prev=refreshTools({offset:0});respond(requests[2],page());await prev;assert.equal(cards()[0].open,true);
// The second query resolves first; a late first-query result must be ignored even if abort is ignored.
q('toolSearch').value='old';resetToolFilter();const old=requests[3];
q('toolSearch').value='查询 52';resetToolFilter();const latest=requests[4];
assert.equal(old.options.signal.aborted,true);
assert.equal(new URL('http://local/'+latest.path).searchParams.get('search'),'查询 52');
respond(latest,page(0,[all[52]],1));await tick();
assert.equal(cards().length,1);assert.equal(cards()[0].dataset.toolName,'tool_52');assert.equal(q('loadMoreTools').hidden,true);
respond(old,page());await tick();assert.equal(cards().length,1);
// Revision notifications invalidate only the current bounded page and coalesce duplicates.
observeToolRevision({tools:{catalog_revision:'revB'}});const revisionLoad=refreshTools();
observeToolRevision({tools:{catalog_revision:'revB'}});const duplicate=refreshTools();assert.equal(requests.length,6);
respond(requests[5],page(0,[all[52]],1));await revisionLoad;await duplicate;
window.location.hash='#overview';observeToolRevision({tools:{catalog_revision:'revC'}});
await refreshTools();assert.equal(requests.length,6);
window.location.hash='#tools';const back=refreshTools();respond(requests[6],page(0,[all[52]],1));await back;
// Filtering happens on the server, including service/type; zero results remain a valid page.
q('toolSearch').value='';q('serviceFilter').value='renovation_hub';q('riskFilter').value='write';resetToolFilter();
assert.match(requests[7].path,/service=renovation_hub&risk=write/);
respond(requests[7],page(0,[],0));await tick();assert.equal(cards().length,0);
// Failed requests do not retry on every status notification; explicit retry still works.
const failed=refreshTools({force:true});requests[8].reject(new Error('offline'));await failed;
await refreshTools();assert.equal(requests.length,9);
const retry=refreshTools({force:true});respond(requests[9],page());await retry;
// A policy mutation continues to send the integer CAS revision, never the catalog fingerprint.
const mutation=setTool(all[0],false,new Element('button'));
assert.equal(requests[10].options.method,'PATCH');assert.equal(JSON.parse(requests[10].options.body).revision,17);
respond(requests[10],{revision:18});await tick();assert.match(requests[11].path,/api\/tools\?limit=12/);
respond(requests[11],page());await mutation;
// Navigation invalidates pending responses; returning loads the needed page again.
const pending=refreshTools({force:true});window.location.hash='#runners';activateView({scroll:false});
respond(requests[12],page(0,[all[52]],1));await pending;assert.equal(cards().length,12);
window.location.hash='#tools';const reopened=refreshTools();respond(requests[13],page());await reopened;
document.visibilityState='hidden';observeToolRevision({tools:{catalog_revision:'revD'}});
await refreshTools();assert.equal(requests.length,14);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-"], input=stub + logic + checks,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
