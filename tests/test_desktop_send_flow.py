"""Exercise actual browser send functions across asynchronous receipt transitions."""
import json
import re
import subprocess
import unittest
from codex_controller.desktop_dashboard import DESKTOP_DASHBOARD_JS


def function(name):
    start = re.search(r'^(?:async )?function '+name+r'\(', DESKTOP_DASHBOARD_JS, re.M).start()
    end = re.search(r'^function |^async function ', DESKTOP_DASHBOARD_JS[start + 1:], re.M)
    return DESKTOP_DASHBOARD_JS[start:start + 1 + end.start()]


class SendFlowTests(unittest.TestCase):
    def run_js(self, body):
        script = '''const assert = require('node:assert/strict');
const elements = new Map(); function q(id) { if (!elements.has(id)) elements.set(id, {value:'hello',textContent:'',className:'',disabled:false}); return elements.get(id); }
const detail = {thread_ref:'TH-test',thread_revision:7,active_turn_ref:null,status:'idle',control_state:'ready'};
const state = {detail,selectedThread:'TH-test',drafts:{'TH-test':'hello'},csrf:'fixture'};
const imageState = {pending:{},busy:{},drafts:{'TH-test':[{image_ref:'IM-test',url:'blob:test'}]}};
const currentAttachments = ref => imageState.drafts[ref || state.selectedThread] || [];
const resizeComposer = () => {}; const renderComposer = () => {}; const API = '/api/desktop/v1';
let nextId=0; const requestId = () => 'request-' + ++nextId;
const window = {setTimeout: callback => callback()};
'''
        script += '\n'.join(function(name) for name in ['prepareSend','commandFeedback','reconcileComposerReceipt','submitAction'])
        script += '\n(async()=>{'+body+'})().catch(error=>{console.error(error);process.exit(1)});'
        subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)

    def test_first_send_refreshes_then_sends_once_and_preserves_images_until_confirmed(self):
        self.run_js('''
const requests=[]; let receipt;
global.jsonFetch = async (path, options) => { requests.push(path); if(path.endsWith('/read')) {receipt={request_id:JSON.parse(options.body).request_id,state:'confirmed',action:'read'};return receipt;} if(options) {receipt={request_id:JSON.parse(options.body).request_id,state:'submitted',action:'continue'};return receipt;} return {...detail,latest_command:receipt};};
global.loadThread = async () => {state.detail={...detail,latest_command:receipt};reconcileComposerReceipt(state.detail)};
await submitAction('continue',{input:'hello',image_refs:['IM-test']});
assert.equal(requests.filter(path=>path.endsWith('/continue')).length,1);
assert.equal(requests[0].endsWith('/read'),true);
assert.equal(q('composerInput').value,'hello');assert.equal(currentAttachments().length,1);
state.detail.latest_command={...receipt,state:'confirmed'};reconcileComposerReceipt(state.detail);
assert.equal(q('composerInput').value,'');assert.equal(currentAttachments().length,0);assert.equal(imageState.pending['TH-test'],undefined);
''')

    def test_conflict_unlocks_preserves_draft_and_unknown_does_not_resend(self):
        self.run_js('''
const pending={body:{request_id:'write-1',input:'hello',image_refs:['IM-test']},action:'continue'};
imageState.pending['TH-test']=pending;
reconcileComposerReceipt({...detail,latest_command:{request_id:'write-1',action:'continue',state:'unknown'}});
assert.equal(imageState.pending['TH-test'],pending);assert.match(q('composerFeedback').textContent,/尚未确认/);
reconcileComposerReceipt({...detail,latest_command:{request_id:'write-1',action:'continue',state:'conflict'}});
assert.equal(imageState.pending['TH-test'],undefined);assert.equal(q('composerInput').value,'hello');assert.equal(currentAttachments().length,1);assert.match(q('composerFeedback').textContent,/消息未发送/);
''')

    def test_real_thread_change_during_refresh_prevents_write(self):
        self.run_js('''
let writes=0;
global.jsonFetch = async (path,options) => {if(options && !path.endsWith('/read')) writes++;return options ? {} : {...detail,thread_revision:8,latest_command:{request_id:'request-2',action:'read',state:'confirmed'}}};
global.loadThread = async () => {};
await submitAction('continue',{input:'hello'});
assert.equal(writes,0);assert.equal(imageState.pending['TH-test'],undefined);assert.equal(q('composerInput').value,'hello');assert.match(q('composerFeedback').textContent,/任务状态已变化/);
''')
