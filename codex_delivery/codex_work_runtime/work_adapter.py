"""Trusted in-process Delivery -> WorkExecutor adapter.

This is not a new remote protocol, Runner identity, task queue or task database.
The existing Remote Work v2/Relay remains unchanged and carries no code/files.
An authenticated HA composition supplies canonical task snapshots, leases and
read-only identity/lease gates; model tools must never call this interface.
"""
from __future__ import annotations

from contextlib import closing
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Mapping

from .errors import WorkRuntimeError
from .work_execution import (WorkExecutor, WorkLimits, WorkRequest, WorkRuntime,
                             _checkpoint_ref, _checkpoint_tree)


REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
CHECKPOINT = re.compile(r"CP-[a-f0-9]{64}\Z")
SPEC_FIELDS = {'profile','runtime_id','program','output_names','input_refs'}


def _fail(code: str, *, uncertain: bool = False) -> WorkRuntimeError:
    return WorkRuntimeError(code,"受限执行适配未通过固定策略。",uncertain=uncertain)


def _ref(value: Any) -> str:
    if not isinstance(value,str) or REF.fullmatch(value) is None:
        raise _fail('work_adapter_reference_invalid')
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)


class WorkExecutorAdapter:
    """Admin-fixed runtimes/budgets with live identity and canonical lease gates.

    identity_gate(runner_ref) returns a normalized read-only snapshot containing
    runner_ref, registered:bool, enabled:bool and state. New work requires idle.
    An own busy snapshot may additionally bind task_ref and lease_ref. Exceptions,
    disabled, unknown, offline, or recovery_required states fail closed.

    lease_gate(owner, task, lease, epoch) must consult the canonical Delivery
    assignment (including this Runner binding). It is mandatory for execution.
    Cancellation is only a stop request, never evidence that a process stopped.
    Checkpoint reads survive adapter restart and never clear recovery state.
    """

    def __init__(self, executor: WorkExecutor, *, runner_ref: str,
                 runtimes: Mapping[str,WorkRuntime], limits: Mapping[str,WorkLimits],
                 identity_gate: Callable[[str],Mapping[str,Any]],
                 lease_gate: Callable[[str,str,str,int],bool] | None = None):
        self.executor=executor
        self.runner_ref=_ref(runner_ref)
        if not callable(identity_gate) or (lease_gate is not None and not callable(lease_gate)):
            raise _fail('work_adapter_gate_invalid')
        self.identity_gate,self.lease_gate=identity_gate,lease_gate
        self.runtimes=dict(runtimes)
        self.limits=dict(limits)
        if not self.runtimes or not self.limits:
            raise _fail('work_adapter_policy_invalid')
        for key,runtime in self.runtimes.items():
            if not isinstance(runtime,WorkRuntime) or key!=runtime.runtime_ref:
                raise _fail('work_adapter_policy_invalid')
            runtime.validate()
            backend_runtimes=getattr(executor.backend,'runtimes',None)
            if backend_runtimes is not None and backend_runtimes.get(key)!=runtime:
                raise _fail('work_adapter_policy_invalid')
        for profile,budget in self.limits.items():
            if profile not in {'document','data','code','browser','preview'} or not isinstance(budget,WorkLimits):
                raise _fail('work_adapter_policy_invalid')
            budget.validate(profile)
        self._lock=threading.Lock()
        self._active: dict[tuple[str,str,str,int],threading.Event]={}

    def _identity(self, *, task: Mapping[str,Any] | None = None,
                  claim: Mapping[str,Any] | None = None) -> None:
        try:
            snapshot=self.identity_gate(self.runner_ref)
            if (not isinstance(snapshot,Mapping) or snapshot.get('runner_ref')!=self.runner_ref
                    or snapshot.get('registered') is not True or snapshot.get('enabled') is not True):
                raise ValueError('identity')
            if snapshot.get('state')=='idle':
                return
            if (snapshot.get('state')=='busy' and task is not None and claim is not None
                    and snapshot.get('task_ref')==task['task_id']
                    and snapshot.get('lease_ref')==claim['lease_ref']):
                return
        except Exception:
            raise _fail('work_runner_identity_unavailable') from None
        raise _fail('work_runner_identity_unavailable')

    def _policy(self, profile: str, runtime_id: str) -> tuple[WorkRuntime,WorkLimits]:
        runtime=self.runtimes.get(runtime_id)
        budget=self.limits.get(profile)
        if runtime is None or budget is None or profile not in runtime.profiles:
            raise _fail('work_runtime_not_registered')
        return runtime,budget

    def qualifies(self, profile: str, runtime_id: str) -> bool:
        try:
            self._identity()
            self._policy(profile,runtime_id)
            return self.lease_gate is not None
        except (WorkRuntimeError,TypeError):
            return False

    def _spec(self, task: Mapping[str,Any], spec: Mapping[str,Any]) -> None:
        if (not isinstance(spec,dict) or set(spec)!=SPEC_FIELDS
                or not isinstance(spec.get('program'),str)
                or not isinstance(task.get('work_json'),str)):
            raise _fail('work_adapter_spec_invalid')
        try:
            if _canonical(json.loads(task['work_json']))!=_canonical(spec):
                raise ValueError('changed')
        except (ValueError,TypeError):
            raise _fail('work_adapter_spec_invalid') from None
        self._policy(spec['profile'],spec['runtime_id'])

    @staticmethod
    def _key(task: Mapping[str,Any], claim: Mapping[str,Any]) -> tuple[str,str,str,int]:
        for value in (task['owner_ref'],task['task_id'],task['workspace_ref'],claim['lease_ref']):
            _ref(value)
        if type(claim.get('lease_epoch')) is not int or claim['lease_epoch']<1:
            raise _fail('work_adapter_claim_invalid')
        return task['owner_ref'],task['task_id'],claim['lease_ref'],claim['lease_epoch']

    def _request(self, task: dict[str,Any], claim: dict[str,Any], spec: dict[str,Any]) -> WorkRequest:
        self._key(task,claim)
        self._spec(task,spec)
        if (claim.get('status')!='accepted' or claim.get('execution_enabled') is not True
                or claim.get('state')!='running' or task.get('mode')!='work'
                or task.get('state') not in {'queued','running'}
                or claim.get('task_id')!=task['task_id'] or claim.get('workspace_ref')!=task['workspace_ref']
                or type(task.get('revision')) is not int or task['revision']<1
                or type(claim.get('revision')) is not int or claim['revision']!=task['revision']
                or claim.get('runner_ref')!=self.runner_ref
                or task.get('assigned_runner_ref') not in {None,self.runner_ref}):
            raise _fail('work_adapter_claim_invalid')
        try:
            expires=dt.datetime.fromisoformat(claim['lease_expires_at'])
        except (KeyError,ValueError,TypeError):
            raise _fail('work_adapter_claim_invalid') from None
        _,budget=self._policy(spec['profile'],spec['runtime_id'])
        request=WorkRequest(task['owner_ref'],task['task_id'],task['workspace_ref'],
            task['revision'],spec['profile'],spec['runtime_id'],claim['lease_ref'],
            claim['lease_epoch'],expires,budget)
        request.validate(self.executor.now())
        return request

    def _lease(self, task: dict[str,Any], claim: dict[str,Any]) -> bool:
        try:
            return (self.lease_gate is not None and self.lease_gate(task['owner_ref'],task['task_id'],
                    claim['lease_ref'],claim['lease_epoch']) is True)
        except Exception:
            return False

    def execute(self, task: dict[str,Any], claim: dict[str,Any], spec: dict[str,Any],
                inputs: dict[str,bytes]) -> dict[str,Any]:
        request=self._request(task,claim,spec)
        self._identity(task=task,claim=claim)
        if not self._lease(task,claim):
            raise _fail('work_adapter_lease_fenced')
        if not isinstance(inputs,dict):
            raise _fail('work_input_invalid')
        key=self._key(task,claim)
        cancelled=threading.Event(); finished=threading.Event()
        with self._lock:
            if self._active:
                raise _fail('work_runner_busy')
            self._active[key]=cancelled
        def watch() -> None:
            while not finished.wait(.05):
                try:
                    self._identity(task=task,claim=claim)
                    valid=self._lease(task,claim)
                except WorkRuntimeError:
                    valid=False
                if cancelled.is_set() or not valid:
                    cancelled.set()
                    try:
                        self.executor.cancel(request)
                    except WorkRuntimeError as error:
                        if error.code!='work_lease_mismatch':
                            return
                    except Exception:
                        return
        watcher=threading.Thread(target=watch,daemon=True,name='bounded-work-lease')
        watcher.start()
        try:
            if cancelled.is_set() or not self._lease(task,claim):
                raise _fail('work_adapter_lease_fenced')
            return self.executor.execute(request,program=spec['program'],inputs=dict(inputs))
        finally:
            finished.set(); watcher.join(1)
            with self._lock:
                self._active.pop(key,None)

    def _journal(self, task: dict[str,Any], *, lease: str | None = None,
                 epoch: int | None = None, execution: str | None = None) -> dict[str,Any]:
        for value in (task['owner_ref'],task['task_id'],task['workspace_ref']): _ref(value)
        path=self.executor.path
        if path.is_symlink() or not path.is_file():
            raise _fail('work_adapter_journal_invalid')
        try:
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)) as connection:
                connection.row_factory=sqlite3.Row
                row=connection.execute('''SELECT c.claim_ref,c.lease_id,c.epoch,c.stage,c.receipt_json,
                    w.owner_ref,w.task_ref,w.workspace_ref,w.highest_epoch,w.input_revision,w.checkpoint_ref
                    FROM claims c JOIN workspaces w USING(workspace_ref)
                    WHERE w.owner_ref=? AND w.task_ref=? AND w.workspace_ref=?
                    AND (? IS NULL OR c.lease_id=?) AND (? IS NULL OR c.epoch=?)
                    AND (? IS NULL OR c.claim_ref=?)''',
                    (task['owner_ref'],task['task_id'],task['workspace_ref'],lease,lease,epoch,epoch,
                     execution,execution)).fetchone()
        except (sqlite3.Error,OSError):
            raise _fail('work_adapter_journal_invalid') from None
        if row is None:
            raise _fail('work_lease_mismatch')
        return dict(row)

    def cancel(self, task: dict[str,Any], claim: dict[str,Any]) -> None:
        key=self._key(task,claim)
        with self._lock:
            active=self._active.get(key)
            if active is not None:
                active.set()
        try:
            row=self._journal(task,lease=claim['lease_ref'],epoch=claim['lease_epoch'])
        except WorkRuntimeError as error:
            if active is not None and error.code=='work_lease_mismatch':
                return # Watcher retries after the executor commits its claim.
            raise
        if row['stage']=='settled':
            return
        # WorkExecutor.cancel matches owner/task/workspace/epoch/lease in the
        # existing journal. It does not clear recovery_required or rerun work.
        # A stop request needs no still-enabled execution runtime or live lease.
        # The executor's cancel method consumes only this exact journal fence;
        # this synthetic object is never passed to execute or validated as one.
        request=WorkRequest(task['owner_ref'],task['task_id'],task['workspace_ref'],row['input_revision'],
            'data','cancel-only',claim['lease_ref'],claim['lease_epoch'],self.executor.now())
        try:
            self.executor.cancel(request)
        except sqlite3.Error:
            raise _fail('work_adapter_journal_invalid') from None

    def checkpoint(self, task: dict[str,Any], receipt: dict[str,Any]) -> Path:
        """Read settled exact evidence after restart; never accept model paths."""
        if (not isinstance(receipt,dict) or type(receipt.get('version')) is not int or receipt['version']!=1
                or receipt.get('checkpoint_confirmed') is not True
                or not isinstance(receipt.get('checkpoint_ref'),str)
                or CHECKPOINT.fullmatch(receipt['checkpoint_ref']) is None
                or receipt.get('workspace_ref')!=task.get('workspace_ref')
                or type(receipt.get('input_revision')) is not int
                or type(task.get('revision')) is not int
                or not 1 <= receipt['input_revision'] <= task['revision']
                or type(receipt.get('assignment_epoch')) is not int):
            raise _fail('work_adapter_receipt_invalid')
        row=self._journal(task,epoch=receipt['assignment_epoch'],execution=_ref(receipt.get('execution_ref')))
        try:
            # Pause/cancel/append advance the canonical row while the original
            # execution is stopping. Its journal and archive keep that original
            # revision; only the same still-bound assignment may read it.
            if (receipt['input_revision'] != task['revision'] and
                    (task.get('control_pending') not in {'pause','cancel','revise'}
                     or task.get('lease_epoch') != row['epoch']
                     or task.get('lease_ref') != row['lease_id'])):
                raise ValueError('revision')
            if (row['stage']!='settled' or _canonical(json.loads(row['receipt_json']))!=_canonical(receipt)
                    or row['highest_epoch']!=row['epoch'] or row['input_revision']!=receipt['input_revision']
                    or row['checkpoint_ref']!=receipt['checkpoint_ref']):
                raise ValueError('receipt')
            spec=json.loads(task['work_json']); self._spec(task,spec)
            _,budget=self._policy(spec['profile'],spec['runtime_id'])
            key=hashlib.sha256((task['owner_ref']+'\n'+task['workspace_ref']).encode()).hexdigest()
            path=self.executor.root/'workspaces'/key/'checkpoints'/receipt['checkpoint_ref']
            for parent in [path,*path.parents]:
                if parent.is_symlink(): raise ValueError('symlink')
                if parent==self.executor.root: break
            if not path.is_dir(): raise ValueError('missing')
            tree=_checkpoint_tree(path,budget)
            if _checkpoint_ref(tree)!=receipt['checkpoint_ref'] or tree['files']!=receipt['files']:
                raise ValueError('integrity')
            return path
        except (ValueError,TypeError,KeyError,OSError):
            raise _fail('work_adapter_receipt_invalid') from None
