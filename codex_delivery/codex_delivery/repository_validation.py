"""Fixed bounded source scanning and bare candidate reads; never executes code."""
from contextlib import closing
from dataclasses import dataclass
import io
import os
from pathlib import Path
import re
import tarfile
import tempfile
from types import MappingProxyType

from .durability import digest
from .repo_manager import ValidationReceipt,_oid,_private,_read
from .repository_inputs import _permitted_name
from .store import DeliveryError,canonical

MAX_SOURCE=32*1024*1024
MAX_FILES=4096
RULES=(rb'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----',rb'AKIA[A-Z0-9]{16}',
    rb'gh[pousr]_[A-Za-z0-9]{20,}',rb'(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[=:]\s*[\"\x27][^\"\x27\r\n]{8,}[\"\x27]')
SCANNER_DIGEST=digest(canonical({'version':1,'rules':[r.decode() for r in RULES],
    'utf8_only':True,'max_bytes':MAX_SOURCE,'max_files':MAX_FILES}).encode())


def fail(code='repository_validation_unavailable'):raise DeliveryError(code) from None


class SourceScanner:
    """Conservative UTF-8/known-pattern profile; not an all-secrets guarantee."""
    policy_digest=SCANNER_DIGEST
    def __init__(self):
        self.patterns=tuple(re.compile(rule) for rule in RULES)
        for raw in (b'-----BEGIN PRIVATE KEY-----',b'AKIAABCDEFGHIJKLMNOP',b'password="actual-long-value"'):  # gitleaks:allow -- fixed synthetic scanner self-tests
            if not any(pattern.search(raw) for pattern in self.patterns):fail('repository_scanner_selfcheck_failed')
        if any(pattern.search(b'print("hello")\n') for pattern in self.patterns):fail('repository_scanner_selfcheck_failed')

    def scan(self,files):
        if not hasattr(files,'items') or not 1<=len(files)<=MAX_FILES:fail()
        inventory=[];total=0
        for name,raw in sorted(files.items()):
            _permitted_name(name)
            if type(raw) is not bytes:fail()
            total+=len(raw)
            if total>MAX_SOURCE or b'\0' in raw:fail('repository_scan_unsupported')
            try:raw.decode('utf-8')
            except UnicodeError:fail('repository_scan_unsupported')
            if any(pattern.search(raw) for pattern in self.patterns):fail('repository_scan_rejected')
            inventory.append({'name':name,'size_bytes':len(raw),'sha256':digest(raw)})
        return digest(canonical({'scanner_digest':self.policy_digest,'manifest':inventory}).encode())


@dataclass(frozen=True)
class CandidateView:
    files:object
    manifest_digest:str
    scan_digest:str
    def package(self):
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode='w',format=tarfile.USTAR_FORMAT) as archive:
            for name,raw in sorted(self.files.items()):
                _permitted_name(name);item=tarfile.TarInfo(name);item.mode=0o400;item.size=len(raw)
                archive.addfile(item,io.BytesIO(raw))
        if output.tell()>MAX_SOURCE+MAX_FILES*1024:fail()
        return output.getvalue()


class CandidateReader:
    def __init__(self,manager,scanner):
        if type(scanner) is not SourceScanner:fail()
        self.manager,self.scanner=manager,scanner
    def read(self,context):
        manager=self.manager;store=manager.store
        store.verify_event_chain()
        actual,_=manager._context(context['owner_ref'],context['task_id'],context['task_revision'],context['repo_id'],write=True)
        if any(context.get(k)!=v for k,v in actual.items()):fail()
        with closing(store._connect()) as connection:
            if connection.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone():fail()
            source,metadata=manager._source(connection,context)
            incoming=connection.execute("SELECT * FROM repository_artifacts WHERE owner_ref=? AND task_id=? "
                "AND task_revision=? AND repo_id=? AND kind='candidate_input' AND sha256=?",
                tuple(context[k] for k in ('owner_ref','task_id','task_revision','repo_id','bundle_sha256'))).fetchall()
        if (len(incoming)!=1 or metadata['baseline_oid']!=context['baseline_oid']
                or metadata['policy_digest']!=context['policy_digest']):fail()
        for artifact in (source,dict(incoming[0])):
            raw=_read(manager._artifact_path(artifact['sha256']),manager.git.max_bundle)
            if len(raw)!=artifact['size'] or digest(raw)!=artifact['sha256']:fail()
        root=store.path.parent/'repository-validation';_private(root)
        budget=manager.git.budget()
        with tempfile.TemporaryDirectory(prefix='.candidate-',dir=root) as temporary:
            temporary=Path(temporary);repository=temporary/'candidate.git'
            fd=os.open(temporary/'git.lock',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
            def run(arguments,repo=repository):return manager.git.run(manager.home,repo,arguments,budget,lockfd=fd)[1]
            try:
                run(['init','--bare','--template=',str(repository)],None);repository.chmod(0o700)
                candidate=_oid(context['candidate_oid']);baseline=_oid(context['baseline_oid'])
                run(['bundle','unbundle',str(manager._artifact_path(source['sha256']))])
                run(['update-ref','refs/codex/baseline',baseline])
                candidate_path=str(manager._artifact_path(incoming[0]['sha256']))
                run(['bundle','verify',candidate_path])
                if run(['bundle','list-heads',candidate_path])!=(candidate+' '+metadata['candidate_branch']+'\n').encode():fail()
                run(['bundle','unbundle',candidate_path])
                run(['update-ref','refs/codex/candidate',candidate]);run(['fsck','--strict','--no-reflogs'])
                run(['merge-base','--is-ancestor',baseline,candidate])
                commits=run(['rev-list',candidate]).decode('ascii').splitlines()
                if not 1<=len(commits)<=4096:fail()
                scanned={};history=[];files={};total=0
                for commit in commits:
                    tree=run(['ls-tree','-r','-z','--full-tree',_oid(commit)])
                    for entry in tree.split(b'\0'):
                        if not entry:continue
                        header,raw_name=entry.split(b'\t',1);mode,kind,oid=header.decode('ascii').split(' ')
                        name=raw_name.decode('utf-8');_permitted_name(name);_oid(oid)
                        if mode not in {'100644','100755'} or kind!='blob':fail()
                        if oid not in scanned:
                            raw=run(['cat-file','blob',oid]);total+=len(raw)
                            if total>MAX_SOURCE or len(scanned)>=MAX_FILES:fail()
                            scanned[oid]=raw
                        self.scanner.scan({name:scanned[oid]})
                        history.append({'commit':commit,'name':name,'oid':oid,'sha256':digest(scanned[oid])})
                        if len(history)>MAX_FILES:fail()
                        if commit==candidate:files[name]=scanned[oid]
                scan=self.scanner.scan(files)
                manifest=digest(canonical([{'name':k,'sha256':digest(v),'size_bytes':len(v)} for k,v in sorted(files.items())]).encode())
                return CandidateView(MappingProxyType(files),manifest,digest(canonical(
                    {'scanner_digest':SCANNER_DIGEST,'tip_scan':scan,'history':history}).encode()))
            finally:os.close(fd)


class RepositoryValidationGate:
    """Actual source/history scan plus deferred test result, no host execution."""
    def __init__(self,reader,tests):
        if type(reader) is not CandidateReader or not callable(getattr(tests,'request',None)):fail()
        self.reader,self.tests=reader,tests

    def __call__(self,context):
        view=self.reader.read(context)
        tests_digest=self.tests.request(context,view)
        return ValidationReceipt(digest(canonical(context).encode()),context['bundle_sha256'],
            context['baseline_oid'],context['candidate_oid'],view.scan_digest,tests_digest,True,True)
