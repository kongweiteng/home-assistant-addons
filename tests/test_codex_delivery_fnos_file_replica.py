"""Real AES and fault/recovery fixtures, never qualification of a real fnOS."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery.durability import ReplicaReceipt, Snapshot, digest
from codex_delivery.codex_delivery.encrypted_replica import EncryptedReplica, MAGIC, ReplicaError
from codex_delivery.codex_delivery.fnos_file_replica import (
    FileBinding, FileReplicaError, FnosFileReplica, POLICY, UploadResult,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_KEY = b'B' * 32
BINDING = FileBinding('nas-fixture', 'private-objects-fixture', 'unqualified-fixture')


class MemoryFilePort:
    def __init__(self):
        self.binding = BINDING
        self.objects, self.reads, self.writes, self.deadlines = {}, [], [], []
        self.upload_mode = None
        self.read_error = False
        self.entered, self.release = threading.Event(), threading.Event()

    def get_exact(self, ref, *, deadline):
        self.deadlines.append(deadline)
        self.reads.append(ref)
        if self.read_error:
            raise OSError('fixture credential and private path must never be echoed')
        return self.objects.get(ref)

    def upload_once(self, ref, envelope, *, deadline):
        self.deadlines.append(deadline)
        self.writes.append((ref, envelope))
        if self.upload_mode == 'lost_before':
            raise OSError('fixture response lost before write')
        created = ref not in self.objects
        if created:
            self.objects[ref] = envelope
        if self.upload_mode == 'block':
            self.entered.set()
            self.release.wait()
        if self.upload_mode == 'lost_after':
            raise OSError('fixture response lost after write')
        if self.upload_mode == 'read_fails':
            self.read_error = True
        if self.upload_mode == 'invalid_result':
            return True
        return UploadResult.CREATED if created else UploadResult.EXISTING


class DiskFilePort(MemoryFilePort):
    """Local fake NAS retained when the separate primary tree is deleted."""
    def __init__(self, root):
        super().__init__()
        self.root = Path(root)

    def get_exact(self, ref, *, deadline):
        self.deadlines.append(deadline)
        self.reads.append(ref)
        try:
            return (self.root / ref).read_bytes()
        except FileNotFoundError:
            return None

    def upload_once(self, ref, envelope, *, deadline):
        self.deadlines.append(deadline)
        self.writes.append((ref, envelope))
        path = self.root / ref
        # Deliberately no server CAS: the client must serialize its sole writer.
        created = not path.exists()
        if created:
            path.write_bytes(envelope)
        if self.upload_mode == 'block':
            self.entered.set()
            self.release.wait()
        return UploadResult.CREATED if created else UploadResult.EXISTING


class FileReplicaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.port = MemoryFilePort()
        self.snapshot = Snapshot('EV-owner-task-fixture', b'private source fixture', digest(b'private source fixture'))

    def replica(self, **changes):
        return FnosFileReplica(changes.pop('port', self.port), changes.pop('encryption_key', FIXTURE_KEY),
            **{'binding':BINDING, 'key_ref':'fixture-key', 'failure_domain':'nas-fixture-domain',
               'journal_path':self.root/'replica.json', 'timeout_seconds':1,
               'maxbytes':8*1024*1024, **changes})

    def error(self, code, invoke):
        with self.assertRaises(FileReplicaError) as failure:
            invoke()
        self.assertEqual(failure.exception.code, code)
        self.assertEqual(str(failure.exception), code)
        self.assertNotIn('credential', str(failure.exception))

    def test_no_port_is_default_disabled_without_any_file_or_network_work(self):
        replica = FnosFileReplica(journal_path=self.root/'not-created.json')
        self.assertEqual(replica.status()['state'], 'disabled')
        self.error('replica_disabled', lambda: replica.save(self.snapshot))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_real_AES_readback_and_hash_no_plaintext_key_or_credentials_in_journal(self):
        replica = self.replica()
        receipt = replica.save(self.snapshot)
        envelope = self.port.objects[receipt.object_ref]
        self.assertTrue(envelope.startswith(MAGIC))
        self.assertNotIn(self.snapshot.content, envelope)
        self.assertEqual(replica.get(receipt), self.snapshot)
        self.assertEqual(replica.ciphertext_digest(receipt), digest(envelope))
        self.assertEqual(replica.status()['state'], 'ready')
        self.assertEqual(replica.status()['policy'], POLICY)
        self.assertFalse(replica.status()['qualified'])
        raw = (self.root/'replica.json').read_bytes()
        self.assertNotIn(self.snapshot.content, raw)
        self.assertNotIn(FIXTURE_KEY, raw)
        self.assertTrue(all((p.stat().st_mode & 0o777) == 0o600 for p in self.root.iterdir()))

    def test_repeated_save_never_seals_or_uploads_again(self):
        replica = self.replica()
        first = replica.save(self.snapshot)
        with patch.object(EncryptedReplica, 'seal', side_effect=AssertionError('new nonce forbidden')):
            self.assertEqual(replica.save(self.snapshot), first)
        self.assertEqual(len(self.port.writes), 1)

    def test_only_codec_methods_are_used_never_strong_ObjectStore_save_or_get(self):
        replica = self.replica()
        with patch.object(EncryptedReplica, 'save', side_effect=AssertionError()), \
             patch.object(EncryptedReplica, 'get', side_effect=AssertionError()):
            receipt = replica.save(self.snapshot)
            self.assertEqual(replica.get(receipt), self.snapshot)
        with self.assertRaises(ReplicaError):
            replica._codec.store.get('anything')

    def test_completed_lost_write_response_settles_by_exact_GET_without_retry(self):
        self.port.upload_mode = 'lost_after'
        replica = self.replica()
        receipt = replica.save(self.snapshot)
        self.assertEqual(replica.get(receipt), self.snapshot)
        self.assertEqual(len(self.port.writes), 1)
        self.assertEqual(set(self.port.reads), {receipt.object_ref})

    def test_unknown_missing_stays_fused_across_restarts_and_never_reuploads(self):
        self.port.upload_mode = 'lost_before'
        replica = self.replica()
        self.error('replica_write_unknown', lambda: replica.save(self.snapshot))
        before = len(self.port.reads)
        self.error('replica_write_unknown', lambda: replica.save(self.snapshot))
        self.assertEqual(len(self.port.reads), before)
        restarted = self.replica()
        self.error('replica_write_unknown', lambda: restarted.reconcile(self.snapshot))
        self.assertEqual(restarted.status()['state'], 'fused')
        self.assertEqual(len(self.port.writes), 1)

    def test_restarted_unknown_reads_exact_original_snapshot_then_allows_idempotent_save(self):
        self.port.upload_mode = 'lost_before'
        self.error('replica_write_unknown', lambda: self.replica().save(self.snapshot))
        ref, envelope = self.port.writes[0]
        self.port.objects[ref] = envelope  # Fixture eventual server completion.
        restarted = self.replica()
        before = len(self.port.reads)
        receipt = restarted.reconcile(self.snapshot)
        self.assertEqual(self.port.reads[before:], [ref])
        self.assertEqual(restarted.status()['state'], 'ready')
        self.assertEqual(restarted.save(self.snapshot), receipt)
        self.assertEqual(len(self.port.writes), 1)

    def test_unknown_wrong_ref_missing_corrupt_or_unreadable_can_never_clear_gate(self):
        self.port.upload_mode = 'read_fails'
        replica = self.replica()
        self.error('replica_write_unknown', lambda: replica.save(self.snapshot))
        other = replace(self.snapshot, event_id='EV-different-owner')
        self.error('replica_write_unknown', lambda: replica.reconcile(other))
        self.port.read_error = False
        ref, original = self.port.writes[0]
        for value in (None, original[:-1], b'not an envelope'):
            if value is None:
                self.port.objects.pop(ref, None)
            else:
                self.port.objects[ref] = value
            self.error('replica_write_unknown', lambda: replica.reconcile(self.snapshot))
            self.assertEqual(replica.status()['state'], 'fused')
        self.port.objects[ref] = original
        self.assertEqual(replica.reconcile(self.snapshot).object_ref, ref)
        self.assertEqual(len(self.port.writes), 1)

    def test_invalid_upload_receipt_never_implies_success(self):
        self.port.upload_mode = 'invalid_result'
        replica = self.replica()
        self.error('replica_write_unknown', lambda: replica.save(self.snapshot))
        self.assertEqual(replica.status()['state'], 'fused')
        self.assertEqual(replica.reconcile(self.snapshot).object_ref, self.port.writes[0][0])
        self.assertEqual(len(self.port.writes), 1)

    def test_upload_dispatch_is_preceded_by_durable_write_claim(self):
        replica = self.replica()
        original = self.port.upload_once
        observed = []
        def inspect_claim(ref, envelope, *, deadline):
            observed.append(json.loads((self.root/'replica.json').read_bytes()))
            return original(ref, envelope, deadline=deadline)
        self.port.upload_once = inspect_claim
        receipt = replica.save(self.snapshot)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]['state'], 'inflight')
        self.assertTrue(observed[0]['may_write'])
        self.assertEqual(observed[0]['object_ref'], receipt.object_ref)

    def test_every_call_in_one_save_uses_one_absolute_deadline(self):
        replica = self.replica()
        before = time.monotonic()
        replica.save(self.snapshot)
        self.assertEqual(len(set(self.port.deadlines)), 1)
        self.assertLessEqual(self.port.deadlines[0] - before, 1.01)

    def test_slow_multiple_calls_share_budget_and_no_late_success_is_returned(self):
        original = self.port.get_exact
        def slow(*args, **kwargs):
            time.sleep(0.18)
            return original(*args, **kwargs)
        self.port.get_exact = slow
        replica = self.replica(timeout_seconds=0.3)
        start = time.monotonic()
        self.error('replica_write_unknown', lambda: replica.save(self.snapshot))
        self.assertLess(time.monotonic() - start, 0.8)
        self.assertEqual(replica.status()['state'], 'fused')
        time.sleep(0.2)  # Let only the disposable fixture finish.
        calls = len(self.port.reads)
        self.error('replica_port_fused', lambda: replica.reconcile(self.snapshot))
        self.assertEqual(len(self.port.reads), calls)

    def child_reconcile(self, nas):
        script = '''import json,sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],str(Path(sys.argv[1])/'tests')]
from test_codex_delivery_fnos_file_replica import DiskFilePort,FIXTURE_KEY,BINDING
from codex_delivery.codex_delivery.fnos_file_replica import FnosFileReplica,FileReplicaError
from codex_delivery.codex_delivery.durability import Snapshot,digest
port=DiskFilePort(sys.argv[3])
replica=FnosFileReplica(port,FIXTURE_KEY,binding=BINDING,key_ref='fixture-key',failure_domain='nas-fixture-domain',journal_path=Path(sys.argv[2]),timeout_seconds=1,maxbytes=8*1024*1024)
try:
 replica.reconcile(Snapshot('EV-owner-task-fixture',b'private source fixture',digest(b'private source fixture')))
 print(json.dumps({'code':'settled','reads':len(port.reads),'writes':len(port.writes)}))
except FileReplicaError as error:
 print(json.dumps({'code':error.code,'reads':len(port.reads),'writes':len(port.writes)}))
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(ROOT),
            str(self.root/'replica.json'), str(nas)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_actual_port_timeout_retains_worker_lock_and_process_poison_never_replaces_thread(self):
        nas = self.root/'nas-fixture'
        nas.mkdir(mode=0o700)
        port = DiskFilePort(nas)
        port.upload_mode = 'block'
        self.addCleanup(port.release.set)
        replica = self.replica(port=port, timeout_seconds=0.3)
        started = time.monotonic()
        self.error('replica_write_unknown', lambda: replica.save(self.snapshot))
        self.assertLess(time.monotonic() - started, 0.9)
        self.assertTrue(port.entered.is_set())
        self.assertTrue(replica.status()['worker_active'])
        receipt = replica._receipt(self.snapshot)
        for invoke in (lambda: replica.save(self.snapshot), lambda: replica.get(receipt),
                       lambda: replica.reconcile(self.snapshot), lambda: replica.ciphertext_digest(receipt)):
            self.error('replica_port_fused', invoke)
        replacement = self.replica(port=port)
        self.error('replica_port_fused', lambda: replacement.reconcile(self.snapshot))
        self.assertEqual(self.child_reconcile(nas), {'code':'replica_worker_unsettled','reads':0,'writes':0})
        port.release.set()
        for _ in range(100):
            if not replica.status()['worker_active']:
                break
            time.sleep(0.005)
        self.error('replica_port_fused', lambda: replica.reconcile(self.snapshot))
        self.assertEqual(self.child_reconcile(nas), {'code':'settled','reads':1,'writes':0})
        self.error('replica_port_fused', lambda: replica.reconcile(self.snapshot))
        self.assertEqual(len(port.writes), 1)

    def test_restart_inflight_is_fused_before_any_fileport_access(self):
        replica = self.replica()
        path = self.root/'replica.json'
        state = json.loads(path.read_bytes())
        state.update(state='inflight', object_ref=replica._receipt(self.snapshot).object_ref, may_write=True)
        path.write_text(json.dumps(state, sort_keys=True, separators=(',', ':')))
        restarted = self.replica()
        self.assertEqual(restarted.status()['state'], 'fused')
        self.assertEqual(self.port.reads, [])
        self.error('replica_write_unknown', lambda: restarted.save(self.snapshot))

    def test_even_Port_binding_access_is_inside_executable_timeout(self):
        class BlockingBinding(MemoryFilePort):
            blocked = False
            @property
            def binding(self):
                if self.blocked:
                    self.entered.set()
                    self.release.wait()
                return self._binding
            @binding.setter
            def binding(self, value):
                self._binding = value
        port = BlockingBinding()
        self.addCleanup(port.release.set)
        replica = self.replica(port=port, timeout_seconds=0.25)
        port.blocked = True
        start = time.monotonic()
        self.error('replica_port_timeout', lambda: replica.save(self.snapshot))
        self.assertLess(time.monotonic()-start, 0.8)
        self.assertTrue(port.entered.is_set())
        self.assertTrue(replica.status()['worker_active'])
        self.assertEqual(port.reads, [])
        self.assertEqual(port.writes, [])

    def test_cross_instance_mutual_exclusion_is_required_even_without_server_CAS(self):
        first, second = self.replica(), self.replica()
        self.port.upload_mode = 'block'
        errors = []
        def work():
            try:
                first.save(self.snapshot)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=work)
        thread.start()
        self.addCleanup(self.port.release.set)
        self.assertTrue(self.port.entered.wait(1))
        self.error('replica_busy', lambda: second.save(self.snapshot))
        self.port.release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.port.writes), 1)

    def test_binding_change_is_rejected_and_never_reenrolls_existing_journal(self):
        replica = self.replica()
        original = (self.root/'replica.json').read_bytes()
        for changes in (dict(failure_domain='other-domain'), dict(key_ref='other-key'), dict(maxbytes=4096)):
            self.error('replica_journal_invalid', lambda: self.replica(**changes))
        self.assertEqual((self.root/'replica.json').read_bytes(), original)
        self.port.binding = replace(BINDING, namespace_ref='other-namespace')
        self.error('replica_binding_drift', lambda: replica.save(self.snapshot))
        self.assertEqual(self.port.writes, [])

    def test_missing_journal_or_lock_is_not_silently_reinitialized(self):
        replica = self.replica()
        (self.root/'replica.json').unlink()
        self.error('replica_journal_missing', lambda: self.replica())
        self.error('replica_journal_missing', lambda: replica.save(self.snapshot))
        self.assertFalse((self.root/'replica.json').exists())

    def test_missing_worker_lock_is_not_recreated_and_unknown_journal_codes_are_not_echoed(self):
        replica = self.replica()
        path = self.root/'replica.json'
        state = json.loads(path.read_bytes())
        state.update(state='fused', object_ref=replica._receipt(self.snapshot).object_ref,
                     may_write=True, code='replica_sensitive_value')
        path.write_text(json.dumps(state, sort_keys=True, separators=(',', ':')))
        self.error('replica_journal_invalid', lambda: self.replica())
        (self.root/'replica.json.worker-lock').unlink()
        self.error('replica_journal_missing', lambda: replica.save(self.snapshot))
        self.assertFalse((self.root/'replica.json.worker-lock').exists())

    def test_private_file_permission_symlink_hardlink_and_duplicate_JSON_are_rejected(self):
        self.replica()
        path = self.root/'replica.json'
        original = path.read_bytes()
        path.chmod(0o644)
        self.error('replica_journal_invalid', lambda: self.replica())
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        path.chmod(0o600)
        external = self.root/'external'
        external.write_bytes(original)
        external.chmod(0o600)
        path.unlink()
        path.symlink_to(external)
        self.error('replica_journal_unavailable', lambda: self.replica())
        path.unlink()
        os.link(external, path)
        self.error('replica_journal_invalid', lambda: self.replica())
        path.unlink()
        path.write_bytes(original[:-1] + b',"version":1}')
        path.chmod(0o600)
        self.error('replica_journal_invalid', lambda: self.replica())

    def test_receipt_scope_owner_event_hash_domain_and_key_are_not_interchangeable(self):
        replica = self.replica()
        receipt = replica.save(self.snapshot)
        for changed in (replace(receipt, event_id='EV-other-owner'), replace(receipt, object_ref='../escape'),
                        replace(receipt, failure_domain='other-domain')):
            before = len(self.port.reads)
            with self.assertRaises(ReplicaError):
                replica.get(changed)
            self.assertEqual(len(self.port.reads), before)
        other_root = self.root/'other-key'
        other_root.mkdir(mode=0o700)
        wrong = self.replica(encryption_key=b'C'*32, journal_path=other_root/'replica.json')
        self.error('replica_read_failed', lambda: wrong.get(receipt))
        self.assertEqual(len(self.port.writes), 1)

    def test_authenticated_envelope_for_another_owner_cannot_be_relabelled_or_overwritten(self):
        replica = self.replica()
        first = replica.save(self.snapshot)
        other = replace(self.snapshot, event_id='EV-other-owner-task')
        other_ref = replica._receipt(other).object_ref
        self.port.objects[other_ref] = self.port.objects[first.object_ref]
        self.error('replica_read_failed', lambda: replica.save(other))
        self.assertEqual(len(self.port.writes), 1)
        self.assertEqual(self.port.objects[other_ref], self.port.objects[first.object_ref])

    def test_entire_primary_tree_loss_recovers_SQLite_and_archive_read_only_from_ciphertext(self):
        from codex_delivery.codex_delivery.store import DeliveryStore, DeliveryError
        from codex_delivery.codex_delivery.object_archive import ObjectArchive, ArchiveError
        primary, nas, recovery = (self.root/name for name in ('primary', 'nas', 'recovery'))
        for directory in (primary, nas, recovery):
            directory.mkdir(mode=0o700)
        port = DiskFilePort(nas)
        replica = self.replica(port=port, journal_path=primary/'replica.json')
        store = DeliveryStore(primary/'delivery.sqlite3', local_domain='primary',
            replica_domain='nas-fixture-domain', replica=replica)
        project = store.create_project('owner-fixture', 'input-fixture', project_alias='fixture', name='Fixture')
        self.assertEqual(project['status'], 'accepted')
        db_snapshot = store.snapshot(project['event_id'])
        db_receipt = replica.save(db_snapshot)
        source = primary/'workspace'
        source.mkdir(mode=0o700)
        (source/'src').mkdir(mode=0o700)
        (source/'empty').mkdir(mode=0o700)
        files = {'src/main.py':b'print(42)\n', 'untracked.txt':b'uncommitted fixture', 'zero.bin':b''}
        expected = []
        for name, content in files.items():
            path = source/name
            path.write_bytes(content)
            path.chmod(0o600)
            expected.append(dict(name=name, size_bytes=len(content), sha256=digest(content)))
        scope = dict(owner_ref='owner-fixture', task_ref='task-fixture', revision=7, checkpoint_ref='CP-fixture')
        archive = ObjectArchive(replica, local_domain='primary', replica_domain='nas-fixture-domain')
        archive_receipt = archive.save(source, scope=scope, expected_files=expected)
        before = {p.name:digest(p.read_bytes()) for p in nas.iterdir()}
        writes = len(port.writes)
        # The supplied receipt/descriptor, binding and key model an operator's
        # independently retained recovery kit; there is no NAS discovery API.
        shutil.rmtree(primary)
        recovered = self.replica(port=port, journal_path=recovery/'replica.json', recovery_only=True)
        snapshot = recovered.get(db_receipt)
        restored = DeliveryStore.restore(recovery/'delivery.sqlite3', snapshot, db_receipt,
            local_domain='recovery-primary', replica_domain='nas-fixture-domain')
        self.assertTrue(restored.recovery_mode)
        self.assertEqual(restored.verify_event_chain(), 1)
        recovered_archive = ObjectArchive(recovered, local_domain='recovery-primary', replica_domain='nas-fixture-domain')
        for key, value in (('owner_ref','other-owner'), ('task_ref','other-task'), ('revision',8)):
            with self.assertRaises(ArchiveError):
                recovered_archive.restore(recovery/'wrong', scope={**scope,key:value}, receipt=archive_receipt)
        result = recovered_archive.restore(recovery/'workspace', scope=scope, receipt=archive_receipt)
        self.assertTrue(result['recovery_read_only'])
        for name, content in files.items():
            self.assertEqual((recovery/'workspace'/name).read_bytes(), content)
        self.assertTrue((recovery/'workspace/empty').is_dir())
        self.error('replica_recovery_read_only', lambda: recovered.save(snapshot))
        self.error('replica_recovery_read_only', lambda: recovered.reconcile(snapshot))
        with self.assertRaises(DeliveryError):
            restored.create_project('owner-fixture', 'new', project_alias='new', name='No writer')
        self.assertEqual(len(port.writes), writes)
        self.assertEqual({p.name:digest(p.read_bytes()) for p in nas.iterdir()}, before)
        self.assertFalse(primary.exists())


if __name__ == '__main__':
    unittest.main()
