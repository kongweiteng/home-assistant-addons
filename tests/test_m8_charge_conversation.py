from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from codex_controller.store import ControllerStore
from codex_controller.tool_proxy import ToolRouter, ToolProxyError
from codex_controller.tool_catalog import M8_CHARGE_TOOLS
from codex_controller.app_server import AppServerClient

class M8ChargeConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ControllerStore(Path(self.temp.name)/'db')
        self.request = Mock(return_value={'resumed':True})
        self.router = ToolRouter(store=self.store,m8_reminder_reply_token='synthetic-'*8,request_json=self.request)

    def test_catalog_and_owner_gate(self):
        self.assertTrue(M8_CHARGE_TOOLS <= set(self.router.available_tools('owner')))
        self.assertFalse(M8_CHARGE_TOOLS & set(self.router.available_tools('member_read_only')))
        self.assertFalse(M8_CHARGE_TOOLS & set(self.router.available_tools('owner_legacy')))
        for profile in ('member_read_only','owner_legacy'):
            self.router.begin_job('job','message',profile)
            with self.assertRaises(ToolProxyError): self.router.call('m8_charge_status',{})
            self.router.clear_job('job')
        with self.assertRaises(ToolProxyError): self.router.call('m8_charge_status',{})
        self.request.assert_not_called()

    def test_routing_and_idempotency(self):
        self.router.begin_job('job','message','owner',conversation_key='fixture')
        self.router.call('m8_charge_reminder',{'action':'resume'})
        self.router.call('m8_charge_reminder',{'action':'resume'})
        first,second=[call.args[3] for call in self.request.call_args_list]
        self.assertEqual(first['request_id'],second['request_id'])
        self.assertEqual(first['command'],'resume_reminders')
        self.assertEqual(first['arguments'],{})
        self.assertNotIn('synthetic',repr(first))
        self.router.call('m8_charge_reminder',{'action':'defer','remind_at':'2026-09-29T08:00:00+08:00'})
        self.assertEqual(self.request.call_args.args[3]['command'],'defer_current_charge')
        with self.assertRaises(ToolProxyError): self.router.call('m8_charge_reminder',{'action':'start_vehicle'})

    def test_queries_and_forecasts_are_read_only_and_not_vehicle_controls(self):
        self.router.begin_job('job','message','owner')
        self.router.call('m8_charge_forecast',{'charging_mode':'fast','charger_power_kw':60,'target_soc_percent':90})
        args=self.request.call_args.args
        self.assertEqual(args[1],'http://local-m8-charge-planner:8099/api/reminders/command')
        self.assertEqual(args[3]['command'],'forecast_charge')
        self.assertTrue(self.router.tool_definitions_by_name()['m8_charge_forecast'].read_only)

    def test_semantic_context_keeps_charge_reminders_out_of_memos(self):
        instructions=AppServerClient.build_developer_instructions(sorted(M8_CHARGE_TOOLS),'owner',self.router.tool_definitions_by_name())
        for text in ('自由口语','当前对话上下文','不创建普通memo','快充和慢充必须区分','不能声称已设置限充'):
            self.assertIn(text,instructions)

if __name__ == '__main__': unittest.main()
