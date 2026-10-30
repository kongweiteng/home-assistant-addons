"""Trusted repository backup evidence before a canonical Work claim.

No checkout paths, Git config or raw repository bytes are exposed to a guest.
Workspace bindings come from the administrator/Runner, never task arguments.
"""
from contextlib import closing
import hashlib
import json

from .durability import digest
from .object_archive import ArchiveError, ObjectArchive
from .repo_manager import RepoManager, RepositoryArchive, RepositoryWorkspace
from .repository_inputs import sealed_repository_inputs
from .store import DeliveryError, canonical


def task_repositories(store, task, *, stopped=False):
    current=store.work_task(task['owner_ref'],task['task_id'])
    if any(current[key]!=task[key] for key in ('owner_ref','task_id','revision','workspace_ref','project_id','work_json')):
        raise DeliveryError('revision_conflict')
    allowed={'running','completed'} if stopped else {'queued'}
    if (current['state'] not in allowed or current['control_pending']
            or stopped and (current['mode']!='work' or json.loads(current['work_json'])['profile']!='code')):
        raise DeliveryError('transition_invalid')
    with closing(store._connect()) as connection:
        return tuple(row['repo_id'] for row in connection.execute(
            'SELECT repo_id FROM task_repositories WHERE owner_ref=? AND task_id=? ORDER BY repo_id',
            (task['owner_ref'],task['task_id'])))


class RepositorySources:
    def __init__(self,manager:RepoManager,archive:ObjectArchive,*,workspace_bindings,quiescence_gate,
                 input_gate=None,source_kind='directory-v1'):
        if (not isinstance(manager,RepoManager) or not isinstance(archive,ObjectArchive)
                or archive.replica is not manager.store.replica
                or archive.local_domain!=manager.store.local_domain
                or archive.replica_domain!=manager.store.replica_domain
                or source_kind not in {'directory-v1','managed-sealed-v2'}
                or source_kind=='directory-v1' and (not callable(workspace_bindings) or not callable(quiescence_gate))
                or source_kind=='managed-sealed-v2' and (workspace_bindings is not None or quiescence_gate is not None)
                or input_gate is not None and not callable(input_gate)):
            raise DeliveryError('repository_sources_invalid')
        self.manager,self.store,self.archive=manager,manager.store,archive
        self.workspace_bindings,self.quiescence_gate=workspace_bindings,quiescence_gate
        self.input_gate=input_gate
        self.source_kind=source_kind

    def freeze(self,task):
        repositories=task_repositories(self.store,task)
        if not repositories:return ()
        self.prepare(task,repositories)
        bindings=self.workspace_bindings(dict(task))
        if (not isinstance(bindings,tuple) or len(bindings)!=len(repositories)
                or any(type(binding) is not RepositoryWorkspace for binding in bindings)
                or tuple(sorted(binding.repo_ref for binding in bindings))!=repositories
                or any((binding.owner_ref,binding.task_ref,binding.revision)!=
                       (task['owner_ref'],task['task_id'],task['revision']) for binding in bindings)):
            raise ArchiveError('repository_sources_binding_invalid')
        archives=RepositoryArchive(self.manager,self.archive,workspaces=bindings,
                                   quiescence_gate=self.quiescence_gate)
        evidence=[]
        for binding in sorted(bindings,key=lambda item:item.repo_ref):
            identity={key:task[key] for key in ('owner_ref','task_id','revision')}
            identity['repo_ref']=binding.repo_ref
            key='repository-source:'+hashlib.sha256(canonical(identity).encode()).hexdigest()
            result=archives.save(task['owner_ref'],key,task_ref=task['task_id'],
                expected_revision=task['revision'],repo_ref=binding.repo_ref,workspace_ref=binding.workspace_ref)
            if result.get('status')!='accepted' or result.get('durability')!='replicated':
                raise ArchiveError('repository_sources_pending')
            # Read the canonical confirmed event instead of trusting a copied
            # result dictionary. No independent task log is created.
            with closing(self.store._connect()) as connection:
                row=connection.execute('SELECT e.body_json,d.state FROM events e JOIN durability d USING(event_id) '
                    'WHERE e.event_id=? AND e.owner_ref=? AND e.task_id=? AND e.kind=?',
                    (result['event_id'],task['owner_ref'],task['task_id'],'repository.archive')).fetchone()
            if row is None or row['state']!='confirmed':
                raise ArchiveError('repository_sources_pending')
            event=json.loads(row['body_json'])
            if event['arguments']!={'task_ref':task['task_id'],'expected_revision':task['revision'],
                    'repo_ref':binding.repo_ref,'workspace_ref':binding.workspace_ref}:
                raise ArchiveError('repository_sources_scope_invalid')
            recorded=event['result']
            if any(recorded.get(field)!=result.get(field) for field in
                   ('backup_ref','context_digest','state_sha256','archive')):
                raise ArchiveError('repository_sources_scope_invalid')
            evidence.append({key:result[key] for key in
                ('repo_id','workspace_ref','backup_ref','context_digest','state_sha256','archive','event_id')})
        if task_repositories(self.store,task)!=repositories:
            raise DeliveryError('revision_conflict')
        return tuple(evidence)

    def prepare(self,task,repositories=None):
        """Use the real manager's frozen per-task baseline, never a live checkout.

        First admission syncs one registered baseline and exports one immutable
        source bundle. Retries use the same original command and cannot select a
        newer remote baseline under an already frozen task revision.
        """
        repositories=task_repositories(self.store,task) if repositories is None else repositories
        for repo in repositories:
            identity={key:task[key] for key in ('owner_ref','task_id','revision')}
            identity['repo_ref']=repo
            suffix=hashlib.sha256(canonical(identity).encode()).hexdigest()
            with closing(self.store._connect()) as connection:
                registered=self.store._owned(connection,'repositories','repo_id',task['owner_ref'],repo)
            result=self.manager.sync(task['owner_ref'],'source-sync:'+suffix,
                repo_ref=repo,expected_revision=registered['revision'])
            if result.get('status')!='accepted' or result.get('durability')!='replicated':
                raise ArchiveError('repository_sources_pending')
            result=self.manager.export_bundle(task['owner_ref'],'source-export:'+suffix,
                task_ref=task['task_id'],expected_revision=task['revision'],repo_ref=repo)
            if result.get('status')!='accepted' or result.get('durability')!='replicated':
                raise ArchiveError('repository_sources_pending')

    def inputs(self,task,evidence):
        """Guest bytes derive solely from the confirmed original sealed backup."""
        repositories=task_repositories(self.store,task)
        if not repositories:
            if evidence:raise DeliveryError('repository_sources_scope_invalid')
            return {},()
        if not callable(self.input_gate):raise DeliveryError('repository_input_gate_unavailable')
        if (not isinstance(evidence,tuple) or len(evidence)!=len(repositories)
                or tuple(sorted(item['repo_id'] for item in evidence))!=repositories):
            raise DeliveryError('repository_sources_scope_invalid')
        bindings=self.workspace_bindings(dict(task))
        archives=RepositoryArchive(self.manager,self.archive,workspaces=bindings,
                                   quiescence_gate=self.quiescence_gate)
        files,views={},[]
        for item in evidence:
            identity={key:task[key] for key in ('owner_ref','task_id','revision')}
            identity['repo_ref']=item['repo_id']
            key='repository-source:'+hashlib.sha256(canonical(identity).encode()).hexdigest()
            recorded,_,_,_=archives._record(task['owner_ref'],key)
            if any(recorded.get(field)!=item.get(field) for field in
                   ('repo_id','workspace_ref','backup_ref','context_digest','state_sha256','archive')):
                raise DeliveryError('repository_sources_scope_invalid')
            view=sealed_repository_inputs(archives,task['owner_ref'],key,exposure_gate=self.input_gate)
            prefix='repositories/'+item['repo_id']
            files[prefix+'.tar']=view.package()
            files[prefix+'.json']=canonical(view.metadata).encode('utf-8')
            views.append(dict(repo_id=item['repo_id'],view_digest=digest(canonical(view.metadata).encode()),
                              package_digest=digest(files[prefix+'.tar'])))
        if task_repositories(self.store,task)!=repositories:raise DeliveryError('revision_conflict')
        return files,tuple(views)
