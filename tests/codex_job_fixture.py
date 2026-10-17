"""Persist real input identities for router tests that exercise write evidence."""
from __future__ import annotations

from codex_controller.store import ControllerStore


def persist_tool_job(store: ControllerStore, message_id: str, profile: str = 'owner', *, conversation_key: str = 'sha256:' + 'a' * 64) -> str:
    payload = {'version':1,'message_id':message_id,'conversation_key':conversation_key,
               'received_at':'2026-10-17T12:00:00+08:00','text':'受控工具测试输入',
               'attachments':[],'reply_capabilities':['text']}
    if profile != 'owner_legacy':
        payload['capability_profile'] = profile
    return store.create_job(payload)['job_id']
