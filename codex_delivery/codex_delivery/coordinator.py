"""Trusted orchestration of one task, qualified execution and original-chat results.

Only immutable upload/completion operations with Controller-enforced idempotency
may be resumed. A task program and a Gateway send are never replayed here.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Callable, Iterator, Protocol

from .callback import CallbackError, CallbackUnknown, ControllerCallback
from .object_archive import ArchiveError, ObjectArchive
from .source_archive import WorkSourceArchive
from .scheduling import confirmed_turns, ordered_tasks
from .store import DeliveryError, canonical
from .work import OUTPUT_NAME, WorkStore

MIMES = {'.png': 'image/png', '.pdf': 'application/pdf', '.zip': 'application/zip',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.csv': 'text/csv', '.txt': 'text/plain'}
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
TRANSIENT = frozenset({'writer_busy', 'durability_pending', 'replica_unavailable', 'replica_unsettled'})


class WorkAdapter(Protocol):
    def qualifies(self, profile: str, runtime_id: str) -> bool: ...
    def execute(self, task: dict[str, Any], claim: dict[str, Any], spec: dict[str, Any],
                inputs: dict[str, bytes]) -> dict[str, Any]: ...
    def cancel(self, task: dict[str, Any], claim: dict[str, Any]) -> None: ...
    def checkpoint(self, task: dict[str, Any], receipt: dict[str, Any]) -> Path: ...


class WorkCoordinator:
    def __init__(self, store: WorkStore, *, runner_ref: str, adapter: WorkAdapter,
                 callback: ControllerCallback,
                 input_resolver: Callable[[dict[str, Any], list[str]], dict[str, bytes]] | None = None,
                 checkpoint_archive: ObjectArchive | None = None,
                 source_archive: WorkSourceArchive | None = None,
                 preview_transport=None,
                 internal_result_handler=None):
        self.store, self.runner_ref, self.adapter, self.callback = store, runner_ref, adapter, callback
        self.input_resolver = input_resolver
        if checkpoint_archive is not None and (checkpoint_archive.replica is not store.replica
                or checkpoint_archive.local_domain != store.local_domain
                or checkpoint_archive.replica_domain != store.replica_domain):
            raise DeliveryError('archive_replica_invalid')
        self.checkpoint_archive = checkpoint_archive
        if source_archive is not None and (source_archive.store is not store
                or source_archive.archive is not checkpoint_archive):
            raise DeliveryError('archive_replica_invalid')
        self.source_archive = source_archive
        self.preview_transport = preview_transport
        if internal_result_handler is not None and not callable(internal_result_handler):
            raise DeliveryError('internal_result_handler_invalid')
        self.internal_result_handler = internal_result_handler
        self._dispatch_started = False
        self._shutdown = threading.Event()

    def request_stop(self) -> None:
        """Stop claiming; the watcher requests a bounded executor stop."""
        self._shutdown.set()

    @staticmethod
    def _claim(task: dict[str, Any]) -> dict[str, Any]:
        return {**{key: task[key] for key in ('lease_ref', 'lease_epoch', 'lease_expires_at')},
                'runner_ref': task['assigned_runner_ref']}

    @contextmanager
    def _dispatch(self) -> Iterator[None]:
        path = self.store.path.with_suffix('.work-coordinator.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                raise DeliveryError('storage_permissions_invalid')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeliveryError('worker_busy') from None
            yield
        finally:
            os.close(fd)

    def run_once(self) -> bool:
        self._dispatch_started = True
        if self.store.recovery_mode:
            raise DeliveryError('recovery_read_only')
        if self._shutdown.is_set():
            return False
        with self._dispatch():
            with closing(self.store._connect()) as c:
                if c.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone():
                    return False
                running = c.execute("SELECT * FROM tasks WHERE mode='work' AND state='running' AND assigned_runner_ref=? ORDER BY updated_at,task_id",
                                    (self.runner_ref,)).fetchall()
            for row in running:
                task = dict(row)
                if task['phase'] == 'delivery_wait':
                    if self._reconcile_delivery(task):
                        return True
                    continue
                if task['phase'] == 'artifact_upload':
                    if self._upload(task, json.loads(task['runtime_result_json'])):
                        return True
                    continue
                if task['phase'] == 'checkpoint_archive':
                    if self._continue_checkpoint(task, json.loads(task['runtime_result_json'])):
                        return True
                    continue
                staged = self.store.staged_stopped_checkpoint(task['owner_ref'], task_ref=task['task_id'],
                    lease_ref=task['lease_ref'], lease_epoch=task['lease_epoch'])
                if staged is not None:
                    self._receipt(task, self._claim(task), staged.get('execution'), allow_prior_revision=True)
                    try:
                        recorded = self.store.record_stopped_checkpoint(task['owner_ref'], 'checkpoint:' + task['lease_ref'],
                            task_ref=task['task_id'], lease_ref=task['lease_ref'], lease_epoch=task['lease_epoch'], evidence=staged)
                    except DeliveryError as exc:
                        if exc.code in TRANSIENT:
                            return False
                        raise
                    if recorded['status'] == 'accepted':
                        return self._continue_checkpoint(self.store.work_task(task['owner_ref'], task['task_id']), staged)
                    return False
                # We cannot distinguish a crash before launch from one during a
                # running program. Fence it, request stop, and retain all data.
                try:
                    self.adapter.cancel(task, self._claim(task))
                except Exception:
                    pass
                return self._finish(task, self._claim(task), 'recovery_required', {'error_code': 'execution_outcome_unknown'})
            # Fairness parsing must never block original running/unknown
            # reconciliation. Only now read queue and turns in one snapshot.
            with closing(self.store._connect()) as c:
                c.execute('BEGIN')
                if c.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone():
                    return False
                queued = c.execute("SELECT * FROM tasks WHERE mode='work' AND state='queued' ORDER BY created_at,task_id").fetchall()
                if not queued:
                    return False
                rows = ordered_tasks(queued, confirmed_turns(c, self.runner_ref))
            # An unavailable profile must not starve other queued tasks.
            for row in rows:
                task = dict(row)
                spec = json.loads(task['work_json'])
                if not self.adapter.qualifies(spec['profile'], spec['runtime_id']):
                    continue
                sealed_inputs=None
                if self.source_archive is not None:
                    try:
                        sealed_inputs=self.source_archive.inputs(task,spec)
                    except ArchiveError:
                        continue
                if sealed_inputs is not None:
                    inputs=sealed_inputs
                elif spec['input_refs']:
                    if self.input_resolver is None:
                        continue
                    try:
                        inputs = self.input_resolver(task, spec['input_refs'])
                    except CallbackUnknown:
                        # Resolving is an exact idempotent read/bind operation.
                        # Leave this task queued and allow unrelated work while
                        # Controller connectivity is unavailable.
                        continue
                    except CallbackError as exc:
                        if exc.code not in {'callback_not_found', 'callback_not_authorized',
                                'callback_conflict', 'callback_rejected', 'work_input_request_invalid',
                                'work_input_response_invalid', 'callback_scope_invalid'}:
                            continue
                        try:
                            self.store.wait_for_work_inputs(task['owner_ref'],
                                'inputs-unavailable:' + task['task_id'] + ':' + str(task['revision']),
                                task_ref=task['task_id'], expected_revision=task['revision'])
                        except DeliveryError as error:
                            if error.code in TRANSIENT:
                                return False
                            if error.code not in {'revision_conflict', 'transition_invalid'}:
                                raise
                        return True
                else:
                    inputs = {}
                try:
                    if self.source_archive is not None and sealed_inputs is None:
                        if not self.source_archive.save(task, spec, inputs):continue
                        try:inputs=self.source_archive.inputs(task,spec)
                        except ArchiveError:continue
                        if inputs is None:continue
                    claim = self.store.claim_work(task['owner_ref'], 'claim:' + task['task_id'] + ':' + str(task['revision']),
                        task_ref=task['task_id'], expected_revision=task['revision'], runner_ref=self.runner_ref,
                        qualified_profiles=frozenset({spec['profile']}),
                        lease_seconds={'code': 3600, 'browser': 120, 'preview': 1800}.get(spec['profile'], 900))
                except DeliveryError as exc:
                    if exc.code in TRANSIENT:
                        return False
                    if exc.code in {'revision_conflict', 'transition_invalid', 'project_inactive', 'software_binding_pending'}:
                        continue
                    raise
                if claim['status'] != 'accepted' or not claim['execution_enabled']:
                    return True
                self._execute(task, claim, spec, inputs)
                return True
            return False

    def _receipt(self, task: dict[str, Any], claim: dict[str, Any], receipt: Any, *, allow_prior_revision=False) -> None:
        try:
            if (not isinstance(receipt, dict) or type(receipt.get('version')) is not int or receipt['version'] != 1
                    or receipt.get('workspace_ref') != task['workspace_ref']
                    or type(receipt.get('input_revision')) is not int
                    or not (1 <= receipt['input_revision'] <= task['revision'] if allow_prior_revision
                            else receipt['input_revision'] == task['revision'])
                    or type(receipt.get('assignment_epoch')) is not int or receipt['assignment_epoch'] != claim['lease_epoch']
                    or receipt.get('checkpoint_confirmed') is not True
                    or receipt.get('evidence') not in {'exited', 'terminated'}
                    or receipt.get('stop_reason') not in {'finished', 'cancelled', 'lease_expired', 'timeout'}
                    or not isinstance(receipt.get('checkpoint_ref'), str)
                    or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', receipt['checkpoint_ref'])
                    or not isinstance(receipt.get('files'), list) or len(receipt['files']) > 256
                    or len(canonical(receipt).encode()) > 60000):
                raise ValueError()
            names = set()
            for entry in receipt['files']:
                name = entry['name']
                if (not isinstance(name, str) or not name or len(name.encode()) > 255 or '\\' in name
                        or any(p in {'', '.', '..'} for p in name.split('/')) or any(ord(c) < 32 for c in name)
                        or name in names or type(entry['size_bytes']) is not int or not 0 <= entry['size_bytes'] <= 10 * 1024**3
                        or not isinstance(entry['sha256'], str) or not DIGEST.fullmatch(entry['sha256'])):
                    raise ValueError()
                names.add(name)
            instant = datetime.fromisoformat(receipt['recorded_at'])
            if instant.utcoffset() is None:
                raise ValueError()
        except (KeyError, TypeError, ValueError, OverflowError):
            raise DeliveryError('execution_receipt_invalid') from None

    def _execute(self, task: dict[str, Any], claim: dict[str, Any], spec: dict[str, Any], inputs: dict[str, bytes]) -> None:
        stop = threading.Event()
        def watch() -> None:
            while not stop.wait(.1):
                try:
                    if not self._shutdown.is_set() and self.store.lease_current(
                            task['owner_ref'], task['task_id'], claim['lease_ref'], claim['lease_epoch'], runner_ref=self.runner_ref):
                        continue
                except Exception:
                    pass
                try:
                    self.adapter.cancel(task, claim)
                except Exception:
                    pass
                return
        watcher = threading.Thread(target=watch, daemon=True, name='work-lease')
        watcher.start()
        try:
            receipt = self.adapter.execute(task, claim, spec, inputs)
            self._receipt(task, claim, receipt)
        except Exception:
            self._finish(task, claim, 'recovery_required', {'error_code': 'execution_outcome_unknown'})
            return
        finally:
            stop.set()
            watcher.join(1)
        evidence = {'execution': receipt, 'upload_ids': []}
        if self._shutdown.is_set():
            evidence['stop_after_checkpoint'] = 'paused'
        self.store.stage_stopped_checkpoint(task['owner_ref'], task_ref=task['task_id'],
            lease_ref=claim['lease_ref'], lease_epoch=claim['lease_epoch'], evidence=evidence)
        try:
            recorded = self.store.record_stopped_checkpoint(task['owner_ref'], 'checkpoint:' + claim['lease_ref'],
                task_ref=task['task_id'], lease_ref=claim['lease_ref'], lease_epoch=claim['lease_epoch'], evidence=evidence)
        except DeliveryError as exc:
            if exc.code in TRANSIENT:
                return
            raise
        if recorded['status'] == 'accepted':
            self._continue_checkpoint(self.store.work_task(task['owner_ref'], task['task_id']), evidence)

    def _archive_checkpoint(self, task: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any] | None:
        if self.checkpoint_archive is None or 'checkpoint_archive' in evidence:
            return evidence
        claim, receipt = self._claim(task), evidence['execution']
        try:
            self._receipt(task, claim, receipt, allow_prior_revision=True)
            checkpoint = self.adapter.checkpoint(task, receipt)
            # No DB mutation can pass an unknown chunk. Reconcile the same
            # immutable object group first, then record its manifest receipt.
            with self.store._writer_guard():
                archived = self.checkpoint_archive.save(checkpoint, scope={
                    'owner_ref': task['owner_ref'], 'task_ref': task['task_id'],
                    'revision': receipt['input_revision'], 'checkpoint_ref': receipt['checkpoint_ref']},
                    expected_files=receipt['files'])
                evidence = {**evidence, 'checkpoint_archive': archived.document()}
                result = self.store.record_stopped_checkpoint(task['owner_ref'], 'archive:' + claim['lease_ref'],
                    task_ref=task['task_id'], lease_ref=claim['lease_ref'], lease_epoch=claim['lease_epoch'], evidence=evidence)
            return evidence if result['status'] == 'accepted' else None
        except (ArchiveError, DeliveryError, OSError):
            return None

    def _continue_checkpoint(self, task: dict[str, Any], evidence: dict[str, Any]) -> bool:
        evidence = self._archive_checkpoint(task, evidence)
        if evidence is None:
            return False
        task = self.store.work_task(task['owner_ref'], task['task_id'])
        claim, receipt = self._claim(task), evidence['execution']
        if task['control_pending']:
            return self._stopped(task, claim, receipt)
        stopped = {'validation': 'process_stopped', 'checkpoint_digest': receipt['checkpoint_ref']}
        if self._shutdown.is_set() or evidence.get('stop_after_checkpoint') == 'paused':
            return self._finish(task, claim, 'paused', stopped)
        if self._registered_internal(task):
            return self._internal_result(task, evidence)
        if (receipt['stop_reason'] != 'finished' or receipt['evidence'] != 'exited'
                or type(receipt.get('exit_code')) is not int or receipt['exit_code'] != 0):
            return self._finish(task, claim, 'failed' if receipt['stop_reason'] != 'lease_expired' else 'recovery_required',
                {**stopped, 'error_code': 'execution_failed',
                 **({'exit_code': receipt['exit_code']} if type(receipt.get('exit_code')) is int else {})})
        try:
            recorded = self.store.record_work_evidence(task['owner_ref'], 'result:' + claim['lease_ref'],
                task_ref=task['task_id'], lease_ref=claim['lease_ref'], lease_epoch=claim['lease_epoch'],
                phase='artifact_upload', evidence=evidence)
        except DeliveryError as exc:
            if exc.code in TRANSIENT:
                return False
            if exc.code == 'control_pending':
                return self._stopped(self.store.work_task(task['owner_ref'], task['task_id']), claim, receipt)
            raise
        return self._upload(self.store.work_task(task['owner_ref'], task['task_id']), evidence) if recorded['status'] == 'accepted' else False

    def _registered_internal(self, task):
        # Only a canonical event can select this branch; no task labels,
        # program bytes or output names grant internal completion authority.
        with closing(self.store._connect()) as connection:
            return connection.execute("SELECT 1 FROM events WHERE owner_ref=? AND task_id=? "
                "AND kind='repository.test.create' LIMIT 1",
                (task['owner_ref'], task['task_id'])).fetchone() is not None

    def _internal_result(self, task, evidence):
        if (self.internal_result_handler is None or self.checkpoint_archive is None
                or self.source_archive is None or 'checkpoint_archive' not in evidence):
            return self._finish(task, self._claim(task), 'recovery_required',
                {'error_code': 'internal_result_unavailable'})
        try:
            return self.internal_result_handler(task, evidence) is True
        except DeliveryError as exc:
            if exc.code in TRANSIENT:
                return False
            return self._finish(task, self._claim(task), 'recovery_required',
                {'error_code': 'internal_result_unverified'})
        except Exception:
            return self._finish(task, self._claim(task), 'recovery_required',
                {'error_code': 'internal_result_unverified'})

    def _stopped(self, task: dict[str, Any], claim: dict[str, Any], receipt: dict[str, Any]) -> bool:
        evidence = json.loads(task['runtime_result_json'])
        evidence.setdefault('execution', receipt)
        evidence = self._archive_checkpoint(task, evidence)
        if evidence is None:
            return False
        task = self.store.work_task(task['owner_ref'], task['task_id'])
        state = {'cancel': 'cancelled', 'pause': 'paused', 'revise': 'waiting_input'}.get(task['control_pending'])
        if state is None:
            raise DeliveryError('control_pending')
        return self._finish(task, claim, state, {'validation': 'process_stopped', 'checkpoint_digest': receipt['checkpoint_ref']})

    def _upload(self, task: dict[str, Any], evidence: dict[str, Any]) -> bool:
        claim, receipt = self._claim(task), evidence['execution']
        # With a pending revision, the receipt is for the previous monotonic
        # version. It is still proof of stop, never permission to publish it.
        if task['control_pending']:
            return self._stopped(task, claim, receipt)
        if self._registered_internal(task):
            return self._continue_checkpoint(task, evidence)
        self._receipt(task, claim, receipt)
        spec = json.loads(task['work_json'])
        try:
            evidence = self._archive_checkpoint(task, evidence)
            if evidence is None:
                return False
            files = {entry['name']: entry for entry in receipt['files']}
            if any(name not in files for name in spec['output_names']):
                raise DeliveryError('output_missing')
            if any(not OUTPUT_NAME.fullmatch(name) or Path(name).suffix.lower() not in MIMES for name in spec['output_names']):
                raise DeliveryError('artifact_type_unavailable')
            checkpoint = self.adapter.checkpoint(task, receipt)
            # Deterministic upload IDs are enforced by Controller. Retrying the
            # exact frozen file cannot append a second upload or emit a message.
            upload_ids = []
            for name in spec['output_names']:
                current = self.store.work_task(task['owner_ref'], task['task_id'])
                if current['control_pending']:
                    return self._stopped(current, claim, receipt)
                upload_ids.append(self.callback.upload_file(task, checkpoint,
                    filename=name, mime_type=MIMES[Path(name).suffix.lower()], expected_sha256=files[name]['sha256']))
            summaries = [name for name in spec['output_names'] if name.lower().endswith('.txt')]
            summary = '任务已生成：' + '、'.join(spec['output_names'])
            if summaries:
                with self.callback._file(checkpoint, summaries[0]) as source:
                    raw_report = source.read(48001)
                report = raw_report.decode('utf-8-sig') if len(raw_report) <= 48000 else None
                if report is not None and (not report or '\x00' in report):
                    raise DeliveryError('result_text_invalid')
                # A longer report remains complete as its attached file.
                summary = report if report is not None and len(report) <= 12000 else summary + '\n完整报告见附件。'
            event_id = 'WE-' + hashlib.sha256(canonical({
                'task': task['task_id'], 'lease': claim['lease_ref'], 'uploads': upload_ids, 'result': summary}).encode()).hexdigest()
            evidence = {**evidence, 'upload_ids': upload_ids, 'completion_event_id': event_id, 'summary': summary}
            recorded = self.store.record_work_evidence(task['owner_ref'], 'event:' + claim['lease_ref'],
                task_ref=task['task_id'], lease_ref=claim['lease_ref'], lease_epoch=claim['lease_epoch'],
                phase='delivery_wait', evidence=evidence)
            if recorded['status'] != 'accepted':
                return False
            # A fresh pass arbitrates cancellation before the idempotent POST.
            return self._reconcile_delivery(self.store.work_task(task['owner_ref'], task['task_id']))
        except ArchiveError:
            # Preserve the stopped checkpoint and wait for exact immutable
            # replica reconciliation. No upload or completion may pass it.
            return False
        except CallbackError as exc:
            # Immutable uploads can be reconciled on the next pass; credentials
            # and tokens are never stored in task evidence.
            if exc.code.startswith('artifact_'):
                return self._finish(task, claim, 'recovery_required', {'error_code': 'artifact_validation_failed'})
            return False
        except DeliveryError as exc:
            if exc.code in TRANSIENT:
                return False
            if exc.code == 'control_pending':
                return self._stopped(self.store.work_task(task['owner_ref'], task['task_id']), claim, receipt)
            else:
                return self._finish(task, claim, 'recovery_required', {'error_code': 'artifact_validation_failed'})
        except Exception:
            return self._finish(task, claim, 'recovery_required', {'error_code': 'artifact_delivery_unconfirmed'})

    def _reconcile_delivery(self, task: dict[str, Any]) -> bool:
        if self._registered_internal(task):
            return self._finish(task, self._claim(task), 'recovery_required',
                {'error_code': 'internal_delivery_forbidden'})
        outcome, claim = json.loads(task['runtime_result_json']), self._claim(task)
        event_id = outcome['completion_event_id']
        try:
            status = self.callback.event_status(event_id)
        except CallbackError as exc:
            if exc.code != 'callback_not_found':
                return False
            if task['control_pending']:
                # No event may be emitted by this coordinator while control is
                # pending. A previous POST could still be in flight: do not
                # promise suppression based on a single 404.
                return self._finish(task, claim, 'recovery_required', {'error_code': 'delivery_cancellation_unconfirmed'})
            try:
                # This endpoint guarantees uniqueness by event ID plus full
                # body digest. It only creates one durable Gateway event, even
                # if a prior request is still processing after a lost reply.
                self.callback.complete(task, event_id=event_id, result=outcome['summary'], upload_ids=outcome['upload_ids'])
            except CallbackError:
                return False
            return True
        if status.get('event_id') != event_id or status.get('status') not in {'preparing', 'pending', 'delivering', 'sent', 'suppressed', 'unknown'}:
            return self._finish(task, claim, 'recovery_required', {'error_code': 'delivery_status_invalid'})
        if task['control_pending']:
            if status['status'] in {'preparing', 'pending'}:
                try:
                    status = self.callback.cancel_event(event_id)
                    status = {'status': status['state']}
                except CallbackError:
                    return False
            if status['status'] == 'suppressed':
                return self._stopped(task, claim, outcome['execution'])
            else:
                return self._finish(task, claim, 'recovery_required', {'error_code': 'delivery_cancellation_unconfirmed'})
        if status['status'] == 'preparing':
            try:
                self.callback.complete(task, event_id=event_id, result=outcome['summary'], upload_ids=outcome['upload_ids'])
            except CallbackError:
                return False
            return True
        if status['status'] in {'pending', 'delivering'}:
            return False
        if status['status'] == 'sent':
            try:
                return self._finish(task, claim, 'completed', {'exit_code': 0, 'validation': 'passed',
                    'delivery': 'gateway_sent', 'summary': outcome['summary'], 'artifact_refs': outcome['upload_ids'],
                    'checkpoint_digest': outcome['execution']['checkpoint_ref'],
                    **({'checkpoint_archive': outcome['checkpoint_archive']} if 'checkpoint_archive' in outcome else {})})
            except DeliveryError as exc:
                if exc.code != 'control_pending':
                    raise
                return self._finish(task, claim, 'recovery_required', {'error_code': 'delivery_cancellation_unconfirmed'})
        else:
            return self._finish(task, claim, 'recovery_required', {'error_code': 'delivery_outcome_unknown'})

    def _finish(self, task: dict[str, Any], claim: dict[str, Any], state: str, evidence: dict[str, Any]) -> bool:
        try:
            result = self.store.finish_work(task['owner_ref'], 'finish:' + claim['lease_ref'] + ':' + state,
                task_ref=task['task_id'], lease_ref=claim['lease_ref'], lease_epoch=claim['lease_epoch'],
                state=state, evidence=evidence)
        except DeliveryError as exc:
            if exc.code in TRANSIENT:
                return False
            raise
        return result['status'] == 'accepted'
