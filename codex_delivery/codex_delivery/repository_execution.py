"""Explicit code-Work handoff to the original software task and release facts.

All authority, commands and evidence remain in Delivery's existing Store. Code
Work cannot write a repository or publish. This trusted coordinator needs a
separate RepoManager authorization for its original develop/deliver/deploy task,
a fixed build executor and independent current build/target observers. Missing
Ports are unavailable; config flags and guest-reported success are not facts.
"""
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import math
from pathlib import Path
import stat
import tempfile
import time
import zipfile

from .durability import digest
from .object_archive import ArchiveReceipt, ObjectArchive, _pairs
from .repo_manager import RepoManager, _private, _read, _sha
from .source_archive import WorkSourceArchive
from .store import DeliveryError, canonical, identifier, reference
from .release_authority import CanonicalReleaseAuthority


def _fail():
    raise DeliveryError('software_execution_unavailable') from None


def _deadline(value):
    if type(value) not in {int,float} or not math.isfinite(value) or time.monotonic() >= value:_fail()


def _accepted(result):
    if result.get('status')!='accepted' or result.get('durability')!='replicated':_fail()
    return result


def _confirmed(store,owner,key,kind):
    """Read original canonical confirmed evidence, with no retry or mutation."""
    with closing(store._connect()) as connection:
        row=connection.execute('SELECT c.*,e.kind,e.body_json,d.state FROM commands c '
            'JOIN events e USING(event_id) JOIN durability d USING(event_id) '
            'WHERE c.owner_ref=? AND c.idempotency_key=?',(owner,key)).fetchone()
    if row is None:return None
    event=json.loads(row['body_json'],object_pairs_hook=_pairs)
    result=json.loads(row['result_json'],object_pairs_hook=_pairs)
    if (row['kind']!=kind or row['state']!='confirmed' or event['owner_ref']!=owner
            or event['event_id']!=row['event_id'] or event['kind']!=kind or event['result']!=result
            or row['argument_digest']!=digest(canonical(dict(kind=kind,arguments=event['arguments'])).encode())):
        _fail()
    return dict(arguments=event['arguments'],result=result,event_id=row['event_id'])


@dataclass(frozen=True)
class BuildObservation:
    """Trusted observer's independently read immutable actual build evidence.

    The executor must not select the observer, qualification or build template.
    An observer raises on failed/unknown/missing builds instead of fabricating
    this receipt. qualification_digest identifies its fixed runtime qualification.
    """
    request_digest: str
    build_ref: str
    artifact_identity: dict
    permissions_digest: str
    qualification_digest: str
    evidence_digest: str

    def document(self):
        value=asdict(self)
        reference(self.build_ref)
        for name in ('request_digest','permissions_digest','qualification_digest','evidence_digest'):
            _sha(value[name])
        # Reuse the actual release contract's closed typed image identity check.
        CanonicalReleaseAuthority._validate_release(dict(schema_version=2,release_ref='validate',
            version='0',source_digest=self.request_digest,artifact_identity=self.artifact_identity,
            permissions_digest=self.permissions_digest,qualification_digest=self.qualification_digest),'validate')
        return json.loads(canonical(value))


class ArchivedCodeChanges:
    """Read candidate.zip only from a confirmed original stopped code archive.

    source_archive is the SAME real WorkSourceArchive used before Work claim.
    Files never come from Controller upload URLs, a live workspace or resolver.
    """
    def __init__(self,source_archive:WorkSourceArchive):
        if not isinstance(source_archive,WorkSourceArchive):_fail()
        self.sources=source_archive
        self.store,self.archive=source_archive.store,source_archive.archive
        self.root=self.store.path.parent/'software-readback'

    def __call__(self,binding,*,deadline):
        _deadline(deadline)
        owner=binding['owner_ref'];task=self.store.work_task(owner,binding['code_task_ref'])
        if (task['revision']!=binding['code_revision'] or task['project_id']!=binding['project_ref']
                or task['control_pending'] or task['state'] not in {'running','completed'}
                or digest(task['work_json'].encode())!=binding['code_spec_digest']):_fail()
        spec=json.loads(task['work_json'])
        if spec['profile']!='code' or 'candidate.zip' not in spec['output_names']:_fail()
        inputs=self.sources.stopped_inputs(task,spec)
        if inputs is None:_fail()
        for source in binding['repositories']:
            metadata=json.loads(inputs['repositories/'+source['repo_id']+'.json'],object_pairs_hook=_pairs)
            if metadata['baseline_oid']!=source['baseline_oid']:_fail()
        evidence=json.loads(task['runtime_result_json'],object_pairs_hook=_pairs)
        execution=evidence.get('execution',{})
        if (execution.get('evidence')!='exited' or execution.get('exit_code')!=0
                or execution.get('input_revision')!=binding['code_revision']
                or execution.get('checkpoint_confirmed') is not True
                or execution.get('workspace_ref')!=task['workspace_ref']):_fail()
        checkpoint=execution.get('checkpoint_ref');reference(checkpoint)
        # Locate and verify the exact original stopped archive event. A runtime
        # row by itself is insufficient, even when it contains a valid receipt.
        key='archive:'+task['lease_ref']
        original=_confirmed(self.store,owner,key,'work.checkpoint')
        if (original is None or original['arguments']['task_id']!=task['task_id']
                or original['arguments']['lease_ref']!=task['lease_ref']
                or original['arguments']['lease_epoch']!=task['lease_epoch']
                or original['arguments']['evidence'].get('execution')!=execution
                or original['arguments']['evidence'].get('checkpoint_archive')!=evidence.get('checkpoint_archive')):_fail()
        self.store.verify_event_chain();_private(self.root)
        with tempfile.TemporaryDirectory(prefix='.checkpoint-',dir=self.root) as temporary:
            destination=Path(temporary)/'checkpoint'
            self.archive.restore(destination,scope=dict(owner_ref=owner,task_ref=task['task_id'],
                revision=binding['code_revision'],checkpoint_ref=checkpoint),
                receipt=ArchiveReceipt.from_document(evidence['checkpoint_archive']))
            path=destination/'candidate.zip'
            raw=_read(path,32*1024*1024)
            expected=[item for item in execution.get('files',[]) if item.get('name')=='candidate.zip']
            if len(expected)!=1 or expected[0]['sha256']!=digest(raw) or expected[0]['size_bytes']!=len(raw):_fail()
        packages=self._packages(raw,tuple(item['repo_id'] for item in binding['repositories']))
        _deadline(deadline)
        return packages

    @staticmethod
    def _packages(raw,repositories):
        """Closed outer ZIP lists the exact full frozen repository set."""
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries=archive.infolist()
                expected={'repositories/'+repo+'.zip' for repo in repositories}
                if (len(entries)!=len(expected) or len(entries)>32
                        or {item.filename for item in entries}!=expected
                        or any(item.flag_bits&1 or item.is_dir()
                            or stat.S_IFMT(item.external_attr>>16) not in {0,stat.S_IFREG}
                            or not 0<item.file_size<=32*1024*1024 for item in entries)
                        or sum(item.file_size for item in entries)>32*1024*1024):_fail()
                return {repo:archive.read('repositories/'+repo+'.zip') for repo in repositories}
        except Exception:_fail()


class SoftwareDeliveryPipeline:
    """Trusted explicit composition; no listener, import path or model options."""
    def __init__(self,manager:RepoManager,archive:ObjectArchive,*,changes:ArchivedCodeChanges,
                 build_executor=None,build_observer=None,target_observer=None):
        if (not isinstance(manager,RepoManager) or not isinstance(archive,ObjectArchive)
                or not isinstance(changes,ArchivedCodeChanges) or changes.store is not manager.store
                or changes.archive is not archive or archive.replica is not manager.store.replica
                or any(port is not None and not callable(port) for port in
                       (build_executor,build_observer,target_observer))):_fail()
        self.manager,self.store,self.archive=manager,manager.store,archive
        self.changes=changes
        self.build_executor,self.build_observer,self.target_observer=build_executor,build_observer,target_observer
        self.root=self.store.path.parent/'software-artifacts'

    def _tasks(self,owner,task_ref,revision,code_task_ref,code_revision):
        if any(type(value) is not int or not 1<=value<=2**53-1 for value in (revision,code_revision)):_fail()
        with closing(self.store._connect()) as connection:
            task=dict(self.store._owned(connection,'tasks','task_id',owner,task_ref))
            code=dict(self.store._owned(connection,'tasks','task_id',owner,code_task_ref))
            repos=lambda task:tuple(dict(row) for row in connection.execute(
                'SELECT repo_id,access,paths_json FROM task_repositories WHERE owner_ref=? AND task_id=? ORDER BY repo_id',
                (owner,task)))
            bindings=repos(task_ref)
            if (self.store.recovery_mode or task['revision']!=revision or code['revision']!=code_revision
                    or task['mode'] not in {'develop','deliver','deploy'} or code['mode']!='work'
                    or task['state'] not in {'queued','running'} or task['control_pending'] or code['control_pending']
                    or code['state'] not in {'queued','running','completed'}
                    or task['project_id']!=code['project_id'] or not task['project_id']
                    or not bindings or len(bindings)>32 or bindings!=repos(code_task_ref)
                    or any(row['access']!='write' or row['paths_json']!='[]' for row in bindings)
                    or json.loads(code['work_json'])['profile']!='code'):_fail()
            project=self.store._project(connection,owner,task['project_id'])
            if project['state']!='active':_fail()
        return task,code,tuple(item['repo_id'] for item in bindings)

    def bind(self,owner,key,*,task_ref,revision,code_task_ref,code_revision,
             release_ref,version,target_ref,permissions_digest,publish_targets,deadline):
        """Explicit trusted binding, authorized independently of the code Work.

        All repositories and publish aliases are frozen. develop may build only;
        deliver/deploy may publish through the real Publisher after the build.
        """
        _deadline(deadline)
        for value in (owner,task_ref,code_task_ref,release_ref,target_ref):reference(value)
        _sha(permissions_digest)
        arguments=dict(task_ref=task_ref,revision=revision,code_task_ref=code_task_ref,code_revision=code_revision,
            release_ref=release_ref,version=version,target_ref=target_ref,permissions_digest=permissions_digest,
            publish_targets=publish_targets)
        old=self.manager._existing(owner,key,'software.bind',arguments)
        if old:return old
        task,code,repos=self._tasks(owner,task_ref,revision,code_task_ref,code_revision)
        if (code['state']!='queued' or type(publish_targets) is not dict
                or (task['mode']=='develop' and publish_targets)
                or (task['mode'] in {'deliver','deploy'} and set(publish_targets)!=set(repos))):_fail()
        CanonicalReleaseAuthority._validate_release(dict(schema_version=2,release_ref=release_ref,version=version,
            source_digest=digest(b''),artifact_identity={'kind':'docker_image_id','digest':digest(b'')},
            permissions_digest=permissions_digest,qualification_digest=digest(b'')),release_ref)
        suffix=hashlib.sha256(canonical(dict(owner=owner,key=key)).encode()).hexdigest()
        sources=[]
        for repo in repos:
            _deadline(deadline)
            context,policy=self.manager._context(owner,task_ref,revision,repo,write=True)
            if repo in publish_targets and (type(publish_targets[repo]) is not str
                    or publish_targets[repo] not in dict(policy.publish_targets)):_fail()
            self.manager._authorize('bind_code_task',dict(context,**arguments))
            with closing(self.store._connect()) as connection:
                registered=self.store._owned(connection,'repositories','repo_id',owner,repo)
            _accepted(self.manager.sync(owner,'software-sync:'+suffix+':'+repo,
                repo_ref=repo,expected_revision=registered['revision']))
            _deadline(deadline)
            with closing(self.store._connect()) as connection:
                frozen=connection.execute("SELECT 1 FROM repository_artifacts WHERE owner_ref=? AND task_id=? "
                    "AND task_revision=? AND repo_id=? AND kind='source'",(owner,task_ref,revision,repo)).fetchone()
            if frozen is None:
                _accepted(self.manager.export_bundle(owner,'software-source:'+suffix+':'+repo,
                    task_ref=task_ref,expected_revision=revision,repo_ref=repo))
            _deadline(deadline)
            with closing(self.store._connect()) as connection:
                source,metadata=self.manager._source(connection,context)
                confirmed=connection.execute("SELECT 1 FROM events e JOIN durability d USING(event_id) "
                    "WHERE e.owner_ref=? AND e.task_id=? AND e.kind='repository.export' AND d.state='confirmed' "
                    "AND json_extract(e.body_json,'$.result.artifact_ref')=?",
                    (owner,task_ref,source['artifact_ref'])).fetchone()
            if confirmed is None:_fail()
            if metadata['policy_digest']!=context['policy_digest']:_fail()
            sources.append(dict(repo_id=repo,baseline_oid=metadata['baseline_oid'],
                source_bundle_ref=source['artifact_ref'],source_bundle_digest=source['sha256'],policy_digest=context['policy_digest']))
        binding=dict(owner_ref=owner,task_ref=task_ref,revision=revision,project_ref=task['project_id'],
            code_task_ref=code_task_ref,code_revision=code_revision,code_spec_digest=digest(code['work_json'].encode()),
            release_ref=release_ref,version=version,target_ref=target_ref,permissions_digest=permissions_digest,
            publish_targets=publish_targets,repositories=sources)
        binding_ref='SB-'+digest(canonical(binding).encode()).removeprefix('sha256:')
        def apply(connection,timestamp):
            self._tasks(owner,task_ref,revision,code_task_ref,code_revision)
            exists=connection.execute("SELECT 1 FROM events WHERE kind='software.bind' AND json_extract(body_json,'$.result.binding.release_ref')=?",
                (release_ref,)).fetchone()
            if exists:_fail()
            return dict(task_id=task_ref,binding_ref=binding_ref,binding=binding)
        _deadline(deadline)
        return self.store._mutation(owner,key,'software.bind',arguments,apply)

    def _binding(self,owner,key):
        self.store.verify_event_chain()
        record=_confirmed(self.store,owner,key,'software.bind')
        if record is None:_fail()
        binding=record['result']['binding']
        self._tasks(owner,binding['task_ref'],binding['revision'],binding['code_task_ref'],binding['code_revision'])
        return binding,record['result']['binding_ref']

    def _archive(self,binding,binding_ref,candidates):
        owner=binding['owner_ref'];key='software-archive:'+binding_ref
        prior=_confirmed(self.store,owner,key,'software.archive')
        if prior:return prior['result']
        files={}
        for item in candidates:
            repo=item['repo_id']
            with closing(self.store._connect()) as connection:
                context,_=self.manager._context(owner,binding['task_ref'],binding['revision'],repo,write=True)
                source,_=self.manager._source(connection,context)
            for name,sha in (('source',source['sha256']),('candidate',item['bundle_sha256'])):
                raw=_read(self.manager._artifact_path(sha),self.manager.git.max_bundle)
                if digest(raw)!=sha:_fail()
                files[repo+'-'+name+'.bundle']=raw
        manifest=dict(binding_ref=binding_ref,binding=binding,candidates=candidates)
        files['software.json']=canonical(manifest).encode()
        expected=[dict(name=name,size_bytes=len(raw),sha256=digest(raw)) for name,raw in sorted(files.items())]
        scope=dict(owner_ref=owner,task_ref=binding['task_ref'],revision=binding['revision'],checkpoint_ref=binding_ref)
        _private(self.root)
        with tempfile.TemporaryDirectory(prefix='.archive-',dir=self.root) as temporary:
            root=Path(temporary)
            for name,raw in files.items():
                path=root/name;path.write_bytes(raw);path.chmod(0o600)
            receipt=self.archive.save(root,scope=scope,expected_files=expected)
        result=dict(task_id=binding['task_ref'],scope=scope,archive=receipt.document(),
            source_digest=digest(canonical(manifest).encode()),files=expected)
        return _accepted(self.store._mutation(owner,key,'software.archive',dict(binding_ref=binding_ref),
            lambda connection,timestamp:result))

    def _read_archive(self,record):
        _private(self.root)
        with tempfile.TemporaryDirectory(prefix='.read-',dir=self.root) as temporary:
            root=Path(temporary)/'artifacts'
            self.archive.restore(root,scope=record['scope'],receipt=ArchiveReceipt.from_document(record['archive']))
            files={path.relative_to(root).as_posix():path.read_bytes() for path in root.rglob('*') if path.is_file()}
        expected=[dict(name=name,size_bytes=len(raw),sha256=digest(raw)) for name,raw in sorted(files.items())]
        if expected!=record['files']:_fail()
        return files

    def run(self,owner,*,binding_key,deadline):
        """Idempotent stage/build/publish; an uncertain build is observed only."""
        _deadline(deadline)
        if not all(callable(port) for port in (self.build_executor,self.build_observer,self.target_observer)):_fail()
        binding,binding_ref=self._binding(owner,binding_key)
        final=_confirmed(self.store,owner,'software-release:'+binding_ref,'software.release')
        if final:return final['result']
        packages=self.changes(binding,deadline=deadline)
        if set(packages)!={item['repo_id'] for item in binding['repositories']}:_fail()
        candidates=[]
        for source in binding['repositories']:
            _deadline(deadline);repo=source['repo_id'];prefix=binding_ref+':'+repo
            assembled=_accepted(self.manager.assemble_candidate(owner,'assemble:'+prefix,
                task_ref=binding['task_ref'],expected_revision=binding['revision'],repo_ref=repo,
                content=packages[repo],expected_digest=digest(packages[repo])))
            imported=_accepted(self.manager.import_candidate(owner,'import:'+prefix,
                task_ref=binding['task_ref'],expected_revision=binding['revision'],repo_ref=repo,
                bundle_ref=assembled['bundle_ref'],expected_digest=assembled['bundle_sha256'],
                candidate_oid=assembled['candidate_oid']))
            candidates.append({**source,**{name:assembled[name] for name in ('candidate_oid','bundle_ref','bundle_sha256')},
                **{name:imported[name] for name in ('candidate_ref','validation_digest')}})
        archived=self._archive(binding,binding_ref,candidates)
        files=self._read_archive(archived)
        request=dict(binding_ref=binding_ref,owner_ref=owner,task_ref=binding['task_ref'],revision=binding['revision'],
            project_ref=binding['project_ref'],target_ref=binding['target_ref'],release_ref=binding['release_ref'],
            version=binding['version'],source_digest=archived['source_digest'],permissions_digest=binding['permissions_digest'],
            candidates=candidates,archive=archived['archive'])
        request_digest=digest(canonical(request).encode())
        self._target(binding,request,deadline=deadline)
        build_key='software-build:'+binding_ref
        built=_confirmed(self.store,owner,build_key,'software.build')
        if built is None:
            fresh=False
            def claim(connection,timestamp):
                nonlocal fresh
                self._binding(owner,binding_key);fresh=True
                return dict(task_id=binding['task_ref'],request=request,request_digest=request_digest)
            _accepted(self.store._mutation(owner,'software-build-claim:'+binding_ref,'software.build.claim',
                dict(binding_ref=binding_ref,request_digest=request_digest),claim))
            _deadline(deadline)
            execution=self.build_executor(request,files,deadline=deadline) if fresh else None
            observed=self.build_observer(request,deadline=deadline)
            if type(observed) is not BuildObservation:_fail()
            observation=observed.document()
            if (observation['request_digest']!=request_digest
                    or observation['permissions_digest']!=binding['permissions_digest']
                    or fresh and (type(execution) is not BuildObservation or execution.document()!=observation)):_fail()
            _deadline(deadline)
            _accepted(self.store._mutation(owner,build_key,'software.build',dict(request_digest=request_digest),
                lambda connection,timestamp:dict(task_id=binding['task_ref'],request=request,observation=observation)))
            built=_confirmed(self.store,owner,build_key,'software.build')
        if built['result']['request']!=request:_fail()
        publications=[]
        for candidate in candidates:
            repo=candidate['repo_id']
            if repo not in binding['publish_targets']:continue
            _deadline(deadline)
            prepared=_accepted(self.manager.prepare_publish(owner,'prepare:'+binding_ref+':'+repo,
                task_ref=binding['task_ref'],expected_revision=binding['revision'],repo_ref=repo,
                candidate_ref=candidate['candidate_ref'],target_alias=binding['publish_targets'][repo]))
            status=self.manager.publish(owner,'publish:'+binding_ref+':'+repo,publication_ref=prepared['publication_ref'])
            if status['state']=='unknown':
                status=self.manager.reconcile_publish(owner,identifier('SPO'),
                    publication_ref=prepared['publication_ref'])
            if status['state']!='confirmed' or status['durability']!='replicated':_fail()
            publications.append(dict(repo_id=repo,publication_ref=prepared['publication_ref']))
        observation=built['result']['observation']
        release=dict(schema_version=2,release_ref=binding['release_ref'],version=binding['version'],
            source_digest=archived['source_digest'],artifact_identity=observation['artifact_identity'],
            permissions_digest=observation['permissions_digest'],qualification_digest=observation['qualification_digest'])
        self._qualify(binding,request,observation,publications,deadline=deadline)
        result=dict(task_id=binding['task_ref'],binding=binding,request=request,observation=observation,
            publications=publications,release=release)
        return _accepted(self.store._mutation(owner,'software-release:'+binding_ref,'software.release',
            dict(binding_ref=binding_ref,release_digest=digest(canonical(release).encode())),
            lambda connection,timestamp:result))

    def _qualify(self,binding,request,observation,publications,*,deadline):
        _deadline(deadline)
        self._tasks(binding['owner_ref'],binding['task_ref'],binding['revision'],binding['code_task_ref'],binding['code_revision'])
        archived=_confirmed(self.store,binding['owner_ref'],'software-archive:'+request['binding_ref'],'software.archive')
        if archived is None or archived['result']['archive']!=request['archive']:_fail()
        files=self._read_archive(archived['result'])
        manifest=json.loads(files['software.json'],object_pairs_hook=_pairs)
        if (digest(files['software.json'])!=request['source_digest']
                or manifest!=dict(binding_ref=request['binding_ref'],binding=binding,candidates=request['candidates'])):_fail()
        for candidate in request['candidates']:
            repo=candidate['repo_id']
            if (digest(files[repo+'-candidate.bundle'])!=candidate['bundle_sha256']
                    or digest(files[repo+'-source.bundle'])!=candidate['source_bundle_digest']):_fail()
        _deadline(deadline)
        if not callable(self.build_observer) or not callable(self.target_observer):_fail()
        observed=self.build_observer(request,deadline=deadline)
        if type(observed) is not BuildObservation or observed.document()!=observation:_fail()
        # Current target observer returns a scoped observation and evidence hash,
        # not a ready/approved flag or a copy of build output from configuration.
        self._target(binding,request,deadline=deadline)
        expected={item['repo_id']:item for item in request['candidates']}
        if set(expected)!={item['repo_id'] for item in binding['repositories']}:_fail()
        if set(binding['publish_targets'])!={item['repo_id'] for item in publications}:_fail()
        for item in publications:
            actual=self.manager.observe_publication(binding['owner_ref'],item['publication_ref'],deadline=deadline)
            candidate=expected[item['repo_id']]
            if any(actual[key]!=candidate[key] for key in ('repo_id','candidate_ref','candidate_oid','validation_digest','policy_digest')):_fail()
        # develop still needs current independent candidate validation even
        # though it deliberately has no publication authority.
        for repo,candidate in expected.items():
            context,_=self.manager._context(binding['owner_ref'],binding['task_ref'],binding['revision'],repo,write=True)
            with closing(self.store._connect()) as connection:
                stored=self.manager._candidate(connection,binding['owner_ref'],candidate['candidate_ref'],context)
            if self.manager._validate_candidate(context,stored)!=candidate['validation_digest']:_fail()
        _deadline(deadline)

    def _target(self,binding,request,*,deadline):
        _deadline(deadline)
        if not callable(self.target_observer):_fail()
        target=self.target_observer(binding['target_ref'],request,deadline=deadline)
        if (type(target) is not dict or set(target)!={'target_ref','project_ref','permissions_digest','evidence_digest'}
                or target['target_ref']!=binding['target_ref'] or target['project_ref']!=binding['project_ref']
                or target['permissions_digest']!=binding['permissions_digest']):_fail()
        _sha(target['evidence_digest'])
        _deadline(deadline)

    def _release_record(self,release_ref,*,deadline):
        _deadline(deadline);reference(release_ref)
        if self.store.recovery_mode:_fail()
        self.store.verify_event_chain()
        with closing(self.store._connect()) as connection:
            if connection.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone():_fail()
            rows=connection.execute("SELECT c.owner_ref,c.idempotency_key FROM commands c JOIN events e USING(event_id) "
                "WHERE e.kind='software.release' AND json_extract(c.result_json,'$.release.release_ref')=?",(release_ref,)).fetchall()
        if len(rows)!=1:_fail()
        record=_confirmed(self.store,rows[0]['owner_ref'],rows[0]['idempotency_key'],'software.release')
        if record is None:_fail()
        result=record['result']
        build=_confirmed(self.store,result['binding']['owner_ref'],
            'software-build:'+result['request']['binding_ref'],'software.build')
        if (build is None or build['result']['request']!=result['request']
                or build['result']['observation']!=result['observation']):_fail()
        observation=BuildObservation(**build['result']['observation']).document()
        expected=dict(schema_version=2,release_ref=result['binding']['release_ref'],version=result['binding']['version'],
            source_digest=build['result']['request']['source_digest'],artifact_identity=observation['artifact_identity'],
            permissions_digest=observation['permissions_digest'],qualification_digest=observation['qualification_digest'])
        if result['release']!=expected:_fail()
        CanonicalReleaseAuthority._validate_release(result['release'],release_ref)
        _deadline(deadline)
        return result

    def release_reader(self,release_ref,*,deadline):
        """Independent frozen, confirmed actual build event; no build/network."""
        return self._release_record(release_ref,deadline=deadline)['release']

    def source_qualifier(self,target_ref,release_ref,*,deadline):
        """Fresh scoped repo/validation/build/target observations, exact Release7."""
        record=self._release_record(release_ref,deadline=deadline)
        if record['binding']['target_ref']!=target_ref:_fail()
        self._qualify(record['binding'],record['request'],record['observation'],record['publications'],deadline=deadline)
        return record['release']
