"""Image-owned RepoManager/Work/Pipeline composition and one bounded worker.

This is executable boot glue, not a dynamic plug-in loader or a production
builder implementation. Builder, independent observers and authorization /
validation gates must be real qualified trusted Ports supplied at image boot.
There is no option that turns a missing Port into a successful build/release.
"""
from contextlib import closing
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import stat
import threading
import time
from types import MappingProxyType

from .durability import digest
from .repo_manager import GitRuntime, RepoManager, _sha
from .repository_execution import ArchivedCodeChanges, SoftwareDeliveryPipeline, _fail
from .repository_sources import RepositorySources
from .source_archive import WorkSourceArchive
from .store import DeliveryError,reference


@dataclass(frozen=True)
class RepositoryTestBinding:
    """Image-owned binding; never deserialized from tasks/options or plug-ins."""
    owner_ref:str
    repo_ref:str
    repository_policy_digest:str
    runtime_id:str
    recipe:str
    source_prefix:str
    template_digest:str

    def __post_init__(self):
        from .repository_tests import program
        for value in (self.owner_ref,self.repo_ref,self.runtime_id):reference(value)
        _sha(self.repository_policy_digest);_sha(self.template_digest)
        if self.template_digest!=digest(program(self.source_prefix,self.recipe).encode()):
            raise DeliveryError('repository_validation_unavailable')


class SoftwareDeliveryWorker:
    """One worker consumes only explicit confirmed software.bind commands.

    It creates no tasks and changes no task mode. Unknown build/publish outcomes
    remain fenced by Pipeline/Publisher; a restart cannot replay either effect.
    """
    def __init__(self,pipeline:SoftwareDeliveryPipeline,*,job_seconds=900,poll_seconds=5,admission_gate=None):
        if (not isinstance(pipeline,SoftwareDeliveryPipeline)
                or type(job_seconds) not in {float,int} or not 1<=job_seconds<=3600
                or type(poll_seconds) not in {float,int} or not .05<=poll_seconds<=60
                or admission_gate is not None and not callable(admission_gate)):_fail()
        self.pipeline,self.store=pipeline,pipeline.store
        self.job_seconds,self.poll_seconds=job_seconds,poll_seconds
        self._stop=threading.Event();self._thread=None;self._fd=None
        self._busy=threading.Lock();self._after={};self.last_error=None
        self._dispatch_started=False
        self._admission_gate=admission_gate
        self.request_handler=None  # Explicit original-worker handoff by trusted boot.

    def run_once(self):
        self._dispatch_started=True
        if self._admission_gate is not None and self._admission_gate() is not True:return False
        if self.store.recovery_mode or not self._busy.acquire(blocking=False):return False
        try:
            if self.request_handler is not None and self.request_handler(deadline=time.monotonic()+self.job_seconds):
                return True
            with closing(self.store._connect()) as connection:
                if connection.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone():return False
                rows=connection.execute("SELECT c.owner_ref,c.idempotency_key,e.body_json FROM commands c "
                    "JOIN events e USING(event_id) JOIN durability d USING(event_id) "
                    "WHERE e.kind='software.bind' AND d.state='confirmed' "
                    "AND NOT EXISTS (SELECT 1 FROM commands finished JOIN events fe USING(event_id) "
                    "WHERE fe.kind='software.release' AND finished.owner_ref=c.owner_ref "
                    "AND finished.idempotency_key='software-release:'||json_extract(e.body_json,'$.result.binding_ref')) "
                    "ORDER BY e.sequence LIMIT 1024").fetchall()
                for row in rows:
                    document=json.loads(row['body_json'])['result']
                    binding=document['binding'];ref=document['binding_ref']
                    if self._after.get(ref,0)>time.monotonic():continue
                    final=connection.execute("SELECT 1 FROM commands c JOIN events e USING(event_id) "
                        "WHERE e.kind='software.release' AND c.owner_ref=? AND c.idempotency_key=?",
                        (row['owner_ref'],'software-release:'+ref)).fetchone()
                    if final:continue
                    code=connection.execute('SELECT state,phase,revision,control_pending FROM tasks WHERE owner_ref=? AND task_id=?',
                        (row['owner_ref'],binding['code_task_ref'])).fetchone()
                    if (code is None or code['revision']!=binding['code_revision'] or code['control_pending']
                            or code['state'] not in {'running','completed'}
                            or code['phase'] not in {'checkpoint_archive','artifact_upload','delivery_wait'}):continue
                    chosen=(row['owner_ref'],row['idempotency_key'],ref)
                    break
                else:return False
            owner,key,ref=chosen
            try:
                self.pipeline.run(owner,binding_key=key,deadline=time.monotonic()+self.job_seconds)
                self.last_error=None
            except Exception:
                # Detailed exceptions may contain source/provider data. The
                # original claim/event/archive is the recoverable evidence.
                self.last_error='software_execution_unavailable'
                self._after[ref]=time.monotonic()+max(30,self.poll_seconds)
            return True
        finally:self._busy.release()

    def start(self):
        if (self._admission_gate is not None and self._admission_gate() is not True
                or self._thread is not None or self._fd is not None or not all(callable(port) for port in
                (self.pipeline.build_executor,self.pipeline.build_observer,self.pipeline.target_observer))):_fail()
        path=self.store.path.parent/'software-worker.lock'
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600
                    or info.st_uid!=os.getuid() or info.st_nlink!=1):_fail()
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except Exception:
            os.close(fd);_fail()
        self._fd=fd
        def loop():
            while not self._stop.is_set():
                try:self.run_once()
                except Exception:self.last_error='software_execution_unavailable'
                self._stop.wait(self.poll_seconds)
        self._thread=threading.Thread(target=loop,name='software-delivery',daemon=True)
        try:self._thread.start()
        except Exception:
            os.close(self._fd);self._fd=None;self._thread=None;_fail()

    def close(self,*,timeout_seconds=1):
        self._stop.set()
        if self._thread is not None:self._thread.join(timeout_seconds)
        if self._thread is not None and self._thread.is_alive():
            # Preserve singleton ownership when the original trusted Port has
            # not settled; another worker must not replace an uncertain one.
            return False
        if self._fd is not None:os.close(self._fd);self._fd=None
        return True


class RepositoryExecutionFactory:
    """Concrete trusted factory for QualifiedExecution.repository_factory.

    Required objects are supplied by image-owned boot code. No module names,
    source paths, credentials, policies, scanners or builders come from tasks.
    The same factory is later attached to QualifiedExecution.source_archive.
    """
    def __init__(self,*,policies,git_binary:Path,authorization_gate,validation_gate=None,
                 workspace_bindings,quiescence_gate,input_gate,build_executor,
                 build_observer,target_observer,source_kind='directory-v1',git_runtime=None):
        if (not isinstance(git_binary,Path) or not git_binary.is_absolute()
                or source_kind not in {'directory-v1','managed-sealed-v2'}
                or git_runtime is not None and not isinstance(git_runtime,GitRuntime)
                or git_runtime is not None and Path(git_runtime.git_binary)!=git_binary
                or source_kind=='directory-v1' and not all(callable(port) for port in (workspace_bindings,quiescence_gate))
                or source_kind=='managed-sealed-v2' and (workspace_bindings is not None or quiescence_gate is not None)
                or validation_gate is not None and not callable(validation_gate)
                or not all(callable(port) for port in (authorization_gate,input_gate,build_executor,build_observer,target_observer))):_fail()
        self.policies,self.git_binary=policies,git_binary
        self.source_kind,self.git_runtime=source_kind,git_runtime
        self.authorization_gate,self.validation_gate=authorization_gate,validation_gate
        self.workspace_bindings,self.quiescence_gate,self.input_gate=workspace_bindings,quiescence_gate,input_gate
        self.build_executor,self.build_observer,self.target_observer=build_executor,build_observer,target_observer
        self.sources=self.pipeline=self.worker=None
        self._validation_state='unattached' if validation_gate is None else 'external'
        self._validation_ports=self._validation_bindings=self._validation_gates=None
        self._validation_policy=None

    def __call__(self,store,archive,state_root):
        if self.sources is not None or Path(state_root)!=store.path.parent:_fail()
        manager=RepoManager(store,Path(state_root)/'repositories',policies=self.policies,
            git_runtime=self.git_runtime if self.git_runtime is not None else GitRuntime(str(self.git_binary)),authorization_gate=self.authorization_gate,
            validation_gate=self.validation_gate if self.validation_gate is not None else self._deferred_validation)
        if self.source_kind=='managed-sealed-v2':
            from .sealed_repository_sources import ManagedSealedRepositorySources
            self.sources=ManagedSealedRepositorySources(manager,archive,input_gate=self.input_gate)
        else:
            self.sources=RepositorySources(manager,archive,workspace_bindings=self.workspace_bindings,
                quiescence_gate=self.quiescence_gate,input_gate=self.input_gate)
        return self.sources

    def _validation_port(self,context):
        if self._validation_state!='ready':raise DeliveryError('repository_validation_unavailable')
        self._validation_policy.verify()
        key=(context['owner_ref'],context['repo_id'])
        binding=self._validation_bindings.get(key)
        if binding is None or context['policy_digest']!=binding.repository_policy_digest:
            raise DeliveryError('repository_validation_unavailable')
        with closing(self.sources.store._connect()) as connection:
            repository=self.sources.store._owned(connection,'repositories','repo_id',binding.owner_ref,binding.repo_ref)
        if self.sources.manager._policy(repository).sha256!=binding.repository_policy_digest:
            raise DeliveryError('repository_validation_unavailable')
        return key,self._validation_ports[key]

    def _deferred_validation(self,context):
        key,_=self._validation_port(context)
        return self._validation_gates[key](context)

    def _internal_result(self,task,evidence):
        from .repository_tests import registered_test
        original=registered_test(self.sources.store,task)
        if original is None:raise DeliveryError('repository_validation_unavailable')
        identity=original['result']['identity']
        _,port=self._validation_port(identity['context'])
        if (identity['runtime_id']!=port.runtime_id or identity['recipe']!=port.recipe
                or identity['template_digest']!=port.template_digest):
            raise DeliveryError('repository_validation_unavailable')
        return port(task,evidence)

    def attach_validation(self,execution,coordinator,*,policy,bindings,scanner_digest,runtime_qualification_digest):
        """One attempt by the serial trusted boot, before any scheduler starts.

        QualifiedExecution must already have built the original source archive.
        No task, VM, request, network client or worker is started here. Failure
        permanently closes this factory instance; no replacement identity is
        generated. An explicitly supplied legacy gate cannot be replaced.
        """
        if self._validation_state!='unattached':raise DeliveryError('repository_validation_unavailable')
        self._validation_state='failed'
        from .coordinator import WorkCoordinator
        from .execution_composition import QualifiedExecution
        from .repository_tests import RepositoryTestPort
        from .repository_validation import CandidateReader,RepositoryValidationGate,SCANNER_DIGEST,SourceScanner
        if (type(execution) is not QualifiedExecution or type(coordinator) is not WorkCoordinator
                or self.sources is None or execution.repository_factory is not self
                or execution.store is not self.sources.store or execution.repository_sources is not self.sources
                or type(execution.source_archive) is not WorkSourceArchive
                or execution.source_archive.repository_sources is not self.sources
                or execution.source_archive.store is not self.sources.store
                or execution.source_archive.archive is not self.sources.archive
                or coordinator.store is not execution.store or coordinator.adapter is not execution.adapter
                or coordinator.runner_ref!=execution.settings.runner_ref
                or coordinator.source_archive is not execution.source_archive
                or coordinator.checkpoint_archive is not self.sources.archive
                or coordinator.internal_result_handler is not None or coordinator._shutdown.is_set()
                or self.worker is not None and (self.worker._thread is not None or self.worker._fd is not None
                    or self.worker._dispatch_started or self.worker._stop.is_set())
                or not callable(getattr(policy,'verify',None)) or type(bindings) is not tuple
                or not 1<=len(bindings)<=32 or any(type(value) is not RepositoryTestBinding for value in bindings)
                or scanner_digest!=SCANNER_DIGEST):
            raise DeliveryError('repository_validation_unavailable')
        _sha(runtime_qualification_digest)
        # The same persistent dispatch lock is reused after normal restart;
        # only the process-local flag means this Coordinator already ran.
        with coordinator._dispatch():
            if coordinator._dispatch_started:raise DeliveryError('repository_validation_unavailable')
            policy.verify()
            ports={};gates={};mapping={}
            for binding in bindings:
                key=(binding.owner_ref,binding.repo_ref)
                if key in mapping:raise DeliveryError('repository_validation_unavailable')
                with closing(self.sources.store._connect()) as connection:
                    repository=self.sources.store._owned(connection,'repositories','repo_id',binding.owner_ref,binding.repo_ref)
                if self.sources.manager._policy(repository).sha256!=binding.repository_policy_digest:
                    raise DeliveryError('repository_validation_unavailable')
                port=RepositoryTestPort(self.sources.store,execution.source_archive,runtime_id=binding.runtime_id,
                    runtime_qualification_digest=runtime_qualification_digest,source_prefix=binding.source_prefix,
                    policy=policy,recipe=binding.recipe)
                if port.template_digest!=binding.template_digest:raise DeliveryError('repository_validation_unavailable')
                port.attach_execution(execution)
                ports[key]=port;mapping[key]=binding
                gates[key]=RepositoryValidationGate(CandidateReader(self.sources.manager,SourceScanner()),port)
            policy.verify()
            if (coordinator._dispatch_started or coordinator._shutdown.is_set()
                    or self.worker is not None and (self.worker._thread is not None or self.worker._fd is not None
                        or self.worker._dispatch_started or self.worker._stop.is_set())):
                raise DeliveryError('repository_validation_unavailable')
            self._validation_ports=MappingProxyType(ports)
            self._validation_gates=MappingProxyType(gates)
            self._validation_bindings=MappingProxyType(mapping)
            self._validation_policy=policy
            coordinator.internal_result_handler=self._internal_result
            self._validation_state='ready'
        return self

    def attach(self,source_archive:WorkSourceArchive):
        if (self._validation_state=='failed' or self.pipeline is not None or not isinstance(source_archive,WorkSourceArchive)
                or self.sources is None or source_archive.repository_sources is not self.sources
                or source_archive.store is not self.sources.store or source_archive.archive is not self.sources.archive):_fail()
        changes=ArchivedCodeChanges(source_archive)
        if self.source_kind=='managed-sealed-v2':
            from .sealed_repository_sources import CompleteStoppedCheckpointChanges
            changes=CompleteStoppedCheckpointChanges(source_archive)
        self.pipeline=SoftwareDeliveryPipeline(self.sources.manager,self.sources.archive,
            changes=changes,build_executor=self.build_executor,
            build_observer=self.build_observer,target_observer=self.target_observer)
        self.worker=SoftwareDeliveryWorker(self.pipeline,admission_gate=(
            None if self.validation_gate is not None else lambda:self._validation_state=='ready'))
        return self.worker

    def release_reader(self,release_ref,*,deadline):
        if self.pipeline is None:_fail()
        return self.pipeline.release_reader(release_ref,deadline=deadline)

    def source_qualifier(self,target_ref,release_ref,*,deadline):
        if self.pipeline is None:_fail()
        return self.pipeline.source_qualifier(target_ref,release_ref,deadline=deadline)
