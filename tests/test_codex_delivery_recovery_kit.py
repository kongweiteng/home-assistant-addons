"""Signed external kits, real SQLite/AES and new OS process; NAS is a fixture."""
from copy import deepcopy
from contextlib import closing
from dataclasses import asdict
import base64
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from codex_delivery.codex_delivery.durability import ReplicaReceipt,Snapshot,digest
from codex_delivery.codex_delivery.fnos_file_replica import FnosFileReplica
from codex_delivery.codex_delivery.fnos_file_settings import FnosFileSettings
from codex_delivery.codex_delivery.object_archive import ObjectArchive
from codex_delivery.codex_delivery.recovery_kit import (ReadOnlyReplica,RecoveryKitError,_secrets,
    build_descriptor,canonical,database_facts,decode,recover_descriptor,retain_descriptor,verify_descriptor)
from codex_delivery.codex_delivery.store import DeliveryStore,DeliveryError,SCHEMA_VERSION
from test_codex_delivery_fnos_file_replica import BINDING,DiskFilePort,FIXTURE_KEY

ROOT=Path(__file__).resolve().parents[1]
TOKEN='fixture-read-only-token-never-persist'
DOMAIN='nas-fixture-domain'


class RecoveryKitTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.root.chmod(0o700)
        self.primary,self.nas,self.external,self.scratch,self.recovery=(self.root/name for name in ('primary','nas','external','scratch','recovery'))
        for path in (self.primary,self.nas,self.external,self.scratch,self.recovery):path.mkdir(mode=0o700)
        self.settings=FnosFileSettings('nas-live-primary-loss-v1','https://nas.example.invalid','/vol1/fixture/objects',
            BINDING.store_ref,BINDING.namespace_ref,BINDING.qualification_ref,'fixture-read-credential','fixture-key',timeout_seconds=1,maxbytes=8*1024*1024)
        self.port=DiskFilePort(self.nas)
        writer=FnosFileReplica(self.port,FIXTURE_KEY,binding=BINDING,key_ref='fixture-key',failure_domain=DOMAIN,
            journal_path=self.primary/'writer.json',timeout_seconds=1,maxbytes=self.settings.maxbytes)
        self.writer=writer
        self.store=DeliveryStore(self.primary/'delivery.sqlite3',local_domain='primary-domain',replica_domain=DOMAIN,replica=writer)
        project=self.store.create_project('owner-fixture','project-key',project_alias='fixture',name='Fixture')
        source=self.primary/'workspace';source.mkdir(mode=0o700);(source/'empty').mkdir(mode=0o700)
        self.files={'main.py':b'print("secret source fixture")\n','input.bin':b'\x00uncommitted-input\xff','zero':b''}
        expected=[]
        for name,content in self.files.items():
            path=source/name;path.write_bytes(content);path.chmod(0o600)
            expected.append({'name':name,'size_bytes':len(content),'sha256':digest(content)})
        self.scope={'owner_ref':'owner-fixture','task_ref':'task-fixture','revision':3,'checkpoint_ref':'CP-fixture'}
        self.archive_receipt=ObjectArchive(writer,local_domain='primary-domain',replica_domain=DOMAIN).save(source,scope=self.scope,expected_files=expected)
        # A trusted fixture event records the exact archive descriptor in the
        # same immutable DB chain; no model-facing API is added for this test.
        event=self.store._mutation('owner-fixture','archive-key','work.source_archive',{},
            lambda connection,timestamp:{'task_id':'task-fixture','source_archive':self.archive_receipt.document()})
        self.database=asdict(writer.save(self.store.snapshot(event['event_id'])))
        self.entry={'archive_ref':'source-fixture','event_id':event['event_id'],'scope':self.scope,'receipt':self.archive_receipt.document()}
        self.signer=Ed25519PrivateKey.generate()
        public=self.signer.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        self.anchor={'version':1,'signer_ref':'fixture-signer','public_key':base64.b64encode(public).decode()}

    def reader(self,**changes):
        return ReadOnlyReplica(settings=changes.pop('settings',self.settings),encryption_key=changes.pop('key',FIXTURE_KEY),
            port=changes.pop('port',self.port),failure_domain=DOMAIN,deadline=changes.pop('deadline',time.monotonic()+30),**changes)

    def build(self,**changes):
        return build_descriptor(**{'replica':self.reader(),'database_receipt':self.database,'archives':[self.entry],
            'checkpoint_ref':'checkpoint-fixture','signer_ref':'fixture-signer','signing_key':self.signer,
            'primary_domain':'primary-domain','retention_domain':'external-mac-domain',
            'uncovered_items':['live-runtime-and-unarchived-git-history'],'scratch_root':self.scratch,**changes})

    def recover(self,raw,**changes):
        return recover_descriptor(raw,**{'anchor':self.anchor,'replica':self.reader(),
            'destination':self.recovery/'restored','local_domain':'new-recovery-domain',**changes})

    def resign(self,value):
        value={k:v for k,v in value.items() if k!='signature'}
        value['signature']=base64.b64encode(self.signer.sign(canonical(value))).decode();return canonical(value)

    def database_fixture(self,change):
        """Create authenticated historical/corrupt fixture bytes, not a live migration."""
        original=self.reader().get(ReplicaReceipt(**self.database))
        with closing(sqlite3.connect(':memory:')) as connection:
            connection.deserialize(original.content)
            change(connection)
            raw=connection.serialize()
        snapshot=Snapshot(original.event_id,raw,digest(raw))
        self.database=asdict(self.writer.save(snapshot))
        return snapshot,ReplicaReceipt(**self.database)

    def legacy_descriptor(self):
        def schema5(connection):
            for name in ('delivery_group_edges','delivery_group_members','delivery_groups'):
                connection.execute('DROP TABLE '+name)
            connection.execute('PRAGMA user_version=5')
        self.database_fixture(schema5)
        return self.build()

    def cli_recover(self,raw,*,upgrade=False,empty_stdin=False):
        descriptor=retain_descriptor(raw,root=self.external)
        anchor=self.external/'anchor.json';anchor.write_bytes(canonical(self.anchor));anchor.chmod(0o600)
        script='''import sys,time,runpy
from pathlib import Path
from codex_delivery.codex_delivery import fnos_file_port
nas=Path(sys.argv.pop(1))
class Port:
 def __init__(self,binding,*args,**kwargs):self.binding=binding
 def get_exact(self,ref,*,deadline):
  assert time.monotonic()<deadline
  try:return (nas/ref).read_bytes()
  except FileNotFoundError:return None
fnos_file_port.FnosHTTPFilePort=Port
runpy.run_module('codex_delivery.codex_delivery.recovery_kit',run_name='__main__')
'''
        payload={'version':1,'replica_key':{'version':1,'key_ref':'fixture-key','failure_domain':DOMAIN,
            'key':base64.b64encode(FIXTURE_KEY).decode()},'access_token':TOKEN}
        args=[sys.executable,'-B','-c',script,str(self.nas),'recover','--descriptor',str(descriptor),
              '--anchor',str(anchor),'--destination',str(self.recovery/'cli-fresh'),'--local-domain','cli-recovery-domain']
        if upgrade:args.append('--upgrade-schema')
        return subprocess.run(args,input=b'' if empty_stdin else canonical(payload),stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,cwd=ROOT,timeout=20,check=False)

    def test_schema5_original_signed_descriptor_verifies_without_resigning_or_mutation(self):
        raw=self.legacy_descriptor();original=bytes(raw);anchor=deepcopy(self.anchor)
        with patch('codex_delivery.codex_delivery.recovery_kit.build_descriptor',side_effect=AssertionError('must not resign')):
            value,settings=verify_descriptor(raw,anchor=anchor)
        self.assertEqual(value['database']['schema_version'],5)
        self.assertEqual(settings,self.settings);self.assertEqual(raw,original);self.assertEqual(anchor,self.anchor)

    def test_schema5_default_recovery_refuses_before_reads_or_target_creation(self):
        raw=self.legacy_descriptor();reads=len(self.port.reads);writes=len(self.port.writes)
        before={p.name for p in self.recovery.iterdir()}
        with self.assertRaisesRegex(RecoveryKitError,'schema_upgrade_required'):
            self.recover(raw)
        self.assertEqual(len(self.port.reads),reads);self.assertEqual(len(self.port.writes),writes)
        self.assertEqual({p.name for p in self.recovery.iterdir()},before)

    def test_schema5_explicit_recovery_migrates_with_private_backup_and_persistent_read_only_guard(self):
        raw=self.legacy_descriptor();original=bytes(raw);writes=len(self.port.writes)
        summary=self.recover(raw,upgrade_schema=True)
        self.assertEqual(raw,original);self.assertEqual(summary['source_schema_version'],5)
        self.assertEqual(summary['schema_version'],SCHEMA_VERSION);self.assertTrue(summary['schema_upgraded'])
        root=self.recovery/'restored';backup=root/summary['schema_backup']
        self.assertTrue(backup.is_file());self.assertEqual(backup.stat().st_mode&0o777,0o600)
        with closing(sqlite3.connect(backup)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0],5)
            self.assertEqual(connection.execute('SELECT recovery_required FROM runtime_guard').fetchone()[0],1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='delivery_groups'").fetchone()[0],0)
        restored=DeliveryStore(root/'delivery.sqlite3',local_domain='new-recovery-domain',replica_domain=DOMAIN)
        self.assertTrue(restored.recovery_mode)
        with self.assertRaisesRegex(DeliveryError,'recovery_read_only'):
            restored.create_project('owner-fixture','new-after-recovery',project_alias='new',name='blocked')
        self.assertEqual(restored.verify_event_chain(),2)
        for name,content in self.files.items():self.assertEqual((root/'archives/source-fixture'/name).read_bytes(),content)
        self.assertEqual(len(self.port.writes),writes)

    def test_schema5_cli_default_reports_upgrade_required_without_reading_secrets(self):
        raw=self.legacy_descriptor();before={p.name:digest(p.read_bytes()) for p in self.nas.iterdir()}
        result=self.cli_recover(raw,empty_stdin=True)
        self.assertEqual(result.returncode,1);self.assertEqual(result.stdout,b'')
        self.assertEqual(result.stderr,b'schema_upgrade_required\n')
        self.assertEqual(list(self.recovery.iterdir()),[])
        self.assertEqual({p.name:digest(p.read_bytes()) for p in self.nas.iterdir()},before)

    def test_schema5_cli_explicit_upgrade_survives_total_primary_loss(self):
        raw=self.legacy_descriptor();before={p.name:digest(p.read_bytes()) for p in self.nas.iterdir()}
        shutil.rmtree(self.primary)
        result=self.cli_recover(raw,upgrade=True)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        summary=decode(result.stdout);self.assertEqual(summary['source_schema_version'],5)
        self.assertEqual(summary['schema_version'],6);self.assertTrue(summary['recovery_read_only'])
        self.assertTrue((self.recovery/'cli-fresh'/summary['schema_backup']).is_file())
        restored=DeliveryStore(self.recovery/'cli-fresh/delivery.sqlite3',local_domain='cli-recovery-domain',replica_domain=DOMAIN)
        self.assertTrue(restored.recovery_mode);self.assertFalse(self.primary.exists())
        self.assertEqual({p.name:digest(p.read_bytes()) for p in self.nas.iterdir()},before)
        for secret in (TOKEN.encode(),FIXTURE_KEY):self.assertNotIn(secret,result.stdout+result.stderr)

    def test_schema6_database_facts_rejects_damaged_group_definition(self):
        original=self.build()
        snapshot,receipt=self.database_fixture(lambda connection:connection.execute('ALTER TABLE delivery_groups ADD COLUMN extra TEXT'))
        with self.assertRaisesRegex(RecoveryKitError,'schema_group_invalid'):
            database_facts(snapshot,receipt,primary_domain='primary-domain',replica_domain=DOMAIN)
        with self.assertRaisesRegex(RecoveryKitError,'schema_group_invalid'):self.build()
        value=decode(original);value['database']['receipt']=self.database
        with self.assertRaisesRegex(RecoveryKitError,'schema_group_invalid'):
            self.recover(self.resign(value),upgrade_schema=True)
        self.assertFalse((self.recovery/'restored').exists())

    def test_schema5_database_still_requires_original_task_repository_and_release_schema(self):
        self.legacy_descriptor()
        snapshot,receipt=self.database_fixture(lambda connection:connection.execute('ALTER TABLE repository_artifacts ADD COLUMN extra TEXT'))
        with self.assertRaisesRegex(RecoveryKitError,'schema_repository_invalid'):
            database_facts(snapshot,receipt,primary_domain='primary-domain',replica_domain=DOMAIN)

    def test_unknown_signed_database_versions_and_boolean_upgrade_are_rejected(self):
        raw=self.build()
        for version in (True,1,4,7):
            with self.subTest(version=version):
                value=decode(raw);value['database']['schema_version']=version
                with self.assertRaises(RecoveryKitError):verify_descriptor(self.resign(value),anchor=self.anchor)
        for upgrade in (1,'true',None):
            with self.subTest(upgrade=upgrade):
                with self.assertRaises(RecoveryKitError):self.recover(raw,upgrade_schema=upgrade)
        self.assertFalse((self.recovery/'restored').exists())

    def test_signed_schema_must_match_authenticated_database_before_migration(self):
        raw=self.legacy_descriptor();value=decode(raw);value['database']['schema_version']=6
        with self.assertRaisesRegex(RecoveryKitError,'recovery_kit_database_binding_invalid'):
            self.recover(self.resign(value),upgrade_schema=True)
        self.assertFalse((self.recovery/'restored').exists())

    def test_cli_reports_group_schema_error_without_publishing_or_uploading(self):
        raw=self.build();value=decode(raw)
        self.database_fixture(lambda connection:connection.execute('ALTER TABLE delivery_groups ADD COLUMN extra TEXT'))
        value['database']['receipt']=self.database
        before={p.name:digest(p.read_bytes()) for p in self.nas.iterdir()}
        result=self.cli_recover(self.resign(value),upgrade=True)
        self.assertEqual(result.returncode,1);self.assertEqual(result.stdout,b'')
        self.assertEqual(result.stderr,b'schema_group_invalid\n')
        self.assertEqual(list(self.recovery.iterdir()),[])
        self.assertEqual({p.name:digest(p.read_bytes()) for p in self.nas.iterdir()},before)

    def test_external_descriptor_has_exact_tip_full_inventory_no_key_token_or_plaintext(self):
        writes=len(self.port.writes);raw=self.build();value,settings=verify_descriptor(raw,anchor=self.anchor)
        self.assertEqual(settings,self.settings);self.assertEqual(value['database']['chain_tip']['event_count'],2)
        self.assertEqual(value['database']['chain_tip']['event_id'],self.database['event_id'])
        self.assertEqual(value['coverage']['mode'],'explicit_archives_only');self.assertEqual(value['coverage']['uncovered_archives'],[])
        self.assertEqual({v['name'] for v in value['archives'][0]['inventory']['files']},set(self.files))
        for secret in (FIXTURE_KEY,TOKEN.encode(),self.files['main.py']):self.assertNotIn(secret,raw)
        path=retain_descriptor(raw,root=self.external);self.assertEqual(path.name,'RK-'+digest(raw)[7:]+'.json')
        self.assertEqual(path.stat().st_mode&0o777,0o600);self.assertEqual(retain_descriptor(raw,root=self.external),path)
        self.assertEqual(len(self.port.writes),writes)

    def test_complete_primary_loss_new_process_uses_stdin_keys_and_no_writer_or_OAuth(self):
        raw=self.build();descriptor=retain_descriptor(raw,root=self.external)
        anchor=self.external/'anchor.json';anchor.write_bytes(canonical(self.anchor));anchor.chmod(0o600)
        before={p.name:digest(p.read_bytes()) for p in self.nas.iterdir()};shutil.rmtree(self.primary)
        # Exercise the real CLI in a fresh process. Only the wire transport is
        # replaced with a local retained ciphertext directory, without an
        # upload method, sessions or a production root argument.
        script='''import sys,time
from pathlib import Path
from codex_delivery.codex_delivery import fnos_file_port,recovery_kit
nas=Path(sys.argv[1])
class Port:
 def __init__(self,binding,*args,**kwargs):self.binding=binding
 def get_exact(self,ref,*,deadline):
  assert time.monotonic()<deadline
  try:return (nas/ref).read_bytes()
  except FileNotFoundError:return None
fnos_file_port.FnosHTTPFilePort=Port
result=recovery_kit.main(sys.argv[2:])
import resource
assert resource.getrlimit(resource.RLIMIT_CORE)==(0,0)
raise SystemExit(result)
'''
        payload={'version':1,'replica_key':{'version':1,'key_ref':'fixture-key','failure_domain':DOMAIN,
            'key':base64.b64encode(FIXTURE_KEY).decode()},'access_token':TOKEN}
        result=subprocess.run([sys.executable,'-B','-c',script,str(self.nas),'recover','--descriptor',str(descriptor),
            '--anchor',str(anchor),'--destination',str(self.recovery/'fresh'),'--local-domain','fresh-recovery-domain'],
            input=canonical(payload),stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=ROOT,timeout=20,check=False)
        self.assertEqual(result.returncode,0,result.stderr.decode());summary=decode(result.stdout)
        self.assertTrue(summary['recovery_read_only']);self.assertEqual(summary['event_count'],2)
        for name,content in self.files.items():self.assertEqual((self.recovery/'fresh/archives/source-fixture'/name).read_bytes(),content)
        self.assertTrue((self.recovery/'fresh/archives/source-fixture/empty').is_dir())
        store=DeliveryStore(self.recovery/'fresh/delivery.sqlite3',local_domain='fresh-recovery-domain',replica_domain=DOMAIN)
        self.assertTrue(store.recovery_mode)
        with self.assertRaises(DeliveryError):store.create_project('owner-fixture','new',project_alias='new',name='must not write')
        self.assertEqual({p.name:digest(p.read_bytes()) for p in self.nas.iterdir()},before)
        self.assertFalse(self.primary.exists());self.assertNotIn(TOKEN.encode(),result.stdout+result.stderr)
        self.assertNotIn(FIXTURE_KEY,result.stdout+result.stderr)

    def test_omitted_archives_are_explicit_and_arbitrary_receipts_not_in_DB_are_rejected(self):
        value=decode(self.build(archives=[]))
        self.assertEqual(len(value['coverage']['uncovered_archives']),1)
        self.assertEqual(value['coverage']['uncovered_archives'][0]['receipt'],self.archive_receipt.document())
        entry=deepcopy(self.entry);entry['event_id']='EV-not-in-this-snapshot'
        with self.assertRaises(RecoveryKitError):self.build(archives=[entry])
        entry=deepcopy(self.entry);entry['scope']['owner_ref']='another-owner'
        with self.assertRaises(RecoveryKitError):self.build(archives=[entry])

    def test_repository_software_and_nested_build_archives_are_explicitly_covered(self):
        entries=[self.entry]
        for index,kind in enumerate(('repository.archive','software.archive')):
            scope={**self.scope,'checkpoint_ref':'CP-'+kind}
            receipt=ObjectArchive(self.writer,local_domain='primary-domain',replica_domain=DOMAIN).save(
                self.primary/'workspace',scope=scope,
                expected_files=[{'name':name,'size_bytes':len(raw),'sha256':digest(raw)} for name,raw in self.files.items()])
            result={'task_id':'task-fixture','archive':receipt.document()}
            if kind=='software.archive':result['scope']=scope
            event=self.store._mutation('owner-fixture','archive-add-'+str(index),kind,{},
                lambda connection,timestamp,result=result:result)
            entries.append({'archive_ref':'archive-'+str(index),'event_id':event['event_id'],'scope':scope,'receipt':receipt.document()})
        # The real software pipeline also repeats the archive in build requests.
        nested=self.store._mutation('owner-fixture','build-claim','software.build.claim',{},
            lambda connection,timestamp:{'task_id':'task-fixture','request':{'archive':entries[-1]['receipt']}})
        self.database=asdict(self.writer.save(self.store.snapshot(nested['event_id'])))
        value=decode(self.build())
        omitted=value['coverage']['uncovered_archives']
        self.assertEqual(len(omitted),2)
        software=next(item for item in omitted if item['receipt']==entries[-1]['receipt'])
        self.assertEqual(software['event_ids'],[entries[-1]['event_id'],nested['event_id']])
        raw=self.build(archives=entries);value=decode(raw)
        self.assertEqual(value['coverage']['uncovered_archives'],[])
        summary=self.recover(raw)
        self.assertEqual(summary['archives_restored'],3)
        for entry in entries:
            self.assertEqual((self.recovery/'restored/archives'/entry['archive_ref']/'input.bin').read_bytes(),self.files['input.bin'])

    def test_signed_protocol_rejects_boolean_counts_timezone_and_hidden_coverage(self):
        raw=self.build()
        for field,bad in [('sequence',True),('event_count',True),('event_count',0),('event_count',100001),('event_digest','invalid')]:
            with self.subTest(field=field,bad=bad):
                value=decode(raw);value['database']['chain_tip'][field]=bad
                with self.assertRaises(RecoveryKitError):verify_descriptor(self.resign(value),anchor=self.anchor)
        for timestamp in ('2026-10-29T12:00:00','2026-10-29T04:00:00+00:00'):
            value=decode(raw);value['created_at']=timestamp
            with self.assertRaises(RecoveryKitError):verify_descriptor(self.resign(value),anchor=self.anchor)
        value=decode(raw);value['coverage']['uncovered_items']*=2
        with self.assertRaises(RecoveryKitError):verify_descriptor(self.resign(value),anchor=self.anchor)
        omitted=decode(self.build(archives=[]));omitted['coverage']['uncovered_archives']*=2
        with self.assertRaises(RecoveryKitError):verify_descriptor(self.resign(omitted),anchor=self.anchor)
        value=decode(raw);value['coverage']['uncovered_archives']=[{'receipt':self.archive_receipt.document(),'event_ids':[]}]
        with self.assertRaises(RecoveryKitError):verify_descriptor(self.resign(value),anchor=self.anchor)
        # A signed but incomplete omission list still must match the database.
        omitted=decode(self.build(archives=[]));omitted['coverage']['uncovered_archives']=[]
        with self.assertRaises(RecoveryKitError):self.recover(self.resign(omitted))
        self.assertFalse((self.recovery/'restored').exists())

    def test_modified_signature_wrong_anchor_key_settings_or_inventory_fail_closed(self):
        raw=self.build();value=decode(raw);value['key_ref']='different-key'
        with self.assertRaises(RecoveryKitError):self.recover(canonical(value))
        other=deepcopy(self.anchor);other['public_key']=base64.b64encode(b'X'*32).decode()
        with self.assertRaises(RecoveryKitError):self.recover(raw,anchor=other)
        with self.assertRaises(RecoveryKitError):self.recover(raw,replica=self.reader(key=b'C'*32))
        value=decode(raw);value['archives'][0]['inventory']['files'][0]['sha256']=digest(b'fake')
        with self.assertRaises(RecoveryKitError):self.recover(self.resign(value))
        self.assertFalse((self.recovery/'restored').exists())

    def test_missing_object_existing_destination_and_original_journal_are_preserved(self):
        raw=self.build();journal=(self.primary/'writer.json').read_bytes();before=len(self.port.writes)
        item=decode(raw)['archives'][0]['inventory']['files'];chunk=next(v['chunks'][0] for v in item if v['chunks'])
        (self.nas/chunk['object_ref']).unlink()
        with self.assertRaises(RecoveryKitError):self.recover(raw)
        self.assertFalse((self.recovery/'restored').exists())
        self.assertEqual((self.primary/'writer.json').read_bytes(),journal);self.assertEqual(len(self.port.writes),before)
        target=self.recovery/'existing';target.mkdir(mode=0o700);(target/'user').write_bytes(b'preserve')
        with self.assertRaises(RecoveryKitError):self.recover(raw,destination=target)
        self.assertEqual((target/'user').read_bytes(),b'preserve')

    def test_receipt_must_bind_exact_chain_tip_and_duplicate_JSON_rejected(self):
        raw=self.build();value=decode(raw)
        snapshot=self.port.get_exact(self.database['object_ref'],deadline=time.monotonic()+1)
        self.assertIsNotNone(snapshot)
        value['database']['chain_tip']['event_count']=999
        with self.assertRaises(RecoveryKitError):self.recover(self.resign(value))
        with self.assertRaises(RecoveryKitError):verify_descriptor(b'{"version":1,"version":1}',anchor=self.anchor)
        with self.assertRaises(RecoveryKitError):self.build(retention_domain='primary-domain')

    def test_get_timeout_fuses_and_never_has_write_or_reconcile(self):
        entered=threading.Event();release=threading.Event();owner=self
        class Port:
            binding=BINDING
            def get_exact(self,ref,*,deadline):entered.set();release.wait(1);return (owner.nas/ref).read_bytes()
        reader=self.reader(port=Port(),deadline=time.monotonic()+0.05);start=time.monotonic()
        with self.assertRaises(RecoveryKitError):reader.get(ReplicaReceipt(**self.database))
        self.assertLess(time.monotonic()-start,0.3);self.assertTrue(entered.is_set());release.set()
        with self.assertRaises(RecoveryKitError):reader.get(ReplicaReceipt(**self.database))
        with self.assertRaises(RecoveryKitError):reader.save(None)
        with self.assertRaises(RecoveryKitError):reader.reconcile(None)

    def test_admin_read_budget_is_separate_bounded_and_overall_deadline_still_wins(self):
        deadlines=[]
        owner=self
        class Port:
            binding=BINDING
            def get_exact(self,ref,*,deadline):
                deadlines.append(deadline)
                return (owner.nas/ref).read_bytes()
        start=time.monotonic()
        reader=self.reader(port=Port(),deadline=start+150,object_read_seconds=120)
        self.assertEqual(reader.get(ReplicaReceipt(**self.database)).sha256,self.database['sha256'])
        self.assertGreater(deadlines[0]-start,119)
        self.assertLessEqual(deadlines[0]-start,120.1)
        end=time.monotonic()+0.05
        reader=self.reader(port=Port(),deadline=end,object_read_seconds=120)
        reader.get(ReplicaReceipt(**self.database))
        self.assertEqual(deadlines[-1],end)
        self.assertEqual(self.settings.timeout_seconds,1)
        for bad in (True,0,121,float('nan'),float('inf')):
            with self.subTest(bad=bad):
                with self.assertRaises(RecoveryKitError):self.reader(object_read_seconds=bad)

    def test_Broker_stdin_binding_checks_key_domain_signer_and_derived_public(self):
        private=self.signer.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        value={'version':1,'replica_key':{'version':1,'key_ref':'fixture-key','failure_domain':DOMAIN,'key':base64.b64encode(FIXTURE_KEY).decode()},
            'access_token':TOKEN,'signing_key':{'version':1,'key_ref':'fixture-signer','algorithm':'Ed25519',
                'private_key':base64.b64encode(private).decode(),'public_key':self.anchor['public_key']}}
        def invoke(value):return _secrets(io.BytesIO(canonical(value)),key_ref='fixture-key',failure_domain=DOMAIN,signer_ref='fixture-signer',export=True)
        key,token,signer=invoke(value);self.assertEqual(key,FIXTURE_KEY);self.assertEqual(token,TOKEN)
        for parent,field,bad in [('replica_key','key_ref','wrong'),('replica_key','failure_domain','wrong'),
            ('replica_key','key','%invalid-base64'),('signing_key','private_key',False),
            ('signing_key','key_ref','wrong'),('signing_key','public_key',base64.b64encode(b'X'*32).decode())]:
            changed=deepcopy(value);changed[parent][field]=bad
            with self.assertRaises(RecoveryKitError):invoke(changed)


if __name__=='__main__':unittest.main()
