"""Freeze original programs and resolved input bytes before granting execution.

This is local byte staging plus the existing encrypted archive/event chain,
never a second task store or a second replica journal.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from .durability import digest
from .object_archive import ArchiveError, ArchiveReceipt, MAX_BYTES, MAX_FILES, ObjectArchive, _name, _pairs
from .store import DeliveryError, canonical
from .repository_sources import RepositorySources, task_repositories


def _private(path: Path) -> None:
    if not path.parent.exists():
        _private(path.parent)
    if path.is_symlink() or path.parent.is_symlink():
        raise ArchiveError('source_staging_invalid')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise ArchiveError('source_staging_invalid')


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _put(path: Path, content: bytes) -> None:
    _private(path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size != len(content) or source.read(len(content) + 1) != content):
                raise ArchiveError('source_staging_conflict')
        return
    with os.fdopen(fd, 'wb') as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    _sync(path.parent)


class WorkSourceArchive:
    def __init__(self, store, archive: ObjectArchive, *, repository_sources=None):
        if (archive.replica is not store.replica or archive.local_domain != store.local_domain
                or archive.replica_domain != store.replica_domain):
            raise DeliveryError('archive_replica_invalid')
        if repository_sources is not None and (not isinstance(repository_sources,RepositorySources)
                or repository_sources.store is not store or repository_sources.archive is not archive):
            raise DeliveryError('repository_sources_invalid')
        self.store, self.archive = store, archive
        self.repository_sources=repository_sources
        self.root = store.path.parent.resolve() / (store.path.name + '.sources')

    def save(self, task: dict, spec: dict, inputs: dict[str, bytes]) -> bool:
        """True only after bytes and their canonical metadata event are durable."""
        try:
            if canonical(spec) != task['work_json'] or not isinstance(inputs, dict):
                raise ArchiveError('source_spec_invalid')
            repositories=task_repositories(self.store,task)
            if repositories and spec['profile']=='browser':
                raise ArchiveError('source_profile_forbidden')
            if repositories and self.repository_sources is None:
                raise ArchiveError('repository_sources_unavailable')
            guest_inputs=dict(inputs)
            for name,value in guest_inputs.items():
                _name(name)
                if not isinstance(value,bytes):raise ArchiveError('source_input_invalid')
                if name=='repositories' or name.startswith('repositories/'):
                    raise ArchiveError('source_input_reserved')
            if len(guest_inputs)+2>MAX_FILES or sum(map(len,guest_inputs.values()))>MAX_BYTES:
                raise ArchiveError('source_size_exceeded')
            # RepositoryArchive acquires repository locks before the writer
            # guard. Never call it while holding this source's writer guard.
            repository_evidence=(() if self.repository_sources is None else self.repository_sources.freeze(task))
            repository_views=()
            if repository_evidence:
                repository_inputs,repository_views=self.repository_sources.inputs(task,repository_evidence)
                guest_inputs.update(repository_inputs)
            files = {'program': spec['program'].encode('utf-8')}
            for name, value in guest_inputs.items():
                _name(name)
                if not isinstance(value, bytes):
                    raise ArchiveError('source_input_invalid')
                files[_name('inputs/' + name)] = value
            document = {key: task[key] for key in ('owner_ref', 'task_id', 'workspace_ref', 'revision',
                'input_revision', 'current_source_event_ref', 'project_id', 'requirement')}
            document.update(version=1, spec=spec, inputs={name: digest(value) for name, value in sorted(guest_inputs.items())})
            if repository_evidence:document['repositories']=list(repository_evidence)
            if repository_views:document['repository_views']=list(repository_views)
            files['task.json'] = canonical(document).encode('utf-8')
            expected = [{'name': name, 'size_bytes': len(value), 'sha256': digest(value)}
                        for name, value in sorted(files.items())]
            if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BYTES:
                raise ArchiveError('source_size_exceeded')
            # The revision key is independent of content. A changed resolver
            # response must conflict with retained source bytes, never replace them.
            identity = {key: task[key] for key in ('owner_ref', 'task_id', 'revision')}
            key = hashlib.sha256(canonical(identity).encode()).hexdigest()
            _private(self.root)
            path = self.root / key
            if not path.exists() and len(os.listdir(self.root)) >= 1024:
                raise ArchiveError('source_staging_limit')
            _private(path)
            for name, value in sorted(files.items()):
                _put(path.joinpath(*name.split('/')), value)
            _sync(path)
            _sync(self.root)
            source_digest = digest(canonical(expected).encode())
            source_ref = 'SRC-' + source_digest.removeprefix('sha256:')
            with self.store._writer_guard():
                with closing(self.store._connect()) as connection:
                    if connection.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone():
                        return False
                receipt = self.archive.save(path, scope={'owner_ref': task['owner_ref'],
                    'task_ref': task['task_id'], 'revision': task['revision'], 'checkpoint_ref': source_ref},
                    expected_files=expected)
                result = self.store.record_work_source(task['owner_ref'],
                    'source:' + task['task_id'] + ':' + str(task['revision']), task_ref=task['task_id'],
                    expected_revision=task['revision'], spec_digest=digest(task['work_json'].encode()),
                    source_digest=source_digest, receipt=receipt.document())
            return result['status'] == 'accepted'
        except (ArchiveError, OSError, DeliveryError):
            return False

    def inputs(self,task:dict,spec:dict):
        """Read confirmed archived bytes, never the resolver or a live checkout.

        None means no original source command. Unknown/pending/corrupt original
        evidence raises and cannot be replaced by re-resolving or recapturing.
        Call only before claim; the canonical queued revision is rechecked.
        """
        return self._inputs(task,spec)

    def stopped_inputs(self,task:dict,spec:dict):
        """Read original sealed code inputs for a separately authorized handoff.

        This grants no repository write authority. The handoff coordinator must
        independently verify the confirmed stopped checkpoint and task binding.
        """
        return self._inputs(task,spec,stopped=True)

    def _inputs(self,task,spec,*,stopped=False):
        try:
            repositories=task_repositories(self.store,task,stopped=stopped)
            if repositories and spec['profile']=='browser':raise ArchiveError('source_profile_forbidden')
            if canonical(spec)!=task['work_json']:raise ArchiveError('source_spec_invalid')
            key='source:'+task['task_id']+':'+str(task['revision'])
            self.store.verify_event_chain()
            with closing(self.store._connect()) as connection:
                row=connection.execute('SELECT c.*,e.kind,e.body_json,d.state FROM commands c '
                    'JOIN events e USING(event_id) JOIN durability d USING(event_id) '
                    'WHERE c.owner_ref=? AND c.idempotency_key=?',(task['owner_ref'],key)).fetchone()
            if row is None:return None
            if row['state']!='confirmed':raise ArchiveError('source_pending')
            event=json.loads(row['body_json'],object_pairs_hook=_pairs)
            result=json.loads(row['result_json'],object_pairs_hook=_pairs)
            arguments=event['arguments']
            if (row['kind']!='work.source_archive' or event['owner_ref']!=task['owner_ref']
                    or event['result']!=result or result['task_id']!=task['task_id']
                    or result['revision']!=task['revision']
                    or arguments['task_id']!=task['task_id']
                    or arguments['expected_revision']!=task['revision']
                    or arguments['spec_digest']!=digest(task['work_json'].encode())
                    or result['source_ref']!='SRC-'+arguments['source_digest'].removeprefix('sha256:')
                    or result['source_archive']!=arguments['receipt']
                    or row['argument_digest']!=digest(canonical(dict(kind=event['kind'],arguments=arguments)).encode())):
                raise ArchiveError('source_evidence_invalid')
            _private(self.root)
            with tempfile.TemporaryDirectory(prefix='.read-',dir=self.root) as temporary:
                destination=Path(temporary)/'source'
                self.archive.restore(destination,scope=dict(owner_ref=task['owner_ref'],task_ref=task['task_id'],
                    revision=task['revision'],checkpoint_ref=result['source_ref']),
                    receipt=ArchiveReceipt.from_document(result['source_archive']))
                actual={path.relative_to(destination).as_posix():path.read_bytes()
                    for path in destination.rglob('*') if path.is_file()}
                expected=[dict(name=name,size_bytes=len(value),sha256=digest(value))
                    for name,value in sorted(actual.items())]
                if (digest(canonical(expected).encode())!=arguments['source_digest']
                        or actual.get('program')!=spec['program'].encode() or 'task.json' not in actual):
                    raise ArchiveError('source_evidence_invalid')
                document=json.loads(actual['task.json'],object_pairs_hook=_pairs)
                fields=('owner_ref','task_id','workspace_ref','revision','input_revision',
                        'current_source_event_ref','project_id','requirement')
                if (canonical(document).encode()!=actual['task.json'] or document.get('version')!=1
                        or any(document.get(field)!=task[field] for field in fields)
                        or document.get('spec')!=spec or type(document.get('inputs')) is not dict
                        or set(actual)!={'program','task.json',*('inputs/'+name for name in document['inputs'])}):
                    raise ArchiveError('source_evidence_invalid')
                inputs={}
                for name,sha in document['inputs'].items():
                    _name(name)
                    value=actual['inputs/'+name]
                    if digest(value)!=sha:raise ArchiveError('source_evidence_invalid')
                    inputs[name]=value
                repositories=task_repositories(self.store,task,stopped=stopped)
                if tuple(sorted(item['repo_id'] for item in document.get('repositories',())))!=repositories:
                    raise ArchiveError('source_evidence_invalid')
                if repositories:
                    if self.repository_sources is None:raise ArchiveError('repository_sources_unavailable')
                    # Current owner/task/repository authorization is mandatory,
                    # even when source bytes were sealed in a previous process.
                    for evidence in document['repositories']:
                        context,_=self.repository_sources.manager._context(task['owner_ref'],task['task_id'],
                            task['revision'],evidence['repo_id'],terminal=stopped and task['state']=='completed')
                        self.repository_sources.manager._authorize('export_execution_input',context)
                    views=document.get('repository_views')
                    if (type(views) is not list or tuple(sorted(item['repo_id'] for item in views))!=repositories):
                        raise ArchiveError('source_evidence_invalid')
                    for view in views:
                        prefix='repositories/'+view['repo_id']
                        if (digest(inputs[prefix+'.json'])!=view['view_digest']
                                or digest(inputs[prefix+'.tar'])!=view['package_digest']):
                            raise ArchiveError('source_evidence_invalid')
                return inputs
        except ArchiveError:
            raise
        except Exception:
            raise ArchiveError('source_unavailable') from None
