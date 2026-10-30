"""Compile an explicitly scoped guest change package into a bare Git candidate.

This transforms bounded data only. No checkout, hook, user program, source script
or build command runs on the trusted host. The resulting bundle still requires
RepoManager's independent full validation/import and original-mode publication.
"""
from contextlib import closing
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import zipfile
import zlib

from .durability import digest
from .object_archive import _name, _pairs
from .repo_manager import _oid, _private, _read, _sha
from .store import DeliveryError, canonical


MAX_CHANGE_BYTES=32*1024*1024
MAX_CHANGE_FILES=4096


def parse_changes(content,*,repo_ref,baseline_oid):
    if not isinstance(content,bytes) or not 1<=len(content)<=MAX_CHANGE_BYTES:
        raise DeliveryError('repository_changes_invalid')
    try:
        with zipfile.ZipFile(io.BytesIO(content),'r') as archive:
            entries=archive.infolist()
            if not 1<=len(entries)<=MAX_CHANGE_FILES+1:
                raise ValueError()
            names=set();total=0
            for item in entries:
                _name(item.filename)
                if (item.filename.casefold() in names or item.is_dir() or item.flag_bits&1
                        or stat.S_IFMT(item.external_attr>>16) not in {0,stat.S_IFREG}
                        or not 0<=item.file_size<=MAX_CHANGE_BYTES):
                    raise ValueError()
                names.add(item.filename.casefold());total+=item.file_size
            if total>MAX_CHANGE_BYTES:raise ValueError()
            raw=archive.read('changes.json')
            value=json.loads(raw,object_pairs_hook=_pairs,
                parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            if (type(value) is not dict or set(value)!={'version','repo_ref','baseline_oid','changes'}
                    or type(value['version']) is not int or value['version']!=1
                    or value['repo_ref']!=repo_ref or value['baseline_oid']!=baseline_oid
                    or type(value['changes']) is not list or not 1<=len(value['changes'])<=MAX_CHANGE_FILES
                    or canonical(value).encode()!=raw):
                raise ValueError()
            changes={};expected={'changes.json'};paths=set()
            for item in value['changes']:
                if type(item) is not dict or item.get('operation') not in {'write','delete'}:
                    raise ValueError()
                name=_name(item['path'])
                if (any(part.casefold()=='.git' for part in name.split('/'))
                        or name.casefold() in paths):
                    raise ValueError()
                paths.add(name.casefold())
                if item['operation']=='delete':
                    if set(item)!={'path','operation'}:raise ValueError()
                    changes[name]=None
                    continue
                if (set(item)!={'path','operation','mode','entry','sha256'}
                        or type(item['mode']) is not int or item['mode'] not in {0o644,0o755}
                        or _name(item['entry']) in expected or not item['entry'].startswith('files/')):
                    raise ValueError()
                expected.add(item['entry'])
                raw=archive.read(item['entry'])
                if digest(raw)!=_sha(item['sha256']):raise ValueError()
                changes[name]=(item['mode'],raw)
            if set(item.filename for item in entries)!=expected:raise ValueError()
            return changes
    except Exception:
        raise DeliveryError('repository_changes_invalid') from None


class _Objects:
    def __init__(self,repository,baseline):
        self.root=repository/'objects'
        self.algorithm='sha1' if len(baseline)==40 else 'sha256'

    def put(self,kind,content):
        raw=kind.encode()+b' '+str(len(content)).encode()+b'\x00'+content
        oid=hashlib.new(self.algorithm,raw).hexdigest()
        folder=self.root/oid[:2]
        folder.mkdir(mode=0o700,exist_ok=True)
        info=folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or folder.is_symlink() or info.st_mode&0o022:
            raise DeliveryError('repository_changes_storage_invalid')
        target=folder/oid[2:]
        compressed=zlib.compress(raw)
        try:fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:
            fd=os.open(target,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                stored=os.read(fd,len(compressed)+len(raw)+1024)
                if zlib.decompress(stored)!=raw:raise DeliveryError('repository_changes_storage_invalid')
            finally:os.close(fd)
            return oid
        with os.fdopen(fd,'wb') as output:
            output.write(compressed);output.flush();os.fsync(output.fileno())
        return oid

    def tree(self,entries):
        root={};spellings={}
        for path,(mode,oid) in entries.items():
            parts=_name(path).split('/');node=root
            for index in range(1,len(parts)+1):
                prefix='/'.join(parts[:index]);folded=prefix.casefold()
                if folded in spellings and spellings[folded]!=prefix:
                    raise DeliveryError('repository_changes_path_conflict')
                spellings[folded]=prefix
            for part in parts[:-1]:
                if part in node and not isinstance(node[part],dict):
                    raise DeliveryError('repository_changes_path_conflict')
                node=node.setdefault(part,{})
            if parts[-1] in node and isinstance(node[parts[-1]],dict):
                raise DeliveryError('repository_changes_path_conflict')
            node[parts[-1]]=(mode,oid)
        def encode(node):
            items=[]
            for name,item in node.items():
                if isinstance(item,dict):mode,oid='40000',encode(item)
                else:mode,oid=item
                key=name.encode('utf-8')+(b'/' if mode=='40000' else b'')
                items.append((key,mode.encode()+b' '+name.encode('utf-8')+b'\x00'+bytes.fromhex(oid)))
            return self.put('tree',b''.join(value for _,value in sorted(items)))
        return encode(root)


def assemble_candidate(manager,owner,key,*,task_ref,expected_revision,repo_ref,content,expected_digest):
    if not isinstance(content,bytes) or digest(content)!=_sha(expected_digest):
        raise DeliveryError('repository_changes_invalid')
    arguments=dict(task_ref=task_ref,expected_revision=expected_revision,repo_ref=repo_ref,
                   changes_sha256=expected_digest)
    old=manager._existing(owner,key,'repository.assemble',arguments)
    if old:return old
    context,policy=manager._context(owner,task_ref,expected_revision,repo_ref,write=True)
    manager._authorize('assemble_candidate',dict(context,changes_sha256=expected_digest))
    with closing(manager.store._connect()) as connection:
        source,metadata=manager._source(connection,context)
        task=manager.store._owned(connection,'tasks','task_id',owner,task_ref)
    baseline=_oid(metadata['baseline_oid'])
    changes=parse_changes(content,repo_ref=repo_ref,baseline_oid=baseline)
    source_bytes=_read(manager._artifact_path(source['sha256']),manager.git.max_bundle)
    if digest(source_bytes)!=source['sha256']:raise DeliveryError('repository_artifact_changed')
    staging=manager.root/'candidate-assembly';_private(staging)
    budget=manager.git.budget()
    with tempfile.TemporaryDirectory(prefix='.assemble-',dir=staging) as temporary:
        root=Path(temporary);repository=root/'candidate.git';lock=root/'lock'
        fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            algorithm='sha1' if len(baseline)==40 else 'sha256'
            manager._run(None,['init','--bare','--template=','--object-format='+algorithm,str(repository)],budget,fd)
            repository.chmod(0o700)
            manager._run(repository,['bundle','unbundle',str(manager._artifact_path(source['sha256']))],budget,fd)
            manager._run(repository,['update-ref','refs/codex/baseline',baseline],budget,fd)
            _,raw=manager._run(repository,['ls-tree','-r','-z',baseline],budget,fd)
            files={}
            for item in raw.split(b'\x00'):
                if not item:continue
                header,name=item.split(b'\t',1);mode,kind,oid=header.decode('ascii').split(' ')
                if mode not in {'100644','100755'} or kind!='blob':
                    raise DeliveryError('repository_changes_baseline_unsupported')
                name=_name(name.decode('utf-8'))
                files[name]=(mode,_oid(oid))
            objects=_Objects(repository,baseline)
            for name,value in changes.items():
                budget.remaining()
                if value is None:
                    if name not in files:raise DeliveryError('repository_changes_delete_missing')
                    del files[name]
                else:
                    mode,raw=value
                    files[name]=(format(0o100000|mode,'o'),objects.put('blob',raw))
            tree=objects.tree(files)
            timestamp=str(int(datetime.fromisoformat(task['created_at']).timestamp()))
            identity='Delivery Worker <delivery@example.invalid> '+timestamp+' +0800'
            commit=('tree '+tree+'\nparent '+baseline+'\nauthor '+identity+'\ncommitter '+identity+
                    '\n\nTask '+task_ref+' revision '+str(expected_revision)+'\n').encode()
            candidate=objects.put('commit',commit)
            manager._run(repository,['update-ref',metadata['candidate_branch'],candidate],budget,fd)
            manager._run(repository,['fsck','--full','--strict','--no-reflogs'],budget,fd)
            manager._inspect_history(repository,candidate,budget,fd)
            bundle=root/'candidate.bundle'
            manager._run(repository,['bundle','create',str(bundle),metadata['candidate_branch']],budget,fd)
            bundle.chmod(0o600)
            bundle_bytes=_read(bundle,manager.git.max_bundle)
        finally:os.close(fd)
    stage_key='assembled-stage:'+hashlib.sha256(canonical(dict(owner_ref=owner,key=key)).encode()).hexdigest()
    staged=manager.stage_bundle(owner,stage_key,task_ref=task_ref,expected_revision=expected_revision,
        repo_ref=repo_ref,content=bundle_bytes,expected_digest=digest(bundle_bytes))
    if staged.get('status')!='accepted' or staged.get('durability')!='replicated':return staged
    def apply(connection,timestamp):
        manager._context(owner,task_ref,expected_revision,repo_ref,write=True)
        return dict(task_id=task_ref,repo_id=repo_ref,baseline_oid=baseline,candidate_oid=candidate,
            bundle_ref=staged['artifact_ref'],bundle_sha256=digest(bundle_bytes),changes_sha256=expected_digest)
    return manager.store._mutation(owner,key,'repository.assemble',arguments,apply)
