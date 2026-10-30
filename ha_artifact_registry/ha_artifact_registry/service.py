"""Signed exact publication grants and fsync/exclusive immutable version slots."""
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime,timedelta
import base64
import fcntl
import hmac
import os
from pathlib import Path
import re
import stat
import time
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .image import (MAX_ARCHIVE,SHA,canonical,decode,deadline_check,fail,sha,verify_archive)

TZ=ZoneInfo('Asia/Shanghai')
REF=re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z')
REPOSITORY=re.compile(r'codex-work/(amd64|aarch64)-canary\Z')
VERSION=re.compile(r'[0-9][A-Za-z0-9_.-]{0,63}\Z')
GRANT_FIELDS={'version','grant_ref','target_ref','instance_ref','slug','repository','image_version',
    'architecture','artifact_digest','manifest_digest','config_digest','graph_digest','request_digest',
    'source_digest','recipe_digest','context_digest','build_profile',
    'source_evidence_digest','permissions_digest','qualification_digest','archive_receipt_digest',
    'issued_at','expires_at'}


def stamp(raw):
    try:value=datetime.fromisoformat(raw)
    except (ValueError,TypeError):fail('registry_grant_invalid')
    if value.tzinfo is None or value.utcoffset() is None:fail('registry_grant_invalid')
    return value.astimezone(TZ)


@dataclass(frozen=True)
class Binding:
    target_ref:str
    instance_ref:str
    slug:str
    repository:str
    architecture:str
    recipe_digest:str
    permissions_digest:str
    qualification_digest:str

    def __post_init__(self):
        if (any(not isinstance(v,str) or not REF.fullmatch(v) for v in
                (self.target_ref,self.instance_ref,self.slug))
                or not isinstance(self.repository,str) or not REPOSITORY.fullmatch(self.repository)
                or self.architecture not in {'amd64','arm64'}
                or self.repository.split('/')[1].split('-')[0] != ('aarch64' if self.architecture=='arm64' else 'amd64')):
            fail('registry_binding_invalid')
        if any(not isinstance(v,str) or not SHA.fullmatch(v) for v in
               (self.recipe_digest,self.permissions_digest,self.qualification_digest)):fail('registry_binding_invalid')


def verify_grant(envelope,public_key,binding,*,clock=None,allow_expired=False):
    clock=clock or (lambda:datetime.now(TZ))
    if not isinstance(envelope,dict) or set(envelope)!={'grant','signature'}:fail('registry_grant_invalid')
    grant=envelope['grant']
    if not isinstance(grant,dict) or set(grant)!=GRANT_FIELDS:fail('registry_grant_invalid')
    try:
        signature=base64.b64decode(envelope['signature'],validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature,canonical(grant))
    except Exception:fail('registry_grant_signature_invalid')
    if (type(grant['version']) is not int or grant['version']!=1
            or not isinstance(grant['grant_ref'],str) or not REF.fullmatch(grant['grant_ref'])
            or any(grant[name]!=getattr(binding,name) for name in ('target_ref','instance_ref','slug','repository','architecture',
                'recipe_digest','permissions_digest','qualification_digest'))
            or not isinstance(grant['image_version'],str) or not VERSION.fullmatch(grant['image_version'])
            or grant['build_profile']!='ha-canary-python-v1'):
        fail('registry_grant_invalid')
    for name in GRANT_FIELDS:
        if name.endswith('_digest') and (not isinstance(grant[name],str) or not SHA.fullmatch(grant[name])):
            fail('registry_grant_invalid')
    issued,expires=stamp(grant['issued_at']),stamp(grant['expires_at'])
    if (not timedelta(0)<expires-issued<=timedelta(hours=24) or issued>clock()
            or not allow_expired and expires<=clock()):fail('registry_grant_expired')
    return decode(canonical(grant))


def verify_runtime(graph,grant):
    metadata=decode(graph.config)
    labels={'org.codex.build.request':grant['request_digest'],'org.codex.build.recipe':grant['recipe_digest'],
        'org.codex.build.context':grant['context_digest'],'io.hass.type':'addon','io.hass.version':grant['image_version'],
        'io.hass.arch':'aarch64' if grant['architecture']=='arm64' else 'amd64'}
    expected={'User':'0:0','WorkingDir':'/app','Cmd':[],
        'Entrypoint':['/usr/bin/python3','-I','-c',
            'import runpy,sys;sys.path.insert(0,"/app");runpy.run_path("/app/main.py",run_name="__main__")'],
        'Env':['PATH=/usr/bin:/bin','TZ=Asia/Shanghai','CODEX_RELEASE_SOURCE_DIGEST='+grant['source_digest'],
               'CODEX_RELEASE_VERSION='+grant['image_version']],'Labels':labels}
    if metadata.get('config')!=expected:fail('registry_runtime_mismatch')


def _identity():
    if os.getuid()!=os.geteuid():fail('registry_uid_invalid')
    return os.geteuid()


def _directory_fd(path,*,create=False,private_leaf=True):
    """Pin every ancestor by openat, never resolve a symlink or re-open it by path."""
    path=Path(path);uid=_identity()
    if not path.is_absolute() or '..' in path.parts:fail('registry_storage_invalid')
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
    fd=os.open('/',flags);current=Path('/')
    try:
        root_info=os.fstat(fd)
        if root_info.st_uid!=0 or not stat.S_ISDIR(root_info.st_mode) or root_info.st_mode&0o022:
            fail('registry_storage_invalid')
        for part in path.parts[1:]:
            current/=part
            try:child=os.open(part,flags,dir_fd=fd)
            except FileNotFoundError:
                if not create:raise
                try:os.mkdir(part,mode=0o700,dir_fd=fd)
                except FileExistsError:pass
                os.fsync(fd);child=os.open(part,flags,dir_fd=fd)
            os.close(fd);fd=child;info=os.fstat(fd)
            sticky_temp=(current in {Path('/tmp'),Path('/private/tmp')} and info.st_uid==0
                         and bool(info.st_mode&stat.S_ISVTX))
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0,uid} or info.st_nlink<1
                    or info.st_mode&0o022 and not sticky_temp):fail('registry_storage_invalid')
        info=os.fstat(fd)
        if private_leaf and (info.st_uid!=uid or stat.S_IMODE(info.st_mode)!=0o700):fail('registry_storage_invalid')
        result=fd;fd=None;return result
    finally:
        if fd is not None:os.close(fd)


def private(path,*,create=False):
    fd=_directory_fd(path,create=create)
    os.close(fd)
    return Path(path)


def read(path,limit=MAX_ARCHIVE):
    path=Path(path);parent=_directory_fd(path.parent);fd=None
    try:
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        info=os.fstat(fd);uid=_identity()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_uid!=uid
                or info.st_nlink!=1 or not 0<=info.st_size<=limit):fail('registry_storage_invalid')
        with os.fdopen(fd,'rb',closefd=False) as stream:raw=stream.read(limit+1)
        after=os.fstat(fd);linked=os.stat(path.name,dir_fd=parent,follow_symlinks=False)
        fields=('st_dev','st_ino','st_uid','st_mode','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
        if len(raw)!=info.st_size or any(getattr(info,k)!=getattr(after,k) or getattr(info,k)!=getattr(linked,k) for k in fields):
            fail('registry_storage_invalid')
        return raw
    finally:
        if fd is not None:os.close(fd)
        os.close(parent)


def sync(path):
    fd=_directory_fd(path)
    try:os.fsync(fd)
    finally:os.close(fd)


def write(path,raw):
    path=Path(path);parent=_directory_fd(path.parent);fd=None
    try:
        fd=os.open(path.name,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600,dir_fd=parent)
        with os.fdopen(fd,'wb',closefd=False) as stream:stream.write(raw);stream.flush();os.fsync(fd)
        info=os.fstat(fd);linked=os.stat(path.name,dir_fd=parent,follow_symlinks=False)
        if (info.st_uid!=_identity() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1
                or info.st_size!=len(raw) or (info.st_dev,info.st_ino)!=(linked.st_dev,linked.st_ino)):
            fail('registry_storage_invalid')
    finally:
        if fd is not None:os.close(fd)
        os.close(parent)


class RegistryStore:
    def __init__(self,root,*,public_key,bindings,grants,writer_token,reader_token,enabled=False,clock=None):
        self.root=Path(root);self.public_key=public_key;self.clock=clock or (lambda:datetime.now(TZ))
        self.uid=_identity()
        if (not isinstance(public_key,bytes) or len(public_key)!=32 or type(enabled) is not bool
                or not 1<=len(bindings)<=8 or any(not isinstance(b,Binding) for b in bindings)
                or not 0<=len(grants)<=24 or not all(isinstance(t,str) and 32<=len(t)<=256 and t.isascii()
                    and not any(c.isspace() for c in t) for t in (writer_token,reader_token))
                or hmac.compare_digest(writer_token,reader_token)):fail('registry_config_invalid')
        self.bindings={b.target_ref:b for b in bindings};self.grants={};self.slots={};self.unknown_grants=set()
        if len(self.bindings)!=len(bindings) or len({b.repository for b in bindings})!=len(bindings):fail('registry_config_invalid')
        self.writer,self.reader,self.enabled=writer_token,reader_token,enabled
        self.static=tuple(decode(canonical(item)) for item in grants)
        self._refresh()

    def _verified(self,envelope,*,allow_expired=True):
        body=envelope.get('grant',{}) if isinstance(envelope,dict) else {}
        binding=self.bindings.get(body.get('target_ref'))
        if binding is None:fail('registry_config_invalid')
        return verify_grant(envelope,self.public_key,binding,clock=self.clock,allow_expired=allow_expired)

    def _refresh(self):
        records=list(self.static);unknown=set();directory=self.root/'.grants'
        if self.root.exists():private(self.root)
        if directory.exists():
            private(directory)
            paths=list(directory.iterdir())
            if len(paths)>24:fail('registry_registration_limit')
            for path in paths:
                name=path.name.removesuffix('.json')
                if path.name!=name+'.json' or not REF.fullmatch(name):fail('registry_registration_invalid')
                try:
                    raw=read(path,65536);envelope=decode(raw);grant=self._verified(envelope)
                    if raw!=canonical(envelope) or grant['grant_ref']!=name:fail('registry_registration_invalid')
                    records.append(envelope)
                except Exception:unknown.add(name)
        admitted={};slots={}
        for envelope in records:
            grant=self._verified(envelope)
            slot=grant['repository'],grant['image_version']
            if grant['grant_ref'] in admitted:
                if admitted[grant['grant_ref']]!=envelope:fail('registry_registration_conflict')
                continue
            if slot in slots:fail('registry_config_invalid')
            admitted[grant['grant_ref']]=decode(canonical(envelope));slots[slot]=grant['grant_ref']
        if len(admitted)>24:fail('registry_registration_limit')
        self.grants,self.slots,self.unknown_grants=admitted,slots,unknown

    @contextmanager
    def _locked_registration(self):
        private(self.root,create=True);parent=_directory_fd(self.root);fd=None
        try:
            fd=os.open('.registration.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=parent)
            info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=self.uid or info.st_nlink!=1
                    or stat.S_IMODE(info.st_mode)!=0o600):fail('registry_storage_invalid')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:fail('registry_registration_busy')
            yield
        finally:
            if fd is not None:os.close(fd)
            os.close(parent)

    def register(self,envelope,authorization,*,deadline):
        self.authenticate(authorization,'writer');grant=self._verified(envelope,allow_expired=False)
        deadline_check(deadline)
        with self._locked_registration():
            self._refresh()
            if self.unknown_grants:fail('registry_registration_unknown')
            ref=grant['grant_ref'];slot=grant['repository'],grant['image_version']
            if ref in self.grants:
                if self.grants[ref]!=envelope:fail('registry_registration_conflict')
                return {'version':1,'grant_ref':ref,'state':'registered','execute_allowed':False}
            if slot in self.slots:fail('registry_registration_conflict')
            if len(self.grants)>=24:fail('registry_registration_limit')
            directory=private(self.root/'.grants',create=True);sync(self.root);deadline_check(deadline)
            write(directory/(ref+'.json'),canonical(envelope));sync(directory)
            self._refresh()
            return {'version':1,'grant_ref':ref,'state':'registered','execute_allowed':False}

    def registration(self,grant_ref):
        if not self.enabled:fail('registry_disabled')
        if not isinstance(grant_ref,str) or not REF.fullmatch(grant_ref):fail('registry_grant_denied')
        self._refresh()
        state='registered' if grant_ref in self.grants else 'unknown' if grant_ref in self.unknown_grants else 'absent'
        return {'version':1,'grant_ref':grant_ref,'state':state,'envelope':self.grants.get(grant_ref),
                'execute_allowed':False}

    def authenticate(self,authorization,role):
        if _identity()!=self.uid:fail('registry_uid_invalid')
        if not self.enabled:fail('registry_disabled')
        expected=self.writer if role=='writer' else self.reader
        if not isinstance(authorization,str) or not hmac.compare_digest(authorization,'Bearer '+expected):
            fail('registry_not_authorized')

    def grant(self,grant_ref,*,write_access=False):
        if _identity()!=self.uid:fail('registry_uid_invalid')
        if not self.enabled:fail('registry_disabled')
        self._refresh()
        if write_access and self.unknown_grants:fail('registry_registration_unknown')
        envelope=self.grants.get(grant_ref)
        if envelope is None:fail('registry_grant_denied')
        binding=self.bindings[envelope['grant']['target_ref']]
        return verify_grant(envelope,self.public_key,binding,clock=self.clock,allow_expired=not write_access)

    def directory(self,grant):
        return self.root/sha(canonical([grant['repository'],grant['image_version']]))[7:]

    def _ready(self,grant):
        private(self.root);directory=private(self.directory(grant))
        original=read(directory/'grant.json',65536)
        if original!=canonical(self.grants[grant['grant_ref']]):fail('registry_publication_changed')
        if read(directory/'claim.json',4096)!=canonical({'version':1,'grant_digest':sha(original)}):
            fail('registry_publication_changed')
        receipt=decode(read(directory/'ready.json',65536))
        if receipt!={'version':1,'grant_digest':sha(original),'artifact_digest':grant['artifact_digest'],
                     'manifest_digest':grant['manifest_digest'],'graph_digest':grant['graph_digest']}:
            fail('registry_publication_changed')
        return directory

    def publish(self,grant_ref,raw,authorization,*,deadline):
        self.authenticate(authorization,'writer');grant=self.grant(grant_ref,write_access=True)
        deadline_check(deadline);directory=self.directory(grant)
        if directory.exists():return self.status(grant_ref)
        graph=verify_archive(raw,artifact_digest=grant['artifact_digest'],architecture=grant['architecture'],
            manifest_digest=grant['manifest_digest'],config_digest=grant['config_digest'],deadline=deadline)
        if graph.graph_digest!=grant['graph_digest']:fail('registry_graph_mismatch')
        verify_runtime(graph,grant)
        private(self.root,create=True)
        try:directory.mkdir(mode=0o700)
        except FileExistsError:return self.status(grant_ref)
        sync(self.root)
        write(directory/'claim.json',canonical({'version':1,'grant_digest':sha(canonical(self.grants[grant_ref]))}))
        sync(directory)
        # Revocation/expiry is checked again immediately before publication.
        self.grant(grant_ref,write_access=True);deadline_check(deadline)
        write(directory/'grant.json',canonical(self.grants[grant_ref]));write(directory/'artifact.tar',raw)
        write(directory/'manifest',graph.manifest)
        for hashed,data in graph.blobs.items():write(directory/hashed[7:],data)
        sync(directory);deadline_check(deadline)
        receipt={'version':1,'grant_digest':sha(canonical(self.grants[grant_ref])),
                 'artifact_digest':grant['artifact_digest'],'manifest_digest':grant['manifest_digest'],
                 'graph_digest':grant['graph_digest']}
        write(directory/'ready.json',canonical(receipt));sync(directory)
        return self.status(grant_ref)

    def status(self,grant_ref):
        grant=self.grant(grant_ref)
        try:self._ready(grant);state='known'
        except Exception:state='unknown'
        return {'version':1,'grant_ref':grant_ref,'state':state,'execute_allowed':False}

    def manifest(self,repository,reference):
        if not self.enabled:fail('registry_disabled')
        self._refresh()
        grant_ref=self.slots.get((repository,reference))
        if grant_ref is None and isinstance(reference,str) and SHA.fullmatch(reference):
            matches=[ref for (repo,_),ref in self.slots.items() if repo==repository
                     and self.grants[ref]['grant']['manifest_digest']==reference]
            if matches:grant_ref=matches[0]
        if grant_ref is None:fail('registry_manifest_unknown')
        grant=self.grant(grant_ref);directory=self._ready(grant);raw=read(directory/'manifest',1024*1024)
        if sha(raw)!=grant['manifest_digest']:fail('registry_publication_changed')
        return grant,raw

    def blob(self,repository,hashed):
        if not isinstance(hashed,str) or not SHA.fullmatch(hashed):fail('registry_blob_unknown')
        for (repo,version),_ in self.slots.items():
            if repo!=repository:continue
            try:
                grant,raw=self.manifest(repo,version);manifest=decode(raw)
                permitted={item['digest'] for item in manifest['layers']}|{manifest['config']['digest']}
                if hashed not in permitted:continue
                data=read(self._ready(grant)/hashed[7:])
                descriptors=manifest['layers']+[manifest['config']]
                if sha(data)!=hashed or any(d['size']!=len(data) for d in descriptors if d['digest']==hashed):fail()
                return data
            except FileNotFoundError:continue
        fail('registry_blob_unknown')
