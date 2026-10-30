"""Managed clean baseline snapshots, genuinely sealed by the Linux kernel.

No active checkout, host mount, candidate execution or second task/worker. Model
changes stay on the original stopped-checkpoint path, audited in full below.
"""
from contextlib import closing
import fcntl
from functools import wraps
import io
import json
import mmap
import os
from pathlib import Path
import stat
import tarfile
import tempfile
from types import MappingProxyType

from .durability import digest
from .object_archive import ArchiveError, ArchiveReceipt, MAX_BYTES, _name, _pairs
from .repo_manager import _immutable, _oid, _private, _read, _sha
from .repository_execution import ArchivedCodeChanges, _confirmed, _deadline
from .repository_inputs import RepositoryInputView, SourceExposureReceipt, _permitted_name
from .repository_sources import RepositorySources, task_repositories
from .repository_validation import MAX_FILES, MAX_SOURCE, SCANNER_DIGEST, SourceScanner
from .store import DeliveryError, canonical

KIND = 'managed-sealed-v2'
MAX_SNAPSHOT = 128*1024*1024


def fail(code='repository_sealed_source_invalid'):
    raise DeliveryError(code) from None


def _safe(method):
    @wraps(method)
    def call(*args,**kwargs):
        try:return method(*args,**kwargs)
        except (DeliveryError,ArchiveError):raise
        except Exception:fail()
    return call


class SealedBytes:
    """An owned fd, exact immutable identity and bytes; no path-based reopening."""
    def __init__(self, raw):
        self.fd = None
        try:
            names = ('F_ADD_SEALS','F_GET_SEALS','F_SEAL_WRITE','F_SEAL_GROW','F_SEAL_SHRINK','F_SEAL_SEAL')
            if not hasattr(os,'memfd_create') or any(not hasattr(fcntl,name) for name in names):
                fail('repository_sealing_unsupported')
            if type(raw) is not bytes or not 1 <= len(raw) <= MAX_SNAPSHOT: fail()
            self.required = fcntl.F_SEAL_WRITE|fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_SEAL
            self.fd = os.memfd_create('repository-snapshot',os.MFD_ALLOW_SEALING|os.MFD_CLOEXEC)
            offset = 0
            while offset < len(raw):
                written = os.write(self.fd,raw[offset:offset+65536])
                if written <= 0: fail()
                offset += written
            fcntl.fcntl(self.fd,fcntl.F_ADD_SEALS,self.required)
            info = os.fstat(self.fd)
            self.identity = (info.st_dev,info.st_ino,info.st_size,stat.S_IFMT(info.st_mode))
            self.sha256 = digest(raw)
            if self.read() != raw: fail()
        except Exception:
            self.close()
            if not hasattr(os,'memfd_create'): fail('repository_sealing_unsupported')
            fail('repository_sealing_unavailable')

    def read(self):
        try:
            info = os.fstat(self.fd)
            if ((info.st_dev,info.st_ino,info.st_size,stat.S_IFMT(info.st_mode)) != self.identity
                    or not stat.S_ISREG(info.st_mode)
                    or fcntl.fcntl(self.fd,fcntl.F_GET_SEALS) != self.required): fail()
            parts=[]; offset=0
            while offset < info.st_size:
                raw=os.pread(self.fd,min(65536,info.st_size-offset),offset)
                if not raw: fail()
                parts.append(raw);offset+=len(raw)
            value=b''.join(parts)
            if len(value)!=info.st_size or digest(value)!=self.sha256: fail()
            after=os.fstat(self.fd)
            if (after.st_dev,after.st_ino,after.st_size,stat.S_IFMT(after.st_mode))!=self.identity: fail()
            return value
        except Exception: fail('repository_sealing_unavailable')

    def close(self):
        if self.fd is not None:
            os.close(self.fd);self.fd=None

    def __enter__(self): return self
    def __exit__(self,*args): self.close()


def qualify_sealing():
    """Real Linux syscall refusal checks. Unsupported systems never fake them."""
    with SealedBytes(b'qualification') as sealed:
        for attempt in (lambda:os.pwrite(sealed.fd,b'x',0), lambda:os.ftruncate(sealed.fd,0),
                lambda:os.ftruncate(sealed.fd,100),
                lambda:fcntl.fcntl(sealed.fd,fcntl.F_ADD_SEALS,sealed.required),
                lambda:mmap.mmap(sealed.fd,0,flags=mmap.MAP_SHARED,prot=mmap.PROT_READ|mmap.PROT_WRITE)):
            try:
                value=attempt()
            except (OSError,PermissionError): continue
            if isinstance(value,mmap.mmap):value.close()
            fail('repository_sealing_unavailable')
        if sealed.read()!=b'qualification':fail()


def _pack(files):
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode='w',format=tarfile.USTAR_FORMAT) as packed:
        for name,raw in sorted(files.items()):
            _name(name)
            if type(raw) is not bytes or output.tell()+len(raw)+1024>MAX_SNAPSHOT:fail()
            item=tarfile.TarInfo(name);item.mode=0o400;item.size=len(raw)
            packed.addfile(item,io.BytesIO(raw))
    if output.tell()>MAX_SNAPSHOT:fail()
    return output.getvalue()


def _unpack(raw, *, names=None):
    if type(raw) is not bytes or not 1<=len(raw)<=MAX_SNAPSHOT:fail()
    files={};total=0
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as packed:
        for item in packed:
            name=_name(item.name)
            if (not item.isfile() or item.pax_headers or name in files or len(files)>=MAX_FILES
                    or item.size<0 or item.mode not in {0o400,0o500}):fail()
            total+=item.size
            if total>MAX_SNAPSHOT:fail()
            value=packed.extractfile(item).read(item.size+1)
            if len(value)!=item.size:fail()
            files[name]=value
    if names is not None and set(files)!=names:fail()
    return files


class ManagedSealedRepositorySources(RepositorySources):
    def __init__(self,manager,archive,*,input_gate):
        qualify_sealing()
        super().__init__(manager,archive,workspace_bindings=None,quiescence_gate=None,
                         input_gate=input_gate,source_kind=KIND)
        if not callable(input_gate):fail()
        self.root=self.store.path.parent/'sealed-repository-sources';_private(self.root)

    @staticmethod
    def _identity(task,repo):
        return dict(owner_ref=task['owner_ref'],task_id=task['task_id'],revision=task['revision'],repo_ref=repo)

    def _key(self,task,repo):
        return 'sealed-repository-source:'+digest(canonical(self._identity(task,repo)).encode())[7:]

    def _arguments(self,task,repo):
        return dict(task_ref=task['task_id'],expected_revision=task['revision'],repo_ref=repo,
                    workspace_ref=task['workspace_ref'],source_kind=KIND)

    def _export(self,task,repo):
        context,policy=self.manager._context(task['owner_ref'],task['task_id'],task['revision'],repo)
        self.manager._authorize('archive',context)
        with closing(self.store._connect()) as connection:
            source,metadata=self.manager._source(connection,context)
            rows=connection.execute("SELECT c.idempotency_key FROM commands c JOIN events e USING(event_id) "
                "WHERE e.owner_ref=? AND e.task_id=? AND e.kind='repository.export' "
                "AND json_extract(e.body_json,'$.result.artifact_ref')=?",
                (task['owner_ref'],task['task_id'],source['artifact_ref'])).fetchall()
        if len(rows)!=1:fail()
        original=_confirmed(self.store,task['owner_ref'],rows[0][0],'repository.export')
        if (original is None or original['arguments']!={'task_ref':task['task_id'],'expected_revision':task['revision'],'repo_ref':repo}
                or original['result']['repo_id']!=repo or original['result']['task_id']!=task['task_id']
                or original['result']['sha256']!=source['sha256'] or original['result']['size']!=source['size']
                or original['result']['baseline_oid']!=metadata['baseline_oid'] or metadata['policy_digest']!=policy.sha256):fail()
        return context,source,metadata,original['event_id']

    def _source(self,task,repo):
        context,source,metadata,event=self._export(task,repo)
        raw=_read(self.manager._artifact_path(source['sha256']),self.manager.git.max_bundle)
        if len(raw)!=source['size'] or digest(raw)!=source['sha256']:fail()
        return context,source,metadata,event,raw

    def _snapshot(self,task,repo,repositories):
        context,source,metadata,event,bundle=self._source(task,repo)
        manager=self.manager;budget=manager.git.budget();scanner=SourceScanner()
        with SealedBytes(bundle) as sealed, tempfile.TemporaryDirectory(prefix='.bare-',dir=self.root) as temporary:
            temporary=Path(temporary);bundle_path=temporary/'source.bundle';_immutable(bundle_path,sealed.read())
            repository=temporary/'source.git';lock=temporary/'git.lock'
            fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            def run(args,root=repository):return manager.git.run(manager.home,root,args,budget,lockfd=fd)[1]
            try:
                baseline=_oid(metadata['baseline_oid']);algorithm='sha1' if len(baseline)==40 else 'sha256'
                run(['init','--bare','--template=','--object-format='+algorithm,str(repository)],None);repository.chmod(0o700)
                run(['bundle','verify',str(bundle_path)])
                if run(['bundle','list-heads',str(bundle_path)])!=(baseline+' '+metadata['retained_ref']+'\n').encode():fail()
                run(['bundle','unbundle',str(bundle_path)]);run(['update-ref','refs/codex/baseline',baseline])
                run(['fsck','--strict','--no-reflogs'])
                commits=run(['rev-list',baseline]).decode('ascii').splitlines()
                if not 1<=len(commits)<=MAX_FILES:fail()
                blobs={};files={};manifest=[];history=[];total=0
                for commit in commits:
                    for entry in run(['ls-tree','-r','-z','--full-tree',_oid(commit)]).split(b'\0'):
                        if not entry:continue
                        header,name=entry.split(b'\t',1);mode,kind,oid=header.decode('ascii').split(' ')
                        name=_permitted_name(name.decode('utf-8'));_oid(oid)
                        if mode not in {'100644','100755'} or kind!='blob':fail()
                        if oid not in blobs:
                            raw=run(['cat-file','blob',oid]);total+=len(raw)
                            if total>MAX_SOURCE or len(blobs)>=MAX_FILES:fail()
                            blobs[oid]=raw
                        scanner.scan({name:blobs[oid]});history.append(dict(commit=commit,name=name,oid=oid,sha256=digest(blobs[oid])))
                        if len(history)>MAX_FILES:fail()
                        if commit==baseline:
                            files[name]=blobs[oid];manifest.append(dict(name=name,size_bytes=len(blobs[oid]),
                                sha256=digest(blobs[oid]),executable=mode=='100755'))
                manifest.sort(key=lambda item:item['name']);manifest_sha=digest(canonical(manifest).encode())
                source_context=dict(context,source_kind=KIND,manifest_digest=manifest_sha,baseline_oid=baseline,
                    source_bundle_ref=source['artifact_ref'],source_bundle_digest=source['sha256'],export_event_id=event)
                receipt=self.input_gate(source_context,MappingProxyType(files))
                if (type(receipt) is not SourceExposureReceipt or receipt.passed is not True
                        or receipt.context_digest!=digest(canonical(source_context).encode())
                        or receipt.manifest_digest!=manifest_sha):fail()
                _sha(receipt.scan_digest)
                view=RepositoryInputView(dict(manifest=manifest),MappingProxyType(files)).package()
                document=dict(version=2,source_kind=KIND,context=context,repositories=list(repositories),
                    workspace_ref=task['workspace_ref'],baseline_oid=baseline,source_bundle_ref=source['artifact_ref'],
                    source_bundle_digest=source['sha256'],source_bundle_size=source['size'],export_event_id=event,
                    manifest=manifest,manifest_digest=manifest_sha,history_scan_digest=digest(canonical(history).encode()),
                    scanner_digest=SCANNER_DIGEST,view_digest=digest(view),source_context=source_context,
                    exposure=dict(context_digest=receipt.context_digest,manifest_digest=receipt.manifest_digest,
                                  scan_digest=receipt.scan_digest,passed=receipt.passed))
                self._source(task,repo)
                return _pack({'metadata.json':canonical(document).encode(),'source.bundle':sealed.read(),'view.tar':view})
            finally:os.close(fd)

    @_safe
    def freeze(self,task):
        repositories=task_repositories(self.store,task)
        if not repositories:return ()
        self.prepare(task,repositories);evidence=[]
        for repo in repositories:
            self._export(task,repo)
            key=self._key(task,repo);arguments=self._arguments(task,repo)
            old=self.manager._existing(task['owner_ref'],key,'repository.sealed_source',arguments)
            if old is None:
                stage=self.root/digest(key.encode())[7:];_private(stage)
                with self.manager._locked(repo):
                    # A retained pre-event stage is compared to a fresh read of
                    # the SAME confirmed immutable export, never trusted as a
                    # self-authenticating manifest or replaced with a new base.
                    raw=self._snapshot(task,repo,repositories)
                    with SealedBytes(raw) as sealed:_immutable(stage/'snapshot.tar',sealed.read())
                    raw=_read(stage/'snapshot.tar',MAX_SNAPSHOT)
                    with SealedBytes(raw) as sealed:self._decode(task,repo,repositories,sealed.read())
                    sha=digest(raw);backup='RS-'+sha[7:]
                    scope=dict(owner_ref=task['owner_ref'],task_ref=task['task_id'],revision=task['revision'],checkpoint_ref=backup)
                    receipt=self.archive.save(stage,scope=scope,expected_files=[dict(name='snapshot.tar',size_bytes=len(raw),sha256=sha)])
                    context,_=self.manager._context(task['owner_ref'],task['task_id'],task['revision'],repo)
                    self.manager._authorize('archive',context)
                    if task_repositories(self.store,task)!=repositories:fail()
                    result=dict(task_id=task['task_id'],repo_id=repo,workspace_ref=task['workspace_ref'],source_kind=KIND,
                        backup_ref=backup,context_digest=digest(canonical(context).encode()),state_sha256=sha,archive=receipt.document())
                    old=self.store._mutation(task['owner_ref'],key,'repository.sealed_source',arguments,
                        lambda connection,timestamp:result)
            if old.get('status')!='accepted' or old.get('durability')!='replicated':fail('repository_sources_pending')
            recorded=_confirmed(self.store,task['owner_ref'],key,'repository.sealed_source')
            if recorded is None or recorded['arguments']!=arguments:fail()
            value=recorded['result'];evidence.append({name:value[name] for name in
                ('repo_id','workspace_ref','source_kind','backup_ref','context_digest','state_sha256','archive')})
            evidence[-1]['event_id']=recorded['event_id']
        if task_repositories(self.store,task)!=repositories:fail()
        return tuple(evidence)

    def _decode(self,task,repo,repositories,raw):
        files=_unpack(raw,names={'metadata.json','source.bundle','view.tar'})
        document=json.loads(files['metadata.json'],object_pairs_hook=_pairs)
        expected={'version','source_kind','context','repositories','workspace_ref','baseline_oid','source_bundle_ref',
            'source_bundle_digest','source_bundle_size','export_event_id','manifest','manifest_digest','history_scan_digest',
            'scanner_digest','view_digest','source_context','exposure'}
        if type(document) is not dict or set(document)!=expected or canonical(document).encode()!=files['metadata.json']:fail()
        # The confirmed snapshot owns the original bundle bytes. A restart may
        # have only the database and independent archive; local export staging
        # is not part of the recovery contract. Recheck its canonical identity
        # here and verify the archived bytes against that SAME export below.
        context,source,metadata,event=self._export(task,repo)
        if (type(document['version']) is not int or document['version']!=2 or document['source_kind']!=KIND
                or document['context']!=context or document['repositories']!=list(repositories)
                or document['workspace_ref']!=task['workspace_ref'] or document['baseline_oid']!=metadata['baseline_oid']
                or document['source_bundle_ref']!=source['artifact_ref'] or document['source_bundle_digest']!=source['sha256']
                or document['source_bundle_size']!=source['size'] or document['export_event_id']!=event
                or digest(files['source.bundle'])!=source['sha256'] or len(files['source.bundle'])!=source['size']
                or document['scanner_digest']!=SCANNER_DIGEST or digest(files['view.tar'])!=document['view_digest']
                or digest(canonical(document['manifest']).encode())!=document['manifest_digest']):fail()
        view=_unpack(files['view.tar']);manifest=document['manifest']
        if (type(manifest) is not list or len(manifest)!=len(view) or {item['name'] for item in manifest}!=set(view)):fail()
        for item in manifest:
            if (type(item) is not dict or set(item)!={'name','size_bytes','sha256','executable'}
                    or type(item['executable']) is not bool or type(item['size_bytes']) is not int
                    or len(view[item['name']])!=item['size_bytes'] or digest(view[item['name']])!=item['sha256']):fail()
        SourceScanner().scan(view)
        expected_context=dict(context,source_kind=KIND,manifest_digest=document['manifest_digest'],
            baseline_oid=metadata['baseline_oid'],source_bundle_ref=source['artifact_ref'],
            source_bundle_digest=source['sha256'],export_event_id=event)
        if document['source_context']!=expected_context:fail()
        if RepositoryInputView(dict(manifest=manifest),MappingProxyType(view)).package()!=files['view.tar']:fail()
        receipt=self.input_gate(document['source_context'],MappingProxyType(view))
        if type(receipt) is not SourceExposureReceipt:fail()
        _sha(receipt.scan_digest);_sha(document['history_scan_digest'])
        actual=dict(context_digest=receipt.context_digest,manifest_digest=receipt.manifest_digest,
                    scan_digest=receipt.scan_digest,passed=receipt.passed)
        if (actual!=document['exposure'] or receipt.passed is not True
                or receipt.context_digest!=digest(canonical(expected_context).encode())
                or receipt.manifest_digest!=document['manifest_digest']):fail()
        return document,view

    @_safe
    def inputs(self,task,evidence):
        repositories=task_repositories(self.store,task)
        if (type(evidence) is not tuple or len(evidence)!=len(repositories)
                or tuple(sorted(item['repo_id'] for item in evidence))!=repositories):fail()
        result={};views=[]
        for item in evidence:
            repo=item['repo_id'];key=self._key(task,repo)
            original=_confirmed(self.store,task['owner_ref'],key,'repository.sealed_source')
            if original is None or original['arguments']!=self._arguments(task,repo) or original['event_id']!=item['event_id']:fail()
            record=original['result']
            if any(record[name]!=item[name] for name in item if name!='event_id'):fail()
            with tempfile.TemporaryDirectory(prefix='.restore-',dir=self.root) as temporary:
                destination=Path(temporary)/'snapshot'
                self.archive.restore(destination,scope=dict(owner_ref=task['owner_ref'],task_ref=task['task_id'],
                    revision=task['revision'],checkpoint_ref=record['backup_ref']),receipt=ArchiveReceipt.from_document(record['archive']))
                if {path.name for path in destination.iterdir()}!={'snapshot.tar'}:fail()
                raw=_read(destination/'snapshot.tar',MAX_SNAPSHOT)
                if digest(raw)!=record['state_sha256']:fail()
                with SealedBytes(raw) as sealed:document,files=self._decode(task,repo,repositories,sealed.read())
            metadata=dict(version=2,source_kind=KIND,repo_ref=repo,workspace_ref=task['workspace_ref'],
                backup_ref=record['backup_ref'],state_sha256=record['state_sha256'],baseline_oid=document['baseline_oid'],
                head_oid=document['baseline_oid'],source_bundle_ref=document['source_bundle_ref'],
                source_bundle_digest=document['source_bundle_digest'],manifest=document['manifest'],
                excluded_paths=[],exposure=document['exposure'])
            package=RepositoryInputView(metadata,MappingProxyType(files)).package();prefix='repositories/'+repo
            result[prefix+'.tar']=package;result[prefix+'.json']=canonical(metadata).encode()
            views.append(dict(repo_id=repo,view_digest=digest(result[prefix+'.json']),package_digest=digest(package)))
        if task_repositories(self.store,task)!=repositories:fail()
        return result,tuple(views)


class CompleteStoppedCheckpointChanges(ArchivedCodeChanges):
    """Audit all original stopped bytes; no source path or upload substitutions."""
    @_safe
    def __call__(self,binding,*,deadline):
        _deadline(deadline)
        task=self.store.work_task(binding['owner_ref'],binding['code_task_ref'])
        evidence=json.loads(task['runtime_result_json'],object_pairs_hook=_pairs);execution=evidence.get('execution',{})
        original=_confirmed(self.store,binding['owner_ref'],'archive:'+task['lease_ref'],'work.checkpoint')
        if (original is None or original['arguments'].get('evidence')!=evidence
                or execution.get('checkpoint_confirmed') is not True or execution.get('evidence')!='exited'
                or execution.get('input_revision')!=binding['code_revision']):fail()
        _private(self.root)
        with tempfile.TemporaryDirectory(prefix='.complete-checkpoint-',dir=self.root) as temporary:
            destination=Path(temporary)/'checkpoint'
            self.archive.restore(destination,scope=dict(owner_ref=binding['owner_ref'],task_ref=task['task_id'],
                revision=binding['code_revision'],checkpoint_ref=execution['checkpoint_ref']),
                receipt=ArchiveReceipt.from_document(evidence['checkpoint_archive']))
            actual=[]
            for path in destination.rglob('*'):
                if path.is_dir():continue
                raw=_read(path,MAX_BYTES)
                actual.append(dict(name=path.relative_to(destination).as_posix(),size_bytes=len(raw),sha256=digest(raw)))
            if sorted(actual,key=lambda item:item['name'])!=sorted(execution.get('files',[]),key=lambda item:item['name']):fail()
        _deadline(deadline)
        return super().__call__(binding,deadline=deadline)
