"""Explicit signed, externally retained checkpoint kits and read-only recovery.

No production root, writer journal, OAuth session, refresh, upload or discovery
is opened. A trusted administrator supplies exact receipts and independent keys.
Retention-domain names are assertions; this module cannot prove physical media.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta
import base64
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import time
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .contracts import reference
from .durability import ReplicaReceipt, Snapshot, digest, verify_receipt
from .encrypted_replica import EncryptedReplica, OVERHEAD, SHA
from .fnos_file_settings import FnosFileSettings
from .object_archive import ArchiveReceipt, ObjectArchive, _scope, _json as archive_json
from .store import (DeliveryStore, SCHEMA_VERSION, _verify_task_schema,
    _verify_repository_schema, _verify_release_schema)

MAX_DESCRIPTOR=4*1024*1024
MAX_ARCHIVES=128
MAX_EVENTS=100000
ADMIN_OBJECT_READ_SECONDS=120
SHANGHAI=ZoneInfo('Asia/Shanghai')


class RecoveryKitError(RuntimeError):
    def __init__(self,code='recovery_kit_unavailable'):
        super().__init__(code);self.code=code


def fail(code='recovery_kit_invalid'):raise RecoveryKitError(code) from None


def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')


def decode(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:fail()
            result[key]=value
        return result
    try:
        if not isinstance(raw,bytes) or not 1<=len(raw)<=MAX_DESCRIPTOR:fail()
        value=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:fail())
        if type(value) is not dict:fail()
        return value
    except RecoveryKitError:raise
    except Exception:fail()


def closed(value,fields):
    if type(value) is not dict or set(value)!=set(fields):fail()
    return value


def _receipt(value,domain):
    value=closed(value,{'event_id','sha256','failure_domain','object_ref'})
    receipt=ReplicaReceipt(**value);reference(receipt.event_id)
    if (receipt.failure_domain!=domain or not isinstance(receipt.sha256,str) or not SHA.fullmatch(receipt.sha256)
            or receipt.object_ref!=EncryptedReplica.object_ref(receipt.event_id,receipt.sha256)):fail()
    return receipt


def private_directory(path):
    path=Path(path)
    try:
        info=path.lstat()
        if (not path.is_absolute() or path.resolve()!=path or not stat.S_ISDIR(info.st_mode)
                or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700):fail('recovery_kit_permissions_invalid')
        return path
    except OSError:fail('recovery_kit_permissions_invalid')


def read_private(path,*,limit=MAX_DESCRIPTOR):
    path=Path(path);private_directory(path.parent)
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(descriptor,'rb') as source:
            before=os.fstat(source.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid()
                    or stat.S_IMODE(before.st_mode)!=0o600 or before.st_nlink!=1 or not 1<=before.st_size<=limit):fail()
            raw=source.read(limit+1);after=os.fstat(source.fileno())
        identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if len(raw)!=before.st_size or identity(before)!=identity(after):fail()
        return raw
    except RecoveryKitError:raise
    except Exception:fail('recovery_kit_read_unavailable')


class _ForbiddenStore:
    def get(self,*args):fail('recovery_kit_write_forbidden')
    def put_if_absent(self,*args):fail('recovery_kit_write_forbidden')


class ReadOnlyReplica:
    """Exact GET-only capability; a timeout poisons this instance permanently."""
    def __init__(self,*,settings,encryption_key,port,failure_domain,deadline,object_read_seconds=None):
        if type(settings) is not FnosFileSettings or getattr(port,'binding',None)!=settings.binding:fail()
        reference(failure_domain)
        if type(deadline) not in {int,float} or not time.monotonic()<deadline<=time.monotonic()+600:fail()
        if object_read_seconds is None:object_read_seconds=settings.timeout_seconds
        if (type(object_read_seconds) not in {int,float} or isinstance(object_read_seconds,bool)
                or not 0<object_read_seconds<=ADMIN_OBJECT_READ_SECONDS):fail()
        self.settings,self.domain,self._port,self.deadline=settings,failure_domain,port,deadline
        self.object_read_seconds=object_read_seconds
        self._codec=EncryptedReplica(_ForbiddenStore(),encryption_key,key_ref=settings.key_ref,
            failure_domain=failure_domain,maxbytes=settings.maxbytes)
        self._lock=threading.Lock();self._poisoned=False

    def save(self,*args,**kwargs):fail('recovery_kit_write_forbidden')
    def reconcile(self,*args,**kwargs):fail('recovery_kit_write_forbidden')

    def get(self,receipt):
        if self._poisoned or not self._lock.acquire(blocking=False):fail('recovery_kit_read_unavailable')
        try:
            if time.monotonic()>=self.deadline:fail('recovery_kit_deadline')
            _receipt(asdict(receipt),self.domain)
            deadline=min(self.deadline,time.monotonic()+self.object_read_seconds)
            event=threading.Event();result={}
            def read():
                try:
                    if self._port.binding!=self.settings.binding:fail()
                    raw=self._port.get_exact(receipt.object_ref,deadline=deadline)
                    if (not isinstance(raw,bytes) or len(raw)>self.settings.maxbytes+OVERHEAD
                            or time.monotonic()>=deadline):fail()
                    result['snapshot']=self._codec.decrypt(receipt,raw)
                except BaseException:result['failed']=True
                finally:event.set()
            threading.Thread(target=read,daemon=True).start()
            if not event.wait(max(0,deadline-time.monotonic())):
                self._poisoned=True;fail('recovery_kit_deadline')
            if 'failed' in result or time.monotonic()>=deadline:
                self._poisoned=True;fail('recovery_kit_read_unavailable')
            return result['snapshot']
        finally:self._lock.release()


def _archive_refs(value,result):
    if isinstance(value,dict):
        for key,item in value.items():
            if key in {'source_archive','checkpoint_archive','archive'}:
                # Repository and software events use archive, including nested
                # software build requests. Only authenticated event bodies are
                # inspected; malformed receipts fail the checkpoint closed.
                receipt=ArchiveReceipt.from_document(item)
                result[canonical(receipt.document()).decode()]=receipt.document()
            else:_archive_refs(item,result)
    elif isinstance(value,list):
        for item in value:_archive_refs(item,result)


def database_facts(snapshot,receipt,*,primary_domain,replica_domain):
    """Inspect only authenticated snapshot bytes, never a live SQLite path."""
    verify_receipt(snapshot,receipt,local_domain=primary_domain,replica_domain=replica_domain)
    if digest(snapshot.content)!=snapshot.sha256:fail()
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.deserialize(snapshot.content);connection.row_factory=sqlite3.Row
        connection.execute('PRAGMA trusted_schema=OFF');connection.execute('PRAGMA query_only=ON')
        if (connection.execute('PRAGMA user_version').fetchone()[0]!=SCHEMA_VERSION
                or connection.execute('PRAGMA quick_check').fetchone()[0]!='ok'
                or connection.execute('PRAGMA foreign_key_check').fetchone() is not None):fail()
        _verify_task_schema(connection);_verify_repository_schema(connection);_verify_release_schema(connection)
        previous='';count=0;events={};archives={};tip=None
        for row in connection.execute('SELECT * FROM events ORDER BY sequence'):
            count+=1
            if count>MAX_EVENTS:fail()
            body=decode(row['body_json'].encode())
            if (canonical(body).decode()!=row['body_json'] or row['previous_digest']!=previous
                    or body.get('previous_digest')!=previous or digest(row['body_json'].encode())!=row['event_digest']
                    or any(body.get(k)!=row[k] for k in ('event_id','owner_ref','kind','created_at'))
                    or type(body.get('result')) is not dict
                    or body['result'].get('task_id')!=row['task_id']
                    or body['result'].get('project_id')!=row['project_id']):fail('recovery_kit_event_chain_invalid')
            refs={};_archive_refs(body,refs)
            events[row['event_id']]={'owner_ref':row['owner_ref'],'task_ref':row['task_id'],'archives':refs}
            for key,value in refs.items():
                entry=archives.setdefault(key,{'receipt':value,'event_ids':[]})
                entry['event_ids'].append(row['event_id'])
            previous=row['event_digest']
            tip={'event_id':row['event_id'],'event_digest':previous,'sequence':row['sequence'],'event_count':count}
        if tip is None or tip['event_id']!=receipt.event_id:fail('recovery_kit_checkpoint_not_tip')
        pending=connection.execute("SELECT event_id FROM durability WHERE state!='confirmed'").fetchall()
        # Frozen snapshots normally contain their own not-yet-confirmed row.
        if any(row[0]!=receipt.event_id for row in pending):fail('recovery_kit_unconfirmed_history')
        return {'schema_version':SCHEMA_VERSION,'chain_tip':tip},events,archives


def _inventory(replica,scope,receipt,destination,*,local_domain):
    archive=ObjectArchive(replica,local_domain=local_domain,replica_domain=replica.domain)
    archive.restore(destination,scope=scope,receipt=receipt,deadline=replica.deadline)
    raw=replica.get(receipt.manifest).content
    manifest=decode(raw)
    if (canonical(manifest)!=raw and archive_json(manifest)!=raw
            or manifest.get('scope')!=scope):fail()
    return {'directories':manifest['directories'],'files':manifest['files']}


def build_descriptor(*,replica,database_receipt,archives,checkpoint_ref,signer_ref,signing_key,
                     primary_domain,retention_domain,uncovered_items,scratch_root):
    """Explicit trusted stopped checkpoint export; no production database reads."""
    try:
        if type(replica) is not ReadOnlyReplica:fail()
        for value in (checkpoint_ref,signer_ref,primary_domain,retention_domain):reference(value)
        if len({primary_domain,replica.domain,retention_domain})!=3:fail('recovery_kit_domains_invalid')
        if not isinstance(signing_key,Ed25519PrivateKey):fail()
        if not isinstance(archives,list) or len(archives)>MAX_ARCHIVES:fail()
        if (not isinstance(uncovered_items,list) or not uncovered_items or len(uncovered_items)>128
                or len(set(uncovered_items))!=len(uncovered_items)):fail()
        for value in uncovered_items:reference(value)
        db_receipt=_receipt(database_receipt,replica.domain);snapshot=replica.get(db_receipt)
        db,events,known=database_facts(snapshot,db_receipt,primary_domain=primary_domain,replica_domain=replica.domain)
        selected=[];included=set();names=set();scratch=private_directory(scratch_root)
        with tempfile.TemporaryDirectory(prefix='recovery-export-',dir=scratch) as temporary:
            for index,item in enumerate(archives):
                item=closed(item,{'archive_ref','event_id','scope','receipt'});reference(item['archive_ref']);reference(item['event_id'])
                scope=_scope(item['scope']);receipt=ArchiveReceipt.from_document(item['receipt'])
                identity=canonical(receipt.document()).decode();event=events.get(item['event_id'])
                if (item['archive_ref'] in names or identity in included or event is None
                        or identity not in event['archives'] or scope['owner_ref']!=event['owner_ref']
                        or scope['task_ref']!=event['task_ref']):fail('recovery_kit_archive_not_in_checkpoint')
                inventory=_inventory(replica,scope,receipt,Path(temporary)/str(index),local_domain=primary_domain)
                selected.append({**item,'scope':scope,'receipt':receipt.document(),'inventory':inventory})
                names.add(item['archive_ref']);included.add(identity)
        payload={'version':1,'algorithm':'Ed25519','checkpoint_ref':checkpoint_ref,'signer_ref':signer_ref,
            'created_at':datetime.now(SHANGHAI).isoformat(),'policy':'nas-live-primary-loss-v1',
            'primary_domain':primary_domain,'replica_domain':replica.domain,'retention_domain':retention_domain,
            'key_ref':replica.settings.key_ref,'fnos_file_settings':asdict(replica.settings),
            'database':dict(db,receipt=asdict(db_receipt)),'archives':selected,
            'coverage':{'mode':'explicit_archives_only','uncovered_items':uncovered_items,
                'uncovered_archives':[value for key,value in sorted(known.items()) if key not in included]}}
        payload['signature']=base64.b64encode(signing_key.sign(canonical(payload))).decode()
        raw=canonical(payload)
        if len(raw)>MAX_DESCRIPTOR:fail('recovery_kit_descriptor_too_large')
        return raw
    except RecoveryKitError:raise
    except Exception:fail('recovery_kit_export_unavailable')


def retain_descriptor(raw,*,root):
    """Private external CAS file + directory fsync; no overwrite/latest alias."""
    decode(raw);root=private_directory(root);name='RK-'+digest(raw)[7:]+'.json';path=root/name
    if path.exists():
        if read_private(path)!=raw:fail('recovery_kit_retention_conflict')
        return path
    temporary=root/('.'+name+'.'+os.urandom(8).hex())
    try:
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'wb') as output:
            output.write(raw);output.flush();os.fsync(output.fileno())
        try:os.link(temporary,path,follow_symlinks=False)
        except FileExistsError:pass
        os.unlink(temporary)
        directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(directory)
        finally:os.close(directory)
        if read_private(path)!=raw:fail('recovery_kit_retention_conflict')
        return path
    except RecoveryKitError:raise
    except Exception:fail('recovery_kit_retention_unavailable')
    finally:
        if temporary.exists():temporary.unlink()


def verify_descriptor(raw,*,anchor):
    try:
        anchor=closed(anchor,{'version','signer_ref','public_key'})
        if type(anchor['version']) is not int or anchor['version']!=1:fail()
        reference(anchor['signer_ref']);public=base64.b64decode(anchor['public_key'],validate=True)
        if len(public)!=32 or base64.b64encode(public).decode()!=anchor['public_key']:fail()
        value=closed(decode(raw),{'version','algorithm','checkpoint_ref','signer_ref','created_at','policy',
            'primary_domain','replica_domain','retention_domain','key_ref','fnos_file_settings','database','archives','coverage','signature'})
        if canonical(value)!=raw or type(value['version']) is not int or value['version']!=1 or value['algorithm']!='Ed25519':fail()
        if value['signer_ref']!=anchor['signer_ref'] or value['policy']!='nas-live-primary-loss-v1':fail()
        signature=base64.b64decode(value['signature'],validate=True)
        Ed25519PublicKey.from_public_bytes(public).verify(signature,canonical({k:v for k,v in value.items() if k!='signature'}))
        for key in ('checkpoint_ref','signer_ref','primary_domain','replica_domain','retention_domain','key_ref'):reference(value[key])
        if len({value[k] for k in ('primary_domain','replica_domain','retention_domain')})!=3:fail()
        timestamp=datetime.fromisoformat(value['created_at'])
        if timestamp.utcoffset()!=timedelta(hours=8):fail()
        settings=FnosFileSettings(**value['fnos_file_settings'])
        if settings.key_ref!=value['key_ref']:fail()
        db=closed(value['database'],{'schema_version','chain_tip','receipt'})
        if type(db['schema_version']) is not int or db['schema_version']!=SCHEMA_VERSION:fail()
        _receipt(db['receipt'],value['replica_domain'])
        tip=closed(db['chain_tip'],{'event_id','event_digest','sequence','event_count'})
        reference(tip['event_id'])
        if (not isinstance(tip['event_digest'],str) or SHA.fullmatch(tip['event_digest']) is None
                or any(type(tip[key]) is not int or not 1<=tip[key]<=2**53-1 for key in ('sequence','event_count'))
                or tip['event_count']>MAX_EVENTS or tip['sequence']<tip['event_count']
                or tip['event_id']!=db['receipt']['event_id']):fail()
        if not isinstance(value['archives'],list) or len(value['archives'])>MAX_ARCHIVES:fail()
        coverage=closed(value['coverage'],{'mode','uncovered_items','uncovered_archives'})
        if (coverage['mode']!='explicit_archives_only' or not isinstance(coverage['uncovered_items'],list)
                or not 1<=len(coverage['uncovered_items'])<=128
                or len(set(coverage['uncovered_items']))!=len(coverage['uncovered_items'])
                or not isinstance(coverage['uncovered_archives'],list) or len(coverage['uncovered_archives'])>MAX_EVENTS):fail()
        for item in coverage['uncovered_items']:reference(item)
        omitted=set()
        for item in coverage['uncovered_archives']:
            closed(item,{'receipt','event_ids'})
            receipt=ArchiveReceipt.from_document(item['receipt'])
            identity=canonical(receipt.document())
            if (identity in omitted or not isinstance(item['event_ids'],list)
                    or not 1<=len(item['event_ids'])<=MAX_EVENTS
                    or len(set(item['event_ids']))!=len(item['event_ids'])):fail()
            _receipt(asdict(receipt.manifest),value['replica_domain'])
            for event_id in item['event_ids']:reference(event_id)
            omitted.add(identity)
        return value,settings
    except RecoveryKitError:raise
    except Exception:fail('recovery_kit_signature_or_binding_invalid')


def recover_descriptor(raw,*,anchor,replica,destination,local_domain):
    """Read all listed objects into a brand-new read-only tree, then publish it."""
    try:
        value,settings=verify_descriptor(raw,anchor=anchor);reference(local_domain)
        if (type(replica) is not ReadOnlyReplica or replica.settings!=settings or replica.domain!=value['replica_domain']
                or local_domain in {value['primary_domain'],value['replica_domain']}):fail('recovery_kit_binding_invalid')
        destination=Path(destination);parent=private_directory(destination.parent)
        if destination.exists() or destination.is_symlink() or destination.name in {'','.','..'}:fail('recovery_kit_destination_exists')
        receipt=_receipt(value['database']['receipt'],replica.domain);snapshot=replica.get(receipt)
        db,events,known=database_facts(snapshot,receipt,primary_domain=value['primary_domain'],replica_domain=replica.domain)
        if canonical(value['database'])!=canonical(dict(db,receipt=asdict(receipt))):fail('recovery_kit_database_binding_invalid')
        with tempfile.TemporaryDirectory(prefix='.recovery-',dir=parent) as temporary:
            stage=Path(temporary)/'result';stage.mkdir(mode=0o700);archive_root=stage/'archives';archive_root.mkdir(mode=0o700)
            included=set();names=set()
            for item in value['archives']:
                closed(item,{'archive_ref','event_id','scope','receipt','inventory'});reference(item['archive_ref'])
                scope=_scope(item['scope']);archive_receipt=ArchiveReceipt.from_document(item['receipt'])
                identity=canonical(archive_receipt.document()).decode();event=events.get(item['event_id'])
                if (item['archive_ref'] in names or identity in included or event is None or identity not in event['archives']
                        or scope['owner_ref']!=event['owner_ref'] or scope['task_ref']!=event['task_ref']):fail()
                inventory=_inventory(replica,scope,archive_receipt,archive_root/item['archive_ref'],local_domain=local_domain)
                if canonical(inventory)!=canonical(item['inventory']):fail('recovery_kit_archive_inventory_invalid')
                names.add(item['archive_ref']);included.add(identity)
            expected=[entry for key,entry in sorted(known.items()) if key not in included]
            if canonical(value['coverage']['uncovered_archives'])!=canonical(expected):fail('recovery_kit_coverage_invalid')
            store=DeliveryStore.restore(stage/'delivery.sqlite3',snapshot,receipt,
                local_domain=local_domain,replica_domain=replica.domain)
            if not store.recovery_mode:fail()
            summary={'version':1,'descriptor_sha256':digest(raw),'checkpoint_ref':value['checkpoint_ref'],
                'recovery_read_only':True,'event_count':store.verify_event_chain(),'archives_restored':len(included),
                'uncovered_archives':len(expected),'uncovered_items':value['coverage']['uncovered_items']}
            descriptor=os.open(stage/'recovery-summary.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'wb') as output:
                output.write(canonical(summary));output.flush();os.fsync(output.fileno())
            directory=os.open(stage,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
            # Same-parent rename into a name proven absent; concurrent writers
            # to this private administrative recovery parent are excluded.
            if destination.exists() or destination.is_symlink():fail('recovery_kit_destination_exists')
            os.rename(stage,destination)
            directory=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
        return summary
    except RecoveryKitError:raise
    except Exception:fail('recovery_kit_recovery_unavailable')


def _base64_key(value):
    try:
        key=base64.b64decode(value,validate=True)
        if len(key)!=32 or base64.b64encode(key).decode()!=value:fail()
        return key
    except RecoveryKitError:raise
    except Exception:fail('recovery_kit_key_binding_invalid')


def _secrets(stream,*,key_ref,failure_domain,signer_ref=None,export=False):
    value=closed(decode(stream.read(16385)),{'version','replica_key','access_token'}|({'signing_key'} if export else set()))
    if type(value['version']) is not int or value['version']!=1:fail()
    replica_key=closed(value['replica_key'],{'version','key_ref','failure_domain','key'})
    if (type(replica_key['version']) is not int or replica_key['version']!=1
            or replica_key['key_ref']!=key_ref or replica_key['failure_domain']!=failure_domain):fail('recovery_kit_key_binding_invalid')
    key=_base64_key(replica_key['key'])
    token=value['access_token']
    if (not isinstance(token,str) or not 16<=len(token)<=4096 or re.fullmatch(r'[A-Za-z0-9._~+/-]+={0,2}',token) is None):fail()
    signer=None
    if export:
        from cryptography.hazmat.primitives import serialization
        binding=closed(value['signing_key'],{'version','key_ref','algorithm','private_key','public_key'})
        if (type(binding['version']) is not int or binding['version']!=1 or binding['key_ref']!=signer_ref
                or binding['algorithm']!='Ed25519'):fail('recovery_kit_signer_binding_invalid')
        signing=_base64_key(binding['private_key'])
        signer=Ed25519PrivateKey.from_private_bytes(signing)
        public=signer.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        if base64.b64encode(public).decode()!=binding['public_key']:fail('recovery_kit_signer_binding_invalid')
    return key,token,signer


def main(argv=None):
    """Explicit admin CLI; credentials only on stdin, no session/refresh access."""
    import argparse
    import sys
    import resource
    # Disable dumps before reading any independent key or access credential.
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    from .fnos_file_port import FnosHTTPFilePort
    parser=argparse.ArgumentParser(description='Signed external recovery checkpoint, exact GET only')
    sub=parser.add_subparsers(dest='action',required=True)
    export=sub.add_parser('export');export.add_argument('--request',required=True);export.add_argument('--retention-root',required=True);export.add_argument('--scratch-root',required=True)
    recover=sub.add_parser('recover');recover.add_argument('--descriptor',required=True);recover.add_argument('--anchor',required=True)
    recover.add_argument('--destination',required=True);recover.add_argument('--local-domain',required=True)
    arguments=parser.parse_args(argv)
    if arguments.action=='export':
        request=closed(decode(read_private(arguments.request)),{'version','checkpoint_ref','signer_ref','primary_domain','replica_domain',
            'retention_domain','fnos_file_settings','database_receipt','archives','uncovered_items'})
        if type(request['version']) is not int or request['version']!=1:fail()
        settings=FnosFileSettings(**request['fnos_file_settings']);domain=request['replica_domain']
    else:
        raw=read_private(arguments.descriptor);anchor=decode(read_private(arguments.anchor))
        value,settings=verify_descriptor(raw,anchor=anchor);domain=value['replica_domain']
    key,token,signer=_secrets(sys.stdin.buffer,key_ref=settings.key_ref,failure_domain=domain,
        signer_ref=request['signer_ref'] if arguments.action=='export' else None,export=arguments.action=='export')
    port=FnosHTTPFilePort(settings.binding,settings.endpoint,settings.directory,
        credential_provider=lambda *,deadline:token,maxbytes=settings.maxbytes+OVERHEAD)
    # Recovery administrators read retained chunks over a potentially slower
    # independent path. This read-only budget never alters the online writer's
    # registered timeout, and the original overall 300-second cap still wins.
    replica=ReadOnlyReplica(settings=settings,encryption_key=key,port=port,failure_domain=domain,
        deadline=time.monotonic()+300,object_read_seconds=ADMIN_OBJECT_READ_SECONDS)
    if arguments.action=='export':
        fields={k:v for k,v in request.items() if k not in {'version','fnos_file_settings','replica_domain'}}
        raw=build_descriptor(replica=replica,signing_key=signer,scratch_root=arguments.scratch_root,**fields)
        retain_descriptor(raw,root=arguments.retention_root)
        summary={'version':1,'descriptor_sha256':digest(raw),'checkpoint_ref':request['checkpoint_ref'],'retained':True}
    else:summary=recover_descriptor(raw,anchor=anchor,replica=replica,destination=arguments.destination,local_domain=arguments.local_domain)
    print(canonical(summary).decode());return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except RecoveryKitError as error:raise SystemExit(error.code) from None
    except Exception:raise SystemExit('recovery_kit_unavailable') from None
