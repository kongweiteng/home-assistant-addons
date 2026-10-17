from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import threading
import unittest

from codex_controller.app_server import AppServerClient, AppServerError
from codex_controller.media_input import TurnMediaManager
from codex_controller.service import ControllerService
from codex_controller.store import ControllerStore, StoreError
from codex_controller.tool_proxy import ToolRouter, ToolProxyError


def message(number: int, text: str = '补充说明', profile: str = 'owner') -> dict:
    return {'version':1,'message_id':f'message-{number:04}', 'conversation_key':'sha256:'+'a'*64,
            'received_at':f'2026-10-17T12:00:{number % 60:02}+08:00','text':text,'attachments':[],
            'capability_profile':profile,'reply_capabilities':['text','image']}


class FakeApp:
    account_ready = True
    auth_mode = 'chatgpt'
    def __init__(self):
        self.steers = []
        self.starts = []
        self.interrupts = []
        self.on_steer = None
        self.on_interrupt = None
    def status(self):
        return {'running':True,'initialized':True,'protocol_error':None,'account':{'auth_mode':'chatgpt'}}
    def start_thread(self):
        return 'thread-1'
    def resume_thread(self, thread_id):
        return thread_id
    def start_turn(self, thread_id, text, message_id, **kwargs):
        self.starts.append((thread_id,text,message_id,kwargs))
        return f'turn-{len(self.starts)}'
    def steer_turn(self, thread_id, turn_id, text, message_id, **kwargs):
        self.steers.append((thread_id,turn_id,text,message_id,kwargs))
        if self.on_steer:
            return self.on_steer()
        return turn_id
    def interrupt_turn(self, thread_id, turn_id):
        self.interrupts.append((thread_id,turn_id))
        if self.on_interrupt:
            self.on_interrupt()


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ControllerStore(self.root/'controller.sqlite3')
        self.app = FakeApp()
        self.service = ControllerService(self.store,self.app,intake_enabled=True)
    def tearDown(self):
        self.tmp.cleanup()
    def start_parent(self, payload=None, *, router=False):
        if router:
            self.router = ToolRouter(store=self.store)
            self.service.tool_context = self.router
        parent = self.store.create_job(payload or message(1,'原问题'))
        self.service._dispatch(self.store.claim_next())
        return self.store.get_job(parent['job_id'])
    def child(self, payload=None):
        return self.store.create_job(payload or message(2))
    def complete(self, status='completed', text='综合答复'):
        if text:
            self.service.handle_notification({'method':'item/completed','params':{'turnId':'turn-1','item':{'type':'agentMessage','text':text}}})
        self.service.handle_notification({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'id':'turn-1','status':status}}})
    def test_append_accepted_one_receipt_and_message_idempotency(self):
        parent = self.start_parent()
        child = self.child()
        self.assertTrue(self.service._append_active_turn())
        self.assertEqual(len(self.app.steers),1)
        self.assertEqual(self.store.create_job(message(2))['job_id'],child['job_id'])
        self.assertFalse(self.service._append_active_turn())
        self.assertIsNone(self.store.claim_next())
        self.complete()
        result = self.store.get_public_job(child['job_id'])
        self.assertEqual(result['followup_parent_job_id'],parent['job_id'])
        self.assertEqual(result['state'],'completed')
        self.assertTrue(result['reply_suppressed'])
        self.assertFalse(self.store.get_public_job(parent['job_id'])['reply_suppressed'])
    def test_terminal_during_rpc_is_deferred_until_acceptance(self):
        parent = self.start_parent()
        child = self.child()
        def race():
            self.complete()
            self.assertEqual(self.store.get_job(parent['job_id'])['state'],'running')
            return 'turn-1'
        self.app.on_steer = race
        self.service._append_active_turn()
        self.assertEqual(self.store.get_job(parent['job_id'])['state'],'completed')
        self.assertTrue(self.store.get_public_job(child['job_id'])['reply_suppressed'])
    def test_parent_finishes_before_atomic_claim_no_late_input(self):
        parent = self.start_parent()
        child = self.child()
        self.complete()
        self.assertFalse(self.store.claim_followup(parent['job_id'],child['job_id'],'turn-1'))
        self.assertFalse(self.service._append_active_turn())
        self.assertEqual(self.app.steers,[])
    def test_unknown_delivery_blocks_replay_and_remains_visible(self):
        self.start_parent()
        child = self.child()
        def uncertain():
            raise AppServerError('request_timeout','timeout')
        self.app.on_steer = uncertain
        self.service._append_active_turn()
        self.complete()
        public = self.store.get_public_job(child['job_id'])
        self.assertEqual(public['state'],'recovery_required')
        self.assertEqual(public['followup_state'],'unknown')
        self.assertFalse(public['reply_suppressed'])
        self.assertIsNone(self.store.claim_next())
        self.store.recover_running()
        self.assertFalse(self.service._append_active_turn())
        self.assertEqual(len(self.app.steers),1)
    def test_definitive_rejection_preserves_original_input_for_next_turn(self):
        self.start_parent()
        child = self.child()
        self.app.on_steer = lambda: (_ for _ in ()).throw(AppServerError('refused','refused',definitive=True,rpc_code=-32602))
        self.service._append_active_turn()
        self.assertEqual(self.store.get_job(child['job_id'])['state'],'queued')
        self.complete()
        self.service._dispatch(self.store.claim_next())
        self.assertEqual(self.app.starts[-1][1],'补充说明')
    def test_restarted_attempt_becomes_unknown_not_replayed(self):
        parent = self.start_parent()
        child = self.child()
        self.assertTrue(self.store.claim_followup(parent['job_id'],child['job_id'],'turn-1'))
        restored = ControllerStore(self.root/'controller.sqlite3')
        restored.recover_running()
        self.assertEqual(restored.get_job(child['job_id'])['followup_state'],'unknown')
        self.assertEqual(restored.get_job(child['job_id'])['state'],'recovery_required')
        self.assertIsNone(restored.claim_next())
    def test_cross_conversation_and_profile_cannot_be_linked_even_directly(self):
        parent = self.start_parent()
        other = message(2,profile='member_read_only')
        child = self.child(other)
        self.assertFalse(self.service._append_active_turn())
        self.assertFalse(self.store.claim_followup(parent['job_id'],child['job_id'],'turn-1'))
        other['conversation_key']='sha256:'+'b'*64
        other['message_id']='different'
        child = self.child(other)
        self.assertFalse(self.store.claim_followup(parent['job_id'],child['job_id'],'turn-1'))
    def test_control_messages_and_archive_escalation_remain_queued(self):
        for text in ['/new','打开新会话','打开备车','提醒我明天8点取件','查看未完成备忘录']:
            with self.subTest(text=text):
                self.assertTrue(self.service._followup_control_barrier(message(2,text)))
        parent_payload = message(1,'看图片')
        parent_payload['attachments']=[{'attachment_ref':'x'*32,'media_type':'image','size_bytes':3,'sha256':'sha256:'+'a'*64}]
        self.start_parent(parent_payload)
        child = self.child(message(2,'把这个图片归档到装修档案'))
        self.assertTrue(self.service._append_active_turn())
        self.assertEqual(self.store.get_job(child['job_id'])['state'],'queued')
        queued = self.store.continuation_input(child['job_id'],message(2,'把这个图片归档到装修档案'))
        self.assertEqual(queued['attachments'],parent_payload['attachments'])
        self.assertIn('不得重复',queued['text'])
    def test_accepted_failed_parent_is_not_silently_suppressed(self):
        self.start_parent()
        child = self.child()
        self.service._append_active_turn()
        self.complete('failed','')
        public = self.store.get_public_job(child['job_id'])
        self.assertEqual(public['state'],'recovery_required')
        self.assertFalse(public['reply_suppressed'])
    def test_child_acceptance_disables_automatic_parent_retry(self):
        parent = self.start_parent()
        self.child()
        self.service._append_active_turn()
        self.store.complete_turn('turn-1','failed',retryable=True,retry_delay_seconds=0)
        self.assertEqual(self.store.get_job(parent['job_id'])['state'],'failed')
    def test_no_write_fallback_waits_for_terminal_before_continuing_same_thread(self):
        parent = self.start_parent(router=True)
        child = self.child()
        self.app.on_steer=lambda: (_ for _ in ()).throw(AppServerError('unsupported','unsupported',rpc_code=-32601,definitive=True))
        self.service._append_active_turn()
        self.assertEqual(len(self.app.interrupts),1)
        self.assertEqual(self.store.get_job(child['job_id'])['followup_state'],'interrupt_wait')
        self.assertIsNone(self.store.claim_next())
        self.complete('interrupted','')
        public = self.store.get_public_job(parent['job_id'])
        self.assertEqual(public['followup_state'],'continuation')
        self.assertTrue(public['reply_suppressed'])
        self.service._dispatch(self.store.claim_next())
        self.assertEqual(self.app.starts[-1][0],'thread-1')
        self.assertIn('原问题',self.app.starts[-1][1])
        self.assertIn('补充说明',self.app.starts[-1][1])
    def test_started_successful_and_unknown_write_each_prohibits_fallback_interrupt(self):
        parent=self.start_parent(router=True)
        child=self.child()
        evidence=self.store.start_write_evidence(parent['job_id'],'turn-1','ledger_create')
        self.assertFalse(self.store.can_interrupt_for_followup(parent['job_id'],child['job_id']))
        self.app.on_steer=lambda: (_ for _ in ()).throw(AppServerError('unsupported','unsupported',rpc_code=-32601,definitive=True))
        self.service._append_active_turn()
        self.assertEqual(self.app.interrupts,[])
        self.assertEqual(self.store.get_job(child['job_id'])['state'],'queued')
        self.store.finish_write_evidence(evidence,{'result':{'id':'entry-1','amount':23}})
        self.assertFalse(self.store.can_interrupt_for_followup(parent['job_id'],child['job_id']))
        self.store.finish_write_evidence(evidence,None)
        self.assertFalse(self.store.can_interrupt_for_followup(parent['job_id'],child['job_id']))
    def test_write_result_is_present_even_if_final_answer_only_answers_supplement(self):
        parent=self.start_parent()
        evidence=self.store.start_write_evidence(parent['job_id'],'turn-1','ledger_create')
        self.store.finish_write_evidence(evidence,{'result':{'entry_id':'entry-1','amount':23},'token':'must-not-retain'})
        self.child()
        self.service._append_active_turn()
        self.complete(text='补充问题的答案')
        result=self.store.get_job(parent['job_id'])['result']
        self.assertIn('执行结果核对',result)
        self.assertIn('entry-1',result)
        self.assertIn('已成功',result)
        self.assertNotIn('must-not-retain',result)
        self.assertIn('entry-1',self.app.steers[0][2])
    def test_media_paths_for_parent_and_child_live_until_turn_terminal(self):
        content=b'fixture-image'
        digest='sha256:'+hashlib.sha256(content).hexdigest()
        metadata={'mime_type':'image/png','size_bytes':len(content),'sha256':digest}
        manager=TurnMediaManager(self.root/'media',lambda _ref:(metadata,content))
        self.service.turn_media=manager
        attachment={'attachment_ref':'x'*32,'media_type':'image','size_bytes':len(content),'sha256':digest}
        original=message(1); original['attachments']=[attachment]
        self.start_parent(original)
        child=message(2); child['attachments']=[attachment]
        self.child(child)
        self.service._append_active_turn()
        paths=list((self.root/'media').rglob('*.png'))
        self.assertEqual(len(paths),2)
        self.assertTrue(all(path.exists() for path in paths))
        self.complete()
        self.assertTrue(all(not path.exists() for path in paths))
    def test_interrupt_timeout_does_not_continue_or_replay(self):
        self.start_parent(router=True)
        child=self.child()
        self.app.on_steer=lambda: (_ for _ in ()).throw(AppServerError('unsupported','unsupported',rpc_code=-32601,definitive=True))
        self.service._append_active_turn()
        self.store.expire_followup_interrupts(0)
        self.assertEqual(self.store.get_job(child['job_id'])['followup_state'],'unknown')
        self.complete('interrupted','')
        self.assertIsNone(self.store.claim_next())
    def test_multiple_accepted_inputs_are_settled_together_without_second_turn(self):
        parent=self.start_parent()
        first=self.child(message(2,'第一条补充'))
        second=self.child(message(3,'第二条补充'))
        self.service._append_active_turn()
        self.service._append_active_turn()
        self.assertEqual(len(self.app.starts),1)
        self.assertEqual(len(self.app.steers),2)
        self.complete()
        for child in (first,second):
            self.assertTrue(self.store.get_public_job(child['job_id'])['reply_suppressed'])
        self.assertEqual(self.store.get_job(parent['job_id'])['state'],'completed')
    def test_fifo_control_barrier_prevents_later_input_overtaking(self):
        self.start_parent()
        self.child(message(2,'/new'))
        self.child(message(3,'下一会话的问题'))
        self.assertFalse(self.service._append_active_turn())
        self.assertEqual(self.app.steers,[])
    def test_atomic_claim_allows_only_one_concurrent_delivery(self):
        parent=self.start_parent()
        child=self.child()
        barrier=threading.Barrier(3)
        results=[]
        def claim():
            barrier.wait()
            results.append(self.store.claim_followup(parent['job_id'],child['job_id'],'turn-1'))
        workers=[threading.Thread(target=claim) for _ in range(2)]
        for worker in workers: worker.start()
        barrier.wait()
        for worker in workers: worker.join(3)
        self.assertEqual(sorted(results),[False,True])
    def test_tool_write_evidence_precedes_external_call_and_prevents_interrupt(self):
        parent=self.start_parent()
        child=self.child()
        entered=threading.Event()
        release=threading.Event()
        class BlockingRouter(ToolRouter):
            def _call_authorized(inner, definition, arguments, invocation_context=None):
                entered.set()
                release.wait(3)
                return {'result':{'id':'entry-proof','amount':20}}
        router=BlockingRouter(store=self.store,ledger_base_url='http://ledger.invalid',ledger_token='l'*32)
        router.begin_job(parent['job_id'],'message-0001','owner',conversation_key=parent['conversation_key'])
        router.bind_turn(parent['job_id'],'turn-1')
        definition=next(item for item in router.tool_definitions() if item.service=='renovation_hub' and not item.read_only and item.transport=='json')
        errors=[]
        def write():
            try: router.call(definition.name,{})
            except Exception as exc: errors.append(exc)
        worker=threading.Thread(target=write)
        worker.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.store.write_evidence(parent['job_id'])[0]['phase'],'started')
        self.assertFalse(router.begin_followup_interrupt(parent['job_id'],child['job_id'],'turn-1'))
        release.set(); worker.join(3)
        self.assertEqual(errors,[])
        self.assertEqual(self.store.write_evidence(parent['job_id'])[0]['phase'],'succeeded')
    def test_interrupt_guard_rejects_new_write_before_any_external_action(self):
        parent=self.start_parent()
        child=self.child()
        calls=[]
        class CountingRouter(ToolRouter):
            def _call_authorized(inner, definition, arguments, invocation_context=None):
                calls.append(definition.name)
                return {'ok':True}
        router=CountingRouter(store=self.store,ledger_base_url='http://ledger.invalid',ledger_token='l'*32)
        router.begin_job(parent['job_id'],'message-0001','owner',conversation_key=parent['conversation_key'])
        router.bind_turn(parent['job_id'],'turn-1')
        self.assertTrue(router.begin_followup_interrupt(parent['job_id'],child['job_id'],'turn-1'))
        definition=next(item for item in router.tool_definitions() if item.service=='renovation_hub' and not item.read_only and item.transport=='json')
        with self.assertRaises(ToolProxyError): router.call(definition.name,{})
        self.assertEqual(calls,[])
        self.assertEqual(self.store.write_evidence(parent['job_id']),[])
    def test_manual_recovery_does_not_suppress_without_confirmed_parent_receipt(self):
        parent=self.start_parent()
        child=self.child()
        self.service._append_active_turn()
        self.store.recover_running()
        self.store.resolve_recovery(child['job_id'],'confirmed_completed')
        self.assertFalse(self.store.get_public_job(child['job_id'])['reply_suppressed'])
    def test_next_turn_archive_context_includes_previously_appended_images(self):
        self.start_parent()
        content=b'proof'
        digest='sha256:'+hashlib.sha256(content).hexdigest()
        image=message(2,'图片在这里')
        image['attachments']=[{'attachment_ref':'z'*32,'media_type':'image','size_bytes':len(content),'sha256':digest}]
        self.child(image)
        self.service._append_active_turn()
        child=self.child(message(3,'归档到装修档案'))
        self.service._append_active_turn()
        queued=self.store.continuation_input(child['job_id'],message(3,'归档到装修档案'))
        self.assertEqual(queued['attachments'],image['attachments'])
    def test_do_not_archive_supplement_revokes_media_tool_gate(self):
        original=message(1,'归档到装修档案')
        original['attachments']=[{'attachment_ref':'x'*32,'media_type':'image','size_bytes':3,'sha256':'sha256:'+'a'*64}]
        parent=self.start_parent(original,router=True)
        self.assertTrue(self.router._active_context['media_archive_authorized'])
        self.child(message(2,'不要归档了，只识别'))
        self.service._append_active_turn()
        self.assertFalse(self.router._active_context['media_archive_authorized'])

    def test_completion_pending_check_and_claim_do_not_interleave(self):
        parent=self.start_parent()
        child=self.child()
        checked=threading.Event()
        release=threading.Event()
        original=self.store.followup_pending
        def paused_check(turn_id):
            result=original(turn_id)
            checked.set()
            release.wait(2)
            return result
        self.store.followup_pending=paused_check
        completer=threading.Thread(target=lambda:self.complete())
        completer.start()
        self.assertTrue(checked.wait(2))
        delivered=[]
        worker=threading.Thread(target=lambda:delivered.append(self.service._append_active_turn()))
        worker.start()
        release.set()
        completer.join(3); worker.join(3)
        self.assertFalse(completer.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.store.get_job(parent['job_id'])['state'],'completed')
        self.assertEqual(self.store.get_job(child['job_id'])['state'],'queued')
        self.assertEqual(self.app.steers,[])
    def test_late_acceptance_after_terminal_settles_child_in_same_transaction(self):
        parent=self.start_parent()
        child=self.child()
        self.store.claim_followup(parent['job_id'],child['job_id'],'turn-1')
        self.store.set_result_text('turn-1','综合结果')
        self.store.complete_turn('turn-1','completed')
        self.store.settle_followups('turn-1','completed')
        self.store.finish_followup_delivery(child['job_id'],'accepted')
        self.assertEqual(self.store.get_job(child['job_id'])['state'],'completed')
        self.assertTrue(self.store.get_public_job(child['job_id'])['reply_suppressed'])
    def test_submitted_unknown_and_failed_business_results_are_not_claimed_successful(self):
        parent=self.start_parent()
        results=[({'status':'submitted'},'submitted'),({'status':'unknown'},'unknown'),
                 ({'status':'failed'},'failed'),({'result':{'operation':{'status':'unknown'}}},'unknown')]
        for index,(result,phase) in enumerate(results):
            evidence=self.store.start_write_evidence(parent['job_id'],'turn-1','operation_'+str(index))
            self.store.finish_write_evidence(evidence,result)
            self.assertEqual(next(item['phase'] for item in self.store.write_evidence(parent['job_id']) if item['tool_name']=='operation_'+str(index)),phase)
        self.child()
        self.service._append_active_turn()
        self.complete(text='操作已受理')
        result=self.store.get_job(parent['job_id'])['result']
        self.assertNotIn('已成功',result)
        self.assertIn('实际执行结果待核验',result)
        self.assertIn('结果待核对',result)
        self.assertIn('明确失败',result)

    def test_restart_after_parent_receipt_settles_accepted_child_without_replay(self):
        parent=self.start_parent()
        child=self.child()
        self.service._append_active_turn()
        self.store.set_result_text('turn-1','综合回复已完成')
        self.store.complete_turn('turn-1','completed')
        restored=ControllerStore(self.root/'controller.sqlite3')
        self.assertEqual(restored.recover_running(),0)
        self.assertEqual(restored.get_job(parent['job_id'])['state'],'completed')
        self.assertEqual(restored.get_job(child['job_id'])['state'],'completed')
        self.assertTrue(restored.get_public_job(child['job_id'])['reply_suppressed'])

    def test_model_default_snapshot_only_changes_next_turn(self):
        parent=self.start_parent(router=True)
        class Models:
            value='gpt-6.1-sol'
            def get_default(self): return self.value
        models=Models()
        self.router.model_manager=models
        # Keep catalog stable: this test concerns dispatch model pinning only.
        self.complete()
        self.child()
        self.service._dispatch(self.store.claim_next())
        self.assertEqual(self.app.starts[-1][3]['model'],'gpt-6.1-sol')
        models.value='another-model'
        self.assertEqual(self.app.starts[-1][3]['model'],'gpt-6.1-sol')


class SteerContractTests(unittest.TestCase):
    def test_expected_turn_and_request_identity_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            client=AppServerClient(['unused'],codex_home=Path(directory)/'home',workspace=directory)
            requests=[]
            def request(method,params):
                requests.append((method,params)); return {'turnId':'turn-expected'}
            client.request=request
            self.assertEqual(client.steer_turn('thread-exact','turn-expected','补充','message-exact'),'turn-expected')
            method,params=requests[0]
            self.assertEqual(method,'turn/steer')
            self.assertEqual(params['expectedTurnId'],'turn-expected')
            self.assertEqual(params['threadId'],'thread-exact')
            self.assertEqual(params['clientUserMessageId'],'message-exact')
            client.request=lambda *args:{'turnId':'other-turn'}
            with self.assertRaises(AppServerError) as caught:
                client.steer_turn('thread-exact','turn-expected','补充','message-exact')
            self.assertFalse(caught.exception.definitive)


if __name__=='__main__':
    unittest.main()
