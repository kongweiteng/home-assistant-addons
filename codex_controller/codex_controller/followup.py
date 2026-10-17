"""Durable delivery and side-effect evidence for additions to a single active Turn.

The delivery ledger is additive: old job inputs and their idempotency digests never
change. An ambiguous delivery is never replayed automatically.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


def _now() -> str:
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='microseconds')


FOLLOWUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_followups (
    child_job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    parent_job_id TEXT NOT NULL REFERENCES jobs(job_id),
    thread_id TEXT NOT NULL,
    expected_turn_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('attempt','accepted','unknown','rejected','interrupt_wait','continuation')),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_followups_parent_idx ON job_followups(parent_job_id);
CREATE TABLE IF NOT EXISTS job_write_evidence (
    evidence_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    turn_id TEXT,
    tool_name TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('started','succeeded','unknown','submitted','failed')),
    result_json TEXT,
    result_digest TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class FollowupStoreMixin:
    def followup_candidate(self) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Inspect FIFO input only: control messages form a barrier, not a bypass."""
        with self._connect() as connection:
            parent = connection.execute(
                "SELECT * FROM jobs WHERE state='running' AND turn_id IS NOT NULL ORDER BY created_at LIMIT 1"
            ).fetchone()
            child = connection.execute(
                "SELECT * FROM jobs WHERE state='queued' ORDER BY created_at,job_id LIMIT 1"
            ).fetchone()
            if parent is None or child is None:
                return None
            if (parent['conversation_key'], parent['capability_profile']) != (child['conversation_key'], child['capability_profile']):
                return None
            if connection.execute('SELECT 1 FROM job_followups WHERE child_job_id=?', (child['job_id'],)).fetchone():
                return None
            return self._job_document(connection, parent, include_input=True), self._job_document(connection, child, include_input=True)

    def claim_followup(self, parent_id: str, child_id: str, expected_turn_id: str) -> bool:
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            parent = connection.execute("SELECT * FROM jobs WHERE job_id=? AND state='running' AND turn_id=?", (parent_id, expected_turn_id)).fetchone()
            child = connection.execute("SELECT * FROM jobs WHERE job_id=? AND state='queued'", (child_id,)).fetchone()
            if parent is None or child is None or not parent['thread_id']:
                return False
            if (parent['conversation_key'], parent['capability_profile']) != (child['conversation_key'], child['capability_profile']):
                return False
            if connection.execute('SELECT 1 FROM job_followups WHERE child_job_id=?', (child_id,)).fetchone():
                return False
            now = _now()
            connection.execute('INSERT INTO job_followups VALUES (?,?,?,?,?,?,?,?)',
                               (child_id,parent_id,parent['thread_id'],expected_turn_id,'attempt',None,now,now))
            connection.execute("UPDATE jobs SET state='running',started_at=?,attempt=attempt+1,thread_id=? WHERE job_id=?", (now,parent['thread_id'],child_id))
            self._event(connection,child_id,'followup_attempt')
            return True

    def followup_pending(self, turn_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1 FROM job_followups WHERE expected_turn_id=? AND state='attempt' LIMIT 1", (turn_id,)).fetchone() is not None

    def finish_followup_delivery(self, child_id: str, state: str, error_code: str | None = None) -> None:
        if state not in {'accepted','unknown','rejected','interrupt_wait'}:
            raise ValueError('Invalid followup delivery state')
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM job_followups WHERE child_job_id=?', (child_id,)).fetchone()
            if row is None or row['state'] not in {'attempt','interrupt_wait'}:
                return
            now = _now()
            connection.execute('UPDATE job_followups SET state=?,error_code=?,updated_at=? WHERE child_job_id=?', (state,error_code,now,child_id))
            if state == 'rejected':
                connection.execute("UPDATE jobs SET state='queued',started_at=NULL,thread_id=NULL WHERE job_id=?", (child_id,))
            elif state == 'accepted':
                parent = connection.execute('SELECT * FROM jobs WHERE job_id=?', (row['parent_job_id'],)).fetchone()
                if parent is not None and parent['state'] not in {'running','queued'}:
                    self.reconcile_followup_result(connection,row['parent_job_id'])
                    has_receipt = parent['state']=='completed' and bool(parent['result_text'])
                    connection.execute('UPDATE jobs SET state=?,error_code=?,finished_at=? WHERE job_id=?',
                                       ('completed' if has_receipt else 'recovery_required',None if has_receipt else 'followup_result_unconfirmed',now,child_id))
            elif state == 'unknown':
                connection.execute("UPDATE jobs SET state='recovery_required',error_code='followup_delivery_unknown',finished_at=? WHERE job_id=?", (now,child_id))
            self._event(connection,child_id,'followup_'+state,error_code=error_code)

    def settle_followups(self, turn_id: str, turn_status: str) -> None:
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute('SELECT * FROM job_followups WHERE expected_turn_id=?', (turn_id,)).fetchall()
            for row in rows:
                parent = connection.execute('SELECT * FROM jobs WHERE job_id=?', (row['parent_job_id'],)).fetchone()
                if parent is None or parent['state'] in {'running','queued'}:
                    continue
                now = _now()
                if row['state'] == 'accepted':
                    state = 'completed' if parent['state'] == 'completed' and parent['result_text'] else 'recovery_required'
                    connection.execute('UPDATE jobs SET state=?,error_code=?,finished_at=? WHERE job_id=? AND state=\'running\'',
                                       (state,None if state=='completed' else 'followup_result_unconfirmed',now,row['child_job_id']))
                    self._event(connection,row['child_job_id'],'followup_'+state)
                elif row['state'] == 'interrupt_wait':
                    if turn_status == 'interrupted':
                        connection.execute("UPDATE job_followups SET state='continuation',updated_at=? WHERE child_job_id=?", (now,row['child_job_id']))
                        connection.execute("UPDATE jobs SET state='queued',started_at=NULL,thread_id=NULL WHERE job_id=?", (row['child_job_id'],))
                    else:
                        connection.execute("UPDATE job_followups SET state='rejected',error_code='turn_completed_before_interrupt',updated_at=? WHERE child_job_id=?", (now,row['child_job_id']))
                        connection.execute("UPDATE jobs SET state='queued',started_at=NULL,thread_id=NULL WHERE job_id=?", (row['child_job_id'],))

    def followup_details(self, connection: Any, job_id: str, state: str) -> dict[str, Any]:
        row = connection.execute('SELECT * FROM job_followups WHERE child_job_id=?', (job_id,)).fetchone()
        if row is not None:
            parent = connection.execute('SELECT state,result_text FROM jobs WHERE job_id=?', (row['parent_job_id'],)).fetchone()
            return {'followup_parent_job_id':row['parent_job_id'], 'followup_state':row['state'],
                    'reply_suppressed': bool(row['state']=='accepted' and state=='completed' and parent is not None and parent['state']=='completed' and parent['result_text'])}
        continuation = connection.execute("SELECT 1 FROM job_followups WHERE parent_job_id=? AND state='continuation'", (job_id,)).fetchone()
        return {'followup_parent_job_id':None,'followup_state':'continuation' if continuation else None,
                'reply_suppressed':bool(continuation and state=='cancelled')}

    def contextual_attachments(self, connection: Any, parent_id: str, original: list[dict[str, Any]]) -> list[dict[str, Any]]:
        attachments = list(original)
        for row in connection.execute("SELECT j.input_json FROM job_followups f JOIN jobs j ON j.job_id=f.child_job_id WHERE f.parent_job_id=? AND f.state='accepted' ORDER BY f.created_at,f.child_job_id", (parent_id,)):
            attachments.extend(json.loads(row['input_json'])['attachments'])
        unique = {item['attachment_ref']:item for item in attachments}
        return list(unique.values())

    def related_attachments(self, parent_id: str, original: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return self.contextual_attachments(connection,parent_id,original)

    def continuation_input(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT j.job_id,j.input_json,f.state,f.error_code FROM job_followups f JOIN jobs j ON j.job_id=f.parent_job_id WHERE f.child_job_id=? AND (f.state='continuation' OR f.error_code='followup_catalog_changed')", (job_id,)).fetchone()
            if row is None:
                return payload
            parent = json.loads(row['input_json'])
            attachments = self.contextual_attachments(connection,row['job_id'],parent['attachments'])+payload['attachments']
        if row['state']=='continuation':
            text = '上一轮已确认中断，未执行任何外部操作。请结合原问题与补充完整处理并只回复一次。\n原问题：\n'+parent['text']+'\n补充：\n'+payload['text']
        else:
            text = '前一轮已结束。本条是后续补充，只执行本条明确要求，不得重复前轮已执行的操作。以下附件包含前轮的受控图片引用。\n'+payload['text']
            evidence = self.write_evidence(row['job_id'])
            if evidence:
                text += '\n前轮写入结果核对：'+str(evidence)
        return {**payload,'text':text,'attachments':attachments}

    def recover_followups(self) -> None:
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute("SELECT child_job_id FROM job_followups WHERE state IN ('attempt','interrupt_wait')").fetchall()
            for row in rows:
                connection.execute("UPDATE job_followups SET state='unknown',error_code='process_restarted',updated_at=? WHERE child_job_id=?", (_now(),row['child_job_id']))
                connection.execute("UPDATE jobs SET state='recovery_required',error_code='followup_delivery_unknown',finished_at=? WHERE job_id=?", (_now(),row['child_job_id']))
            accepted = connection.execute("SELECT f.child_job_id FROM job_followups f JOIN jobs p ON p.job_id=f.parent_job_id JOIN jobs c ON c.job_id=f.child_job_id WHERE f.state='accepted' AND p.state='completed' AND p.result_text IS NOT NULL AND p.result_text<>'' AND c.state='running'").fetchall()
            for row in accepted:
                connection.execute("UPDATE jobs SET state='completed',error_code=NULL,finished_at=? WHERE job_id=?", (_now(),row['child_job_id']))
                self._event(connection,row['child_job_id'],'followup_recovered_completed')
            connection.execute("UPDATE job_write_evidence SET phase='unknown',updated_at=? WHERE phase='started'", (_now(),))

    def can_interrupt_for_followup(self, parent_id: str, child_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=? AND state='running'", (parent_id,)).fetchone()
            if row is None or any(row[key] for key in ('output_observed','tool_activity_observed','artifact_observed')):
                return False
            if connection.execute('SELECT 1 FROM job_write_evidence WHERE job_id=?', (parent_id,)).fetchone():
                return False
            return connection.execute('SELECT 1 FROM job_followups WHERE parent_job_id=? AND child_job_id<>?', (parent_id,child_id)).fetchone() is None

    def reconcile_followup_result(self, connection: Any, parent_id: str) -> None:
        if not connection.execute("SELECT 1 FROM job_followups WHERE parent_job_id=? AND state='accepted'", (parent_id,)).fetchone():
            return
        evidence = connection.execute('SELECT tool_name,phase,result_json FROM job_write_evidence WHERE job_id=? ORDER BY created_at,evidence_id', (parent_id,)).fetchall()
        if not evidence:
            return
        from .tool_catalog import TOOL_BY_NAME
        summaries = []
        for item in evidence:
            definition = TOOL_BY_NAME.get(item['tool_name'])
            label = definition.display_name if definition is not None else item['tool_name']
            status = {'succeeded':'已成功','submitted':'已提交，实际执行结果待核验','failed':'明确失败，未自动重试'}.get(item['phase'],'结果待核对，未自动重试')
            proof = json.loads(item['result_json']) if item['result_json'] else {}
            detail_parts = []
            labels = {'amount':'金额','currency':'币种','id':'记录','entry_id':'记录','transaction_id':'记录',
                      'memo_id':'记录','record_id':'记录','action_id':'操作','selected_model':'模型',
                      'default_model':'默认模型','effective':'生效时机'}
            def describe(document: dict[str, Any]) -> None:
                for key,value in document.items():
                    if isinstance(value,dict):
                        describe(value)
                    elif key in labels and value is not None:
                        detail_parts.append(labels[key]+' '+('下一轮' if key=='effective' and value=='next_turn' else str(value)))
                    elif key=='idempotent_replay' and value is True:
                        detail_parts.append('已核对幂等回执，未重复执行')
            describe(proof)
            detail = '；'.join(dict.fromkeys(detail_parts))
            summaries.append(label+'：'+status+(' '+detail if detail else ''))
        row = connection.execute('SELECT result_text FROM jobs WHERE job_id=?', (parent_id,)).fetchone()
        text = row['result_text'] or ''
        suffix = '\n\n执行结果核对：\n'+'\n'.join(summaries)
        # Keep the actual execution receipt even when the model only answers its
        # latest supplemental question or its final response is interrupted.
        connection.execute('UPDATE jobs SET result_text=? WHERE job_id=?', ((text[:max(0,self.max_result_chars-len(suffix))]+suffix)[:self.max_result_chars],parent_id))

    def expire_followup_interrupts(self, timeout_seconds: float = 45) -> None:
        with self._connect() as connection:
            rows = connection.execute("SELECT child_job_id,updated_at FROM job_followups WHERE state='interrupt_wait'").fetchall()
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        for row in rows:
            if (now-datetime.fromisoformat(row['updated_at'])).total_seconds() >= timeout_seconds:
                self.finish_followup_delivery(row['child_job_id'],'unknown','interrupt_terminal_unknown')

    def start_write_evidence(self, job_id: str, turn_id: str | None, tool_name: str) -> str:
        evidence_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute('INSERT INTO job_write_evidence VALUES (?,?,?,?,?,?,?,?,?)', (evidence_id,job_id,turn_id,tool_name,'started',None,None,_now(),_now()))
        return evidence_id

    def finish_write_evidence(self, evidence_id: str, result: Any = None) -> None:
        # Only bounded, non-secret outcome/identifier fields are retained as proof.
        safe_keys = {'id','entry_id','transaction_id','memo_id','action_id','record_id','status','state','idempotent_replay','amount','currency','target','success','ok','selected_model','default_model','effective'}
        def summarize(value: Any, depth: int = 0) -> Any:
            if depth > 4 or not isinstance(value,dict):
                return {}
            safe = {key:value[key] for key in safe_keys & value.keys() if isinstance(value[key],(str,int,float,bool,type(None))) and len(str(value[key]))<=256}
            for key in ('result','entry','transaction','memo','record','action','execution','operation'):
                if isinstance(value.get(key),dict):
                    safe[key] = summarize(value[key],depth+1)
            return safe
        safe = summarize(result) if isinstance(result,dict) else None
        phase = 'succeeded' if safe is not None else 'unknown'
        if isinstance(result,dict):
            layers = [result]
            for key in ('result','execution','operation'):
                if isinstance(result.get(key),dict):
                    layers.append(result[key])
                    for nested in ('execution','operation'):
                        if isinstance(result[key].get(nested),dict):
                            layers.append(result[key][nested])
            statuses = {str(layer.get(key,'')).lower() for layer in layers for key in ('status','state')}
            if statuses & {'unknown','outcome_unknown','uncertain','executing','in_progress'}:
                phase = 'unknown'
            elif statuses & {'failed','rejected','cancelled','error','denied'} or any(layer.get('ok') is False or layer.get('success') is False for layer in layers):
                phase = 'failed'
            elif statuses & {'submitted','pending','accepted','queued','pending_confirmation','approved'}:
                phase = 'submitted'
        serialized = None if safe is None else json.dumps(safe,ensure_ascii=False,sort_keys=True)
        with self._connect() as connection:
            connection.execute('UPDATE job_write_evidence SET phase=?,result_json=?,result_digest=?,updated_at=? WHERE evidence_id=?',
                               (phase,serialized,None if serialized is None else hashlib.sha256(serialized.encode()).hexdigest(),_now(),evidence_id))

    def write_evidence(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute('SELECT tool_name,phase,result_json FROM job_write_evidence WHERE job_id=? ORDER BY created_at,evidence_id', (job_id,))]
