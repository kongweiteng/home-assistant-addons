"""General work in Delivery's existing task/event store.

Only trusted schedulers call claim/finish. Model calls never receive leases,
host paths, program output bytes or authority to qualify an execution node.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from typing import Any

from .store import DeliveryError, DeliveryStore, SHANGHAI, canonical, identifier, reference, text

PROFILES = frozenset({'document', 'data', 'code', 'browser', 'preview'})
TERMINAL = frozenset({'completed', 'failed', 'cancelled'})
OUTPUT_NAME = re.compile(r'[^/\\\x00-\x1f\x7f]{1,120}\Z')


def work_spec(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {'profile', 'runtime_id', 'program', 'output_names', 'input_refs'}:
        raise DeliveryError('work_spec_invalid')
    if value['profile'] not in PROFILES:
        raise DeliveryError('profile_invalid')
    reference(value['runtime_id']); text(value['program'], 24000)
    names, inputs = value['output_names'], value['input_refs']
    if (not isinstance(names, list) or not 1 <= len(names) <= 4
            or any(not isinstance(x, str) or not OUTPUT_NAME.fullmatch(x) or x in {'.', '..'} for x in names)
            or len(set(names)) != len(names)):
        raise DeliveryError('output_names_invalid')
    if not isinstance(inputs, list) or len(inputs) > 10 or len(set(map(str, inputs))) != len(inputs):
        raise DeliveryError('input_refs_invalid')
    for item in inputs:
        if (not isinstance(item, str) or not (re.fullmatch(r'WI-[0-9a-f]{32}', item)
                or re.fullmatch(r'[A-Za-z0-9_-]{32,128}', item))):
            raise DeliveryError('input_refs_invalid')
    # A public browser cannot inherit private task inputs implicitly.
    if value['profile'] == 'browser' and inputs:
        raise DeliveryError('outbound_grant_required')
    if value['profile'] == 'browser':
        if __package__ == 'codex_delivery':
            from codex_work_runtime.browser_work import browser_intent
            from codex_work_runtime.errors import WorkRuntimeError
        else:
            from ..codex_work_runtime.browser_work import browser_intent
            from ..codex_work_runtime.errors import WorkRuntimeError
        try:
            intent = browser_intent(value['program'])
        except WorkRuntimeError:
            raise DeliveryError('browser_work_intent_invalid') from None
        if names != (['report.txt', 'page.png'] if intent['screenshot'] else ['report.txt']):
            raise DeliveryError('output_names_invalid')
    if value['profile'] == 'preview':
        if __package__ == 'codex_delivery':
            from codex_work_runtime.preview_work import preview_intent
            from codex_work_runtime.errors import WorkRuntimeError
        else:
            from ..codex_work_runtime.preview_work import preview_intent
            from ..codex_work_runtime.errors import WorkRuntimeError
        try:
            preview_intent(value['program'])
        except WorkRuntimeError:
            raise DeliveryError('preview_work_intent_invalid') from None
        if 'report.txt' not in names:
            raise DeliveryError('output_names_invalid')
    return json.loads(canonical(value))


class WorkStore(DeliveryStore):
    """No parallel task database: owner_work has a nullable project binding."""

    def create_work(self, owner: str, key: str, *, conversation_ref: str,
                    source_event_ref: str, input_revision: int, requirement: str,
                    spec: dict[str, Any], project_id: str | None = None,
                    expected_project_revision: int | None = None) -> dict[str, Any]:
        reference(conversation_ref); reference(source_event_ref); text(requirement, 16000)
        if type(input_revision) is not int or input_revision < 1:
            raise DeliveryError('input_revision_invalid')
        spec = work_spec(spec)
        if (project_id is None) != (expected_project_revision is None):
            raise DeliveryError('project_binding_invalid')

        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            project = self._project(c, owner, project_id, expected_project_revision) if project_id else None
            if project and project['state'] != 'active':
                raise DeliveryError('project_inactive')
            task, workspace = identifier('DL'), identifier('WS')
            c.execute("""INSERT INTO tasks(task_id,owner_ref,project_id,conversation_ref,source_event_ref,
                mode,requirement,project_revision,revision,phase,state,created_at,updated_at,
                task_scope,workspace_ref,work_json,input_revision,current_source_event_ref)
                VALUES (?,?,?,?,?,'work',?,?,1,'execution','queued',?,?,?,?,?,?,?)""",
                (task, owner, project_id, conversation_ref, source_event_ref, requirement,
                 project['revision'] if project else None, timestamp, timestamp,
                 'project' if project else 'owner_work', workspace, canonical(spec), input_revision, source_event_ref))
            if project:
                c.execute('INSERT INTO task_repositories SELECT owner_ref,project_id,?,repo_id,access,paths_json FROM project_repositories WHERE project_id=?', (task, project_id))
            result = {'task_id': task, 'workspace_ref': workspace, 'revision': 1,
                      'state': 'queued', 'execution_enabled': False}
            if project_id:
                result['project_id'] = project_id
            return result

        return self._mutation(owner, key, 'work.create', {
            'conversation_ref': conversation_ref, 'source_event_ref': source_event_ref,
            'input_revision': input_revision, 'requirement': requirement, 'spec': spec,
            'project_id': project_id, 'expected_project_revision': expected_project_revision}, apply)

    def work_task(self, owner: str, task_ref: str) -> dict[str, Any]:
        with closing(self._connect()) as c:
            row = self._owned(c, 'tasks', 'task_id', owner, task_ref)
        if row['mode'] != 'work':
            raise DeliveryError('not_found')
        return row

    def _work(self, c: sqlite3.Connection, owner: str, task: str, revision: int | None = None) -> dict[str, Any]:
        row = self._owned(c, 'tasks', 'task_id', owner, task)
        if row['mode'] != 'work':
            raise DeliveryError('not_found')
        if revision is not None and (type(revision) is not int or row['revision'] != revision):
            raise DeliveryError('revision_conflict')
        return row

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        result = {key: row[key] for key in ('workspace_ref', 'revision', 'state', 'phase', 'task_scope', 'input_revision')}
        result['task_ref'] = row['task_id']
        result['profile'] = json.loads(row['work_json'])['profile']
        if row['project_id']:
            result['project_ref'] = row['project_id']
        if row['control_pending']:
            result['control_pending'] = row['control_pending']
        return result

    def work_status(self, owner: str, task_ref: str) -> dict[str, Any]:
        row = self.work_task(owner, task_ref)
        result = self._public(row)
        # Return bounded validated metadata, never raw terminal diagnostics.
        outcome = json.loads(row['runtime_result_json'])
        # Executor receipts contain lease and storage metadata for the trusted
        # coordinator. They are never part of a model-facing task response.
        public = {k: outcome[k] for k in ('summary', 'artifact_refs', 'error_code',
                    'validation', 'delivery', 'exit_code') if k in outcome}
        if public:
            result['result'] = public
        return result

    def revise_work(self, owner: str, key: str, *, task_ref: str, expected_revision: int,
                    input_revision: int, supplement: str,
                    source_event_ref: str | None = None) -> dict[str, Any]:
        text(supplement, 16000)
        if type(input_revision) is not int or input_revision < 1:
            raise DeliveryError('input_revision_invalid')
        if source_event_ref:
            reference(source_event_ref)

        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            row = self._work(c, owner, task_ref, expected_revision)
            if row['state'] in TERMINAL:
                raise DeliveryError('task_terminal')
            if ((source_event_ref is None or source_event_ref == row['current_source_event_ref'])
                    and input_revision <= row['input_revision']):
                raise DeliveryError('input_revision_conflict')
            requirement = row['requirement'] + '\n\n' + supplement
            text(requirement, 64000)
            # A running program cannot silently adopt a new input. Preserve it
            # for reconciliation and stop at a safe boundary before replanning.
            pending = row['control_pending']
            if row['state'] == 'running' and pending is None:
                pending = 'revise'
            state = row['state'] if row['state'] in {'running', 'recovery_required'} else 'waiting_input'
            c.execute('UPDATE tasks SET requirement=?,revision=revision+1,input_revision=?,current_source_event_ref=?,state=?,control_pending=?,updated_at=? WHERE task_id=?',
                      (requirement, input_revision, source_event_ref or row['current_source_event_ref'], state, pending, timestamp, task_ref))
            return {'task_id': task_ref, 'revision': row['revision'] + 1, 'state': state}
        return self._mutation(owner, key, 'work.revise', {
            'task_id': task_ref, 'expected_revision': expected_revision,
            'input_revision': input_revision, 'source_event_ref': source_event_ref, 'supplement': supplement}, apply)

    def control_work(self, owner: str, key: str, *, task_ref: str, expected_revision: int,
                     action: str) -> dict[str, Any]:
        if action not in {'pause', 'resume', 'cancel'}:
            raise DeliveryError('action_invalid')

        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            row = self._work(c, owner, task_ref, expected_revision)
            if row['state'] in TERMINAL or row['state'] == 'recovery_required':
                raise DeliveryError('transition_invalid')
            if row['control_pending'] == 'cancel' and action != 'cancel':
                raise DeliveryError('cancel_pending')
            pending = None
            if row['state'] == 'running':
                if action == 'resume':
                    raise DeliveryError('transition_invalid')
                state, pending = 'running', action
            elif action == 'resume':
                if row['state'] != 'paused':
                    raise DeliveryError('transition_invalid')
                if row['project_id'] and self._project(c, owner, row['project_id'])['state'] != 'active':
                    raise DeliveryError('project_inactive')
                state = 'queued'
            else:
                state = {'pause': 'paused', 'cancel': 'cancelled'}[action]
            c.execute('UPDATE tasks SET state=?,control_pending=?,revision=revision+1,updated_at=? WHERE task_id=?',
                      (state, pending, timestamp, task_ref))
            return {'task_id': task_ref, 'revision': row['revision'] + 1, 'state': state,
                    **({'control_pending': pending} if pending else {})}
        return self._mutation(owner, key, 'work.control', {
            'task_id': task_ref, 'expected_revision': expected_revision, 'action': action}, apply)

    def prepare_work(self, owner: str, key: str, *, task_ref: str, expected_revision: int,
                     program: str, output_names: list[str], input_refs: list[str]) -> dict[str, Any]:
        """Replan saved supplements only after the previous execution has stopped.

        A new program cannot silently change the profile/runtime or acquire a
        network/deployment permission. Recovery is never cleared here.
        """
        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            row = self._work(c, owner, task_ref, expected_revision)
            if row['state'] not in {'queued', 'waiting_input', 'paused'} or row['control_pending']:
                raise DeliveryError('transition_invalid')
            spec = json.loads(row['work_json'])
            spec.update(program=program, output_names=output_names, input_refs=input_refs)
            spec = work_spec(spec)
            c.execute("UPDATE tasks SET work_json=?,state='queued',revision=revision+1,updated_at=? WHERE task_id=?",
                      (canonical(spec), timestamp, task_ref))
            return {'task_id': task_ref, 'revision': row['revision'] + 1, 'state': 'queued'}
        return self._mutation(owner, key, 'work.prepare', {
            'task_id': task_ref, 'expected_revision': expected_revision,
            'program': program, 'output_names': output_names, 'input_refs': input_refs}, apply)

    def wait_for_work_inputs(self, owner: str, key: str, *, task_ref: str,
                             expected_revision: int) -> dict[str, Any]:
        """Fence a queued task whose trusted inputs cannot be resolved.

        No executor has been claimed. A fresh prepare/revision is required;
        another queued task must not be held behind an expired input reference.
        """
        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            row = self._work(c, owner, task_ref, expected_revision)
            if row['state'] != 'queued' or row['control_pending']:
                raise DeliveryError('transition_invalid')
            c.execute("UPDATE tasks SET state='waiting_input',runtime_result_json=?,updated_at=? WHERE task_id=?",
                      (canonical({'error_code': 'work_inputs_unavailable'}), timestamp, task_ref))
            return {'task_id': task_ref, 'revision': row['revision'], 'state': 'waiting_input'}
        return self._mutation(owner, key, 'work.inputs_unavailable', {
            'task_id': task_ref, 'expected_revision': expected_revision}, apply)

    def record_work_source(self, owner: str, key: str, *, task_ref: str, expected_revision: int,
                           spec_digest: str, source_digest: str, receipt: dict[str, Any]) -> dict[str, Any]:
        """Trusted pre-execution byte receipt in the existing immutable event chain."""
        from .durability import digest
        from .object_archive import ArchiveError, ArchiveReceipt, _json
        try:
            archived = ArchiveReceipt.from_document(receipt)
            if any(not isinstance(value, str) or re.fullmatch(r'sha256:[a-f0-9]{64}', value) is None
                   for value in (spec_digest, source_digest)):
                raise ValueError()
            source_ref = 'SRC-' + source_digest.removeprefix('sha256:')
            scope = {'owner_ref': owner, 'task_ref': task_ref,
                     'revision': expected_revision, 'checkpoint_ref': source_ref}
            if (archived.scope_digest != digest(_json(scope))
                    or archived.manifest.failure_domain != self.replica_domain
                    or self.local_domain == self.replica_domain):
                raise ValueError()
        except (ArchiveError, ValueError, TypeError):
            raise DeliveryError('source_evidence_invalid') from None

        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            row = self._work(c, owner, task_ref, expected_revision)
            if row['state'] != 'queued' or row['control_pending']:
                raise DeliveryError('transition_invalid')
            if digest(row['work_json'].encode()) != spec_digest:
                raise DeliveryError('source_evidence_invalid')
            return {'task_id': task_ref, 'revision': expected_revision,
                    'source_ref': source_ref, 'source_archive': archived.document()}
        return self._mutation(owner, key, 'work.source_archive', {
            'task_id': task_ref, 'expected_revision': expected_revision, 'spec_digest': spec_digest,
            'source_digest': source_digest, 'receipt': archived.document()}, apply)

    def claim_work(self, owner: str, key: str, *, task_ref: str, expected_revision: int,
                   runner_ref: str, qualified_profiles: frozenset[str], lease_seconds: int = 900) -> dict[str, Any]:
        """Trusted scheduler only; returns metadata claim, not permission to replay."""
        reference(runner_ref)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise DeliveryError('lease_invalid')
        if not isinstance(qualified_profiles, frozenset):
            raise DeliveryError('capability_unavailable')

        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            row = self._work(c, owner, task_ref, expected_revision)
            spec = json.loads(row['work_json'])
            if spec['profile'] not in qualified_profiles:
                raise DeliveryError('capability_unavailable')
            if row['state'] != 'queued' or row['control_pending']:
                raise DeliveryError('transition_invalid')
            if row['project_id'] and self._project(c, owner, row['project_id'])['state'] != 'active':
                raise DeliveryError('project_inactive')
            # Only a canonical software request for this exact code revision
            # fences it. Unrelated Work keeps its original scheduling behavior.
            blocked = c.execute("SELECT 1 FROM events request WHERE request.owner_ref=? "
                "AND request.kind='software.bind.request' "
                "AND json_extract(request.body_json,'$.result.code_task_ref')=? "
                "AND json_extract(request.body_json,'$.result.code_revision')=? "
                "AND NOT EXISTS (SELECT 1 FROM commands bound JOIN events be USING(event_id) "
                "JOIN durability bd USING(event_id) WHERE bound.owner_ref=request.owner_ref "
                "AND bound.idempotency_key=json_extract(request.body_json,'$.result.binding_key') "
                "AND json_extract(be.body_json,'$.arguments')=json_extract(request.body_json,'$.result.parameters') "
                "AND be.kind='software.bind' AND bd.state='confirmed') LIMIT 1",
                (owner, task_ref, expected_revision)).fetchone()
            if blocked:
                raise DeliveryError('software_binding_pending')
            epoch, lease = row['lease_epoch'] + 1, identifier('LEASE')
            expires = (datetime.fromisoformat(timestamp) + timedelta(seconds=lease_seconds)).isoformat()
            c.execute("UPDATE tasks SET state='running',phase='execution',runtime_result_json='{}',lease_epoch=?,lease_ref=?,lease_expires_at=?,assigned_runner_ref=?,updated_at=? WHERE task_id=?",
                      (epoch, lease, expires, runner_ref, timestamp, task_ref))
            return {'task_id': task_ref, 'revision': row['revision'], 'state': 'running',
                    'lease_epoch': epoch, 'lease_ref': lease, 'lease_expires_at': expires,
                    'workspace_ref': row['workspace_ref'], 'runner_ref': runner_ref, 'execution_enabled': True}
        result = self._mutation(owner, key, 'work.claim', {
            'task_id': task_ref, 'expected_revision': expected_revision,
            'runner_ref': runner_ref, 'lease_seconds': lease_seconds}, apply)
        if (result['status'] != 'accepted' or not self.lease_current(
                owner, task_ref, result['lease_ref'], result['lease_epoch'])):
            result['execution_enabled'] = False
        return result

    def lease_current(self, owner: str, task_ref: str, lease_ref: str, epoch: int, *, runner_ref: str | None = None) -> bool:
        with closing(self._connect()) as c:
            c.execute('BEGIN')
            row = self._work(c, owner, task_ref)
            if c.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone():
                return False
            if row['project_id']:
                if self._project(c, owner, row['project_id'])['state'] != 'active':
                    return False
        return (not self.recovery_mode and row['state'] == 'running'
                and row['lease_ref'] == lease_ref and row['lease_epoch'] == epoch
                and (runner_ref is None or row['assigned_runner_ref'] == runner_ref)
                and datetime.fromisoformat(row['lease_expires_at']) > datetime.now(SHANGHAI)
                and row['control_pending'] is None)

    def record_work_evidence(self, owner: str, key: str, *, task_ref: str,
                             lease_ref: str, lease_epoch: int, phase: str,
                             evidence: dict[str, Any]) -> dict[str, Any]:
        if (phase not in {'artifact_upload', 'delivery_wait'} or not isinstance(evidence, dict)
                or set(evidence) - {'execution', 'upload_ids', 'completion_event_id', 'summary', 'error_code', 'checkpoint_archive'}
                or len(canonical(evidence).encode()) > 65536):
            raise DeliveryError('evidence_invalid')
        self._archive_evidence(evidence)

        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            row = self._work(c, owner, task_ref)
            self._verify_archive_scope(row, evidence)
            if row['lease_ref'] != lease_ref or type(lease_epoch) is not int or row['lease_epoch'] != lease_epoch:
                raise DeliveryError('lease_fenced')
            if row['state'] != 'running' or row['control_pending']:
                raise DeliveryError('control_pending')
            if row['phase'] == 'delivery_wait' and phase != 'delivery_wait':
                raise DeliveryError('transition_invalid')
            c.execute('UPDATE tasks SET phase=?,runtime_result_json=?,updated_at=? WHERE task_id=?',
                      (phase, canonical(evidence), timestamp, task_ref))
            return {'task_id': task_ref, 'revision': row['revision'], 'state': 'running'}
        return self._mutation(owner, key, 'work.evidence', {
            'task_id': task_ref, 'lease_ref': lease_ref, 'lease_epoch': lease_epoch,
            'phase': phase, 'evidence': evidence}, apply)

    def record_stopped_checkpoint(self, owner: str, key: str, *, task_ref: str,
                                  lease_ref: str, lease_epoch: int,
                                  evidence: dict[str, Any]) -> dict[str, Any]:
        """Trusted stop evidence without clearing pause/cancel/revise requests.

        This is not a model method. A stopped execution must be durably known
        before archiving bytes; restart may resume that archive, never execute.
        """
        allowed = {'execution', 'upload_ids', 'completion_event_id', 'summary', 'error_code', 'checkpoint_archive', 'stop_after_checkpoint'}
        if (not isinstance(evidence, dict) or set(evidence) - allowed
                or not isinstance(evidence.get('execution'), dict)
                or ('stop_after_checkpoint' in evidence and evidence['stop_after_checkpoint'] != 'paused')
                or len(canonical(evidence).encode()) > 65536):
            raise DeliveryError('evidence_invalid')
        self._archive_evidence(evidence)
        receipt = evidence['execution']
        if (receipt.get('checkpoint_confirmed') is not True or receipt.get('evidence') not in {'exited', 'terminated'}
                or type(receipt.get('input_revision')) is not int or receipt['input_revision'] < 1
                or receipt.get('assignment_epoch') != lease_epoch or type(receipt.get('assignment_epoch')) is not int
                or not isinstance(receipt.get('checkpoint_ref'), str)
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', receipt['checkpoint_ref']) is None):
            raise DeliveryError('evidence_invalid')

        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            row = self._work(c, owner, task_ref)
            if row['lease_ref'] != lease_ref or type(lease_epoch) is not int or row['lease_epoch'] != lease_epoch:
                raise DeliveryError('lease_fenced')
            if row['state'] != 'running' or row['phase'] not in {'execution', 'checkpoint_archive', 'artifact_upload', 'delivery_wait'}:
                raise DeliveryError('transition_invalid')
            if (receipt.get('workspace_ref') != row['workspace_ref'] or receipt['input_revision'] > row['revision']):
                raise DeliveryError('evidence_invalid')
            previous = json.loads(row['runtime_result_json'])
            if 'execution' in previous and previous['execution'] != receipt:
                raise DeliveryError('evidence_invalid')
            if any(key not in evidence or evidence[key] != value for key, value in previous.items() if key != 'checkpoint_archive'):
                raise DeliveryError('evidence_invalid')
            self._verify_archive_scope(row, evidence)
            phase = 'checkpoint_archive' if row['phase'] == 'execution' else row['phase']
            c.execute('UPDATE tasks SET phase=?,runtime_result_json=?,updated_at=? WHERE task_id=?',
                      (phase, canonical(evidence), timestamp, task_ref))
            return {'task_id': task_ref, 'revision': row['revision'], 'state': 'running'}
        return self._mutation(owner, key, 'work.checkpoint', {
            'task_id': task_ref, 'lease_ref': lease_ref, 'lease_epoch': lease_epoch, 'evidence': evidence}, apply)

    def _stopped_path(self, owner, task_ref, lease_ref, lease_epoch):
        reference(owner); reference(task_ref); reference(lease_ref)
        if type(lease_epoch) is not int or lease_epoch < 1:
            raise DeliveryError('lease_fenced')
        scope = {'owner_ref': owner, 'task_ref': task_ref, 'lease_ref': lease_ref, 'lease_epoch': lease_epoch}
        folder = self.path.parent / (self.path.name + '.stopped')
        return folder, hashlib.sha256(canonical(scope).encode()).hexdigest() + '.json', scope

    def staged_stopped_checkpoint(self, owner: str, *, task_ref: str,
                                  lease_ref: str, lease_epoch: int) -> dict[str, Any] | None:
        """Read private local stop facts; this is never an off-device receipt."""
        folder, name, scope = self._stopped_path(owner, task_ref, lease_ref, lease_epoch)
        try:
            directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise DeliveryError('stopped_checkpoint_unavailable') from None
        try:
            if os.fstat(directory).st_mode & 0o077:
                raise DeliveryError('stopped_checkpoint_unavailable')
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                        or not 1 <= info.st_size <= 69632):
                    raise DeliveryError('stopped_checkpoint_unavailable')
                raw = source.read(69633)
            document = json.loads(raw)
            if (not isinstance(document, dict) or set(document) != {'version', 'scope', 'evidence'}
                    or type(document['version']) is not int or document['version'] != 1
                    or document['scope'] != scope or type(document['scope'].get('lease_epoch')) is not int
                    or not isinstance(document['evidence'], dict) or canonical(document).encode() != raw):
                raise ValueError('staged_receipt_invalid')
            return document['evidence']
        except DeliveryError:
            raise
        except Exception:
            raise DeliveryError('stopped_checkpoint_unavailable') from None
        finally:
            os.close(directory)

    def stage_stopped_checkpoint(self, owner: str, *, task_ref: str, lease_ref: str,
                                 lease_epoch: int, evidence: dict[str, Any]) -> None:
        """Fsync immutable stop facts before a DB/replica pending gate can fail.

        It carries no task state or replica journal. Reconciliation still uses
        the single existing metadata event chain, replica and object journal.
        """
        if self.recovery_mode:
            raise DeliveryError('recovery_read_only')
        row = self.work_task(owner, task_ref)
        if row['state'] != 'running' or (row['lease_ref'], row['lease_epoch']) != (lease_ref, lease_epoch):
            raise DeliveryError('lease_fenced')
        folder, name, scope = self._stopped_path(owner, task_ref, lease_ref, lease_epoch)
        raw = canonical({'version': 1, 'scope': scope, 'evidence': evidence}).encode()
        if len(raw) > 69632:
            raise DeliveryError('evidence_invalid')
        temporary = None
        try:
            folder.mkdir(mode=0o700, exist_ok=True)
            if folder.is_symlink() or folder.stat().st_mode & 0o077:
                raise DeliveryError('stopped_checkpoint_unavailable')
            existing = self.staged_stopped_checkpoint(owner, task_ref=task_ref, lease_ref=lease_ref, lease_epoch=lease_epoch)
            if existing is not None:
                if existing != evidence:
                    raise DeliveryError('evidence_invalid')
                for path in (folder, folder.parent):
                    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                return
            with os.scandir(folder) as entries:
                if sum(1 for _ in entries) >= 1024:
                    raise DeliveryError('stopped_checkpoint_quota_exceeded')
            fd, temporary = tempfile.mkstemp(prefix='.stopped-', dir=folder)
            with os.fdopen(fd, 'wb') as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            try:
                os.link(temporary, folder / name, follow_symlinks=False)
            except FileExistsError:
                existing = self.staged_stopped_checkpoint(owner, task_ref=task_ref, lease_ref=lease_ref, lease_epoch=lease_epoch)
                if existing != evidence:
                    raise DeliveryError('evidence_invalid')
            os.unlink(temporary)
            temporary = None
            for path in (folder, folder.parent):
                directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except DeliveryError:
            raise
        except Exception:
            raise DeliveryError('stopped_checkpoint_unavailable') from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def finish_work(self, owner: str, key: str, *, task_ref: str, lease_ref: str,
                    lease_epoch: int, state: str, evidence: dict[str, Any]) -> dict[str, Any]:
        if state not in {'completed', 'failed', 'cancelled', 'paused', 'waiting_input', 'recovery_required'}:
            raise DeliveryError('transition_invalid')
        reference(lease_ref)
        allowed = {'exit_code', 'validation', 'delivery', 'summary', 'artifact_refs', 'error_code', 'checkpoint_digest', 'checkpoint_revision', 'checkpoint_archive'}
        if not isinstance(evidence, dict) or set(evidence) - allowed or len(canonical(evidence).encode()) > 32768:
            raise DeliveryError('evidence_invalid')
        # Final evidence must keep the archive descriptor in both the durable
        # event arguments and task row, even when a caller only reports state.
        row = self.work_task(owner, task_ref)
        previous = json.loads(row['runtime_result_json'])
        if 'checkpoint_archive' in previous:
            evidence = {**evidence, 'checkpoint_archive': evidence.get('checkpoint_archive', previous['checkpoint_archive'])}
            if 'checkpoint_digest' not in evidence:
                checkpoint = previous.get('execution', {}).get('checkpoint_ref') or previous.get('checkpoint_digest')
                if checkpoint:
                    evidence['checkpoint_digest'] = checkpoint
            evidence.setdefault('checkpoint_revision', previous.get('execution', {}).get('input_revision',
                previous.get('checkpoint_revision', row['revision'])))
        self._archive_evidence(evidence)
        if state == 'completed' and (type(evidence.get('exit_code')) is not int or evidence.get('exit_code') != 0
                or evidence.get('validation') != 'passed' or evidence.get('delivery') != 'gateway_sent'):
            raise DeliveryError('completion_unverified')

        def apply(c: sqlite3.Connection, timestamp: str) -> dict[str, Any]:
            row = self._work(c, owner, task_ref)
            self._verify_archive_scope(row, evidence)
            if row['lease_ref'] != lease_ref or type(lease_epoch) is not int or row['lease_epoch'] != lease_epoch:
                raise DeliveryError('lease_fenced')
            if row['state'] != 'running':
                raise DeliveryError('transition_invalid')
            if (datetime.fromisoformat(row['lease_expires_at']) <= datetime.fromisoformat(timestamp)
                    and state != 'recovery_required'
                    and not (row['phase'] in {'checkpoint_archive', 'artifact_upload', 'delivery_wait'}
                             and (state == 'completed' or evidence.get('validation') == 'process_stopped'))):
                raise DeliveryError('lease_expired')
            if row['control_pending'] and state == 'completed':
                raise DeliveryError('control_pending')
            c.execute('UPDATE tasks SET state=?,runtime_result_json=?,control_pending=NULL,updated_at=? WHERE task_id=?',
                      (state, canonical(evidence), timestamp, task_ref))
            return {'task_id': task_ref, 'revision': row['revision'], 'state': state}
        return self._mutation(owner, key, 'work.finish', {
            'task_id': task_ref, 'lease_ref': lease_ref, 'lease_epoch': lease_epoch,
            'state': state, 'evidence': evidence}, apply)

    @staticmethod
    def _archive_evidence(evidence: dict[str, Any]) -> None:
        if 'checkpoint_archive' in evidence:
            from .object_archive import ArchiveReceipt, ArchiveError
            try:
                ArchiveReceipt.from_document(evidence['checkpoint_archive'])
            except ArchiveError:
                raise DeliveryError('evidence_invalid') from None

    def _verify_archive_scope(self, row: dict[str, Any], evidence: dict[str, Any]) -> None:
        previous = json.loads(row['runtime_result_json'])
        old = previous.get('checkpoint_archive')
        supplied = evidence.get('checkpoint_archive')
        if old is not None and supplied != old:
            raise DeliveryError('evidence_invalid')
        if supplied is None:
            return
        if supplied['manifest']['failure_domain'] != self.replica_domain or self.replica_domain == self.local_domain:
            raise DeliveryError('evidence_invalid')
        from .durability import digest
        from .object_archive import _json
        execution = evidence.get('execution', previous.get('execution', {}))
        revision = execution.get('input_revision', evidence.get('checkpoint_revision', row['revision']))
        checkpoint = execution.get('checkpoint_ref', evidence.get('checkpoint_digest'))
        if supplied['scope_digest'] != digest(_json({'owner_ref': row['owner_ref'], 'task_ref': row['task_id'],
                'revision': revision, 'checkpoint_ref': checkpoint})):
            raise DeliveryError('evidence_invalid')
