"""Real encryption and loss recovery on synthetic, local immutable CAS fixtures."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from codex_delivery.codex_delivery.durability import Snapshot, digest
from codex_delivery.codex_delivery.encrypted_replica import EncryptedReplica, ReplicaError
from codex_delivery.codex_delivery.object_archive import (
    ArchiveError, ObjectArchive, ArchiveReceipt, CHUNK, _json, _snapshot,
)
from test_codex_delivery_encrypted_replica import MemoryObjects, FIXTURE_KEY


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'checkpoint'
        self.source.mkdir(mode=0o700)
        (self.source / 'src').mkdir()
        (self.source / 'empty').mkdir()
        (self.source / 'src/main.py').write_bytes(b'print(42)\n')
        (self.source / 'empty.bin').write_bytes(b'')
        self.objects = MemoryObjects()
        self.replica = EncryptedReplica(self.objects, FIXTURE_KEY,
            key_ref='archive-fixture', failure_domain='independent')
        self.archive = ObjectArchive(self.replica, local_domain='ha', replica_domain='independent')
        self.scope = dict(owner_ref='owner-fixture', task_ref='task-fixture', revision=1, checkpoint_ref='CP-fixture')

    def files(self):
        return [dict(name=p.relative_to(self.source).as_posix(), size_bytes=p.stat().st_size,
                     sha256=digest(p.read_bytes())) for p in sorted(self.source.rglob('*')) if p.is_file()]

    def save(self):
        return self.archive.save(self.source, scope=self.scope, expected_files=self.files())

    def test_recover_all_bytes_and_empty_directories_after_complete_source_loss(self):
        receipt = self.save()
        writes = len(self.objects.writes)
        for p in self.source.rglob('*'):
            if p.is_file():
                p.unlink()
        restarted = ObjectArchive(EncryptedReplica(self.objects, FIXTURE_KEY,
            key_ref='archive-fixture', failure_domain='independent'),
            local_domain='ha', replica_domain='independent')
        destination = self.root / 'recovered'
        result = restarted.restore(destination, scope=self.scope, receipt=receipt)
        self.assertTrue(result['recovery_read_only'])
        self.assertEqual((destination / 'src/main.py').read_bytes(), b'print(42)\n')
        self.assertEqual((destination / 'empty.bin').read_bytes(), b'')
        self.assertTrue((destination / 'empty').is_dir())
        self.assertEqual(len(self.objects.writes), writes)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual((destination / 'src/main.py').stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b'print(42)', b''.join(self.objects.objects.values()))

    def test_repeated_archive_uses_same_manifest_and_never_rewrites(self):
        receipt = self.save()
        writes = len(self.objects.writes)
        self.assertEqual(self.save(), receipt)
        self.assertEqual(len(self.objects.writes), writes)
        self.assertLess(len(json.dumps(receipt.document())), 600)

    def test_multichunk_restore_checks_total_and_digest(self):
        content = b'a' * CHUNK + b'b' * 127
        (self.source / 'large.bin').write_bytes(content)
        receipt = self.save()
        self.archive.restore(self.root / 'restored', scope=self.scope, receipt=receipt)
        self.assertEqual((self.root / 'restored/large.bin').read_bytes(), content)

    def test_explicit_admin_deadline_does_not_restart_default_archive_budget(self):
        receipt=self.save()
        from codex_delivery.codex_delivery import object_archive as module
        clock=[1000.0]
        def monotonic():
            clock[0]+=0.02
            return clock[0]
        with (mock.patch.object(module,'time',SimpleNamespace(monotonic=monotonic)),
              mock.patch.object(module,'MAX_SECONDS',0.01)):
            with self.assertRaises(ArchiveError):
                self.archive.restore(self.root/'too-short',scope=self.scope,receipt=receipt)
            result=self.archive.restore(self.root/'admin',scope=self.scope,receipt=receipt,deadline=1001.0)
        self.assertTrue(result['recovery_read_only'])
        self.assertEqual((self.root/'admin/src/main.py').read_bytes(),b'print(42)\n')
        for bad in (True,time.monotonic()-1,time.monotonic()+301,float('nan'),float('inf')):
            with self.subTest(bad=bad),self.assertRaises(ArchiveError):
                self.archive.restore(self.root/'invalid',scope=self.scope,receipt=receipt,deadline=bad)
        self.assertFalse((self.root/'invalid').exists())

    def test_wrong_owner_revision_and_checkpoint_are_rejected(self):
        receipt = self.save()
        for key, value in [('owner_ref', 'other'), ('task_ref', 'other'), ('revision', 2), ('checkpoint_ref', 'CP-other')]:
            with self.subTest(key=key), self.assertRaises(ArchiveError):
                self.archive.restore(self.root / 'restored', scope={**self.scope, key:value}, receipt=receipt)
        self.assertFalse((self.root / 'restored').exists())

    def test_missing_chunk_and_corrupt_ciphertext_create_no_partial_restore(self):
        receipt = self.save()
        raw = self.replica.get(receipt.manifest).content
        chunk = json.loads(raw)['files'][1]['chunks'][0]
        value = self.objects.objects.pop(chunk['object_ref'])
        with self.assertRaises((ArchiveError, ReplicaError)):
            self.archive.restore(self.root / 'restored', scope=self.scope, receipt=receipt)
        self.assertFalse((self.root / 'restored').exists())
        self.objects.objects[chunk['object_ref']] = value[:-1] + bytes([value[-1] ^ 1])
        with self.assertRaises((ArchiveError, ReplicaError)):
            self.archive.restore(self.root / 'restored', scope=self.scope, receipt=receipt)
        self.assertFalse((self.root / 'restored').exists())

    def test_changed_or_incomplete_source_cannot_commit_manifest(self):
        expected = self.files()
        (self.source / 'src/main.py').write_bytes(b'changed')
        with self.assertRaises(ArchiveError):
            self.archive.save(self.source, scope=self.scope, expected_files=expected)
        self.assertEqual(self.objects.writes, [])
        with self.assertRaises(ArchiveError):
            self.archive.save(self.source, scope=self.scope, expected_files=[])

    def test_links_and_case_collisions_are_not_silently_omitted(self):
        target = self.source / 'linked'
        target.symlink_to('src/main.py')
        with self.assertRaises(ArchiveError):
            self.save()
        target.unlink()
        os.link(self.source / 'src/main.py', target)
        with self.assertRaises(ArchiveError):
            self.save()
        target.unlink()
        # A case-insensitive host cannot hold a distinct pair; manifest paths
        # still undergo the same collision validation during remote recovery.

    def test_existing_destination_and_nonprivate_parent_are_preserved(self):
        receipt = self.save()
        existing = self.root / 'existing'
        existing.mkdir()
        (existing / 'user.txt').write_text('preserve')
        with self.assertRaises(ArchiveError):
            self.archive.restore(existing, scope=self.scope, receipt=receipt)
        self.assertEqual((existing / 'user.txt').read_text(), 'preserve')
        public = self.root / 'public'
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with self.assertRaises(ArchiveError):
            self.archive.restore(public / 'restored', scope=self.scope, receipt=receipt)

    def test_invalid_authenticated_manifest_rejects_paths_and_budget_before_writes(self):
        receipt = self.save()
        original = json.loads(self.replica.get(receipt.manifest).content)
        for bad_name in ('../escape', '/absolute', 'src/../escape', 'src\\escape', 'x\x00y'):
            document = json.loads(json.dumps(original))
            document['files'][0]['name'] = bad_name
            snapshot = _snapshot('manifest', self.scope, _json(document))
            bad = replace(receipt, manifest=self.replica.save(snapshot))
            with self.subTest(name=bad_name), self.assertRaises(ArchiveError):
                self.archive.restore(self.root / 'restored', scope=self.scope, receipt=bad)
            self.assertFalse((self.root / 'restored').exists())

    def test_unknown_later_chunk_is_reconciled_first_after_restart(self):
        real = self.replica
        class FusedReplica:
            def __init__(self):
                self.fused = None
                self.reads = []
                self.writes = []
                self.fail = True
            def status(self):
                return {'state': 'fused' if self.fused else 'ready', 'object_ref': self.fused}
            def save(self, snapshot):
                if self.fused:
                    raise AssertionError('write during unknown')
                self.writes.append(snapshot.event_id)
                result = real.save(snapshot)
                if len(self.writes) == 2 and self.fail:
                    self.fail = False
                    self.fused = result.object_ref
                    raise ReplicaError('replica_write_unknown')
                return result
            def get(self, receipt):
                return real.get(receipt)
            def reconcile(self, snapshot):
                result = real._receipt(snapshot)
                if result.object_ref != self.fused:
                    raise AssertionError('wrong reconciliation')
                self.reads.append(snapshot.event_id)
                if real.get(result) != snapshot:
                    raise AssertionError('changed')
                self.fused = None
                return result
        fused = FusedReplica()
        (self.source / 'extra.bin').write_bytes(b'extra')
        archive = ObjectArchive(fused, local_domain='ha', replica_domain='independent')
        with self.assertRaises(ArchiveError):
            archive.save(self.source, scope=self.scope, expected_files=self.files())
        failed = fused.writes[-1]
        receipt = archive.save(self.source, scope=self.scope, expected_files=self.files())
        self.assertEqual(fused.reads, [failed])
        archive.restore(self.root / 'restored', scope=self.scope, receipt=receipt)

    def test_fused_unrelated_object_cannot_be_reset_or_written(self):
        archive = ObjectArchive(self.replica, local_domain='ha', replica_domain='independent')
        self.replica.status = lambda: {'state':'fused','object_ref':'unrelated'}
        with self.assertRaises(ArchiveError):
            archive.save(self.source, scope=self.scope, expected_files=self.files())
        self.assertEqual(self.objects.writes, [])

    def test_source_permissions_file_count_and_byte_budget_fail_before_writes(self):
        self.source.chmod(0o755)
        with self.assertRaises(ArchiveError):
            self.save()
        self.source.chmod(0o700)
        for name, value in [('MAX_FILES', 1), ('MAX_BYTES', 4)]:
            with mock.patch('codex_delivery.codex_delivery.object_archive.' + name, value):
                with self.assertRaises(ArchiveError):
                    self.save()
        self.assertEqual([], self.objects.writes)

    def test_source_change_during_chunk_upload_never_commits_manifest(self):
        save = self.replica.save
        snapshots = []
        def mutate(snapshot):
            snapshots.append(snapshot)
            receipt = save(snapshot)
            (self.source / 'src/main.py').write_bytes(b'changed after initial snapshot')
            return receipt
        self.replica.save = mutate
        with self.assertRaises(ArchiveError) as caught:
            self.save()
        self.assertEqual('archive_source_changed', caught.exception.code)
        self.assertEqual(1, len(snapshots))
        self.assertEqual(b'print(42)\n', snapshots[0].content)

    def test_operation_budget_stops_before_object_writes_or_restore_creation(self):
        expected = self.files()
        with mock.patch('codex_delivery.codex_delivery.object_archive.time.monotonic', side_effect=[0, 121]):
            with self.assertRaises(ArchiveError):
                self.archive.save(self.source, scope=self.scope, expected_files=expected)
        self.assertEqual([], self.objects.writes)
        receipt = self.save()
        with mock.patch('codex_delivery.codex_delivery.object_archive.time.monotonic', side_effect=[0, 121]):
            with self.assertRaises(ArchiveError):
                self.archive.restore(self.root / 'restored', scope=self.scope, receipt=receipt)
        self.assertFalse((self.root / 'restored').exists())

    def test_receipt_rejects_nonarchive_objects_wrong_fields_and_types(self):
        document = self.save().document()
        for change in [{'event_id': 'EV-not-a-checkpoint'}, {'sha256': 'bad'},
                {'failure_domain': []}, {'object_ref': 'ER-unknown'}, {'extra': 'field'}]:
            candidate = {**document, 'manifest': {**document['manifest'], **change}}
            with self.subTest(change=change), self.assertRaises(ArchiveError):
                ArchiveReceipt.from_document(candidate)
        with self.assertRaises(ArchiveError):
            ArchiveReceipt.from_document({**document, 'version': True})

    def test_noncanonical_names_collisions_and_chunk_count_reject_before_restore(self):
        receipt = self.save()
        original = json.loads(self.replica.get(receipt.manifest).content)
        documents = []
        for name in ['e\u0301.txt', 'SRC/main.py']:
            document = json.loads(json.dumps(original))
            document['files'][0]['name'] = name
            documents.append(document)
        document = json.loads(json.dumps(original))
        document['files'][1]['chunks'] *= 2
        documents.append(document)
        for document in documents:
            snapshot = _snapshot('manifest', self.scope, _json(document))
            bad = replace(receipt, manifest=self.replica.save(snapshot))
            with self.assertRaises(ArchiveError):
                self.archive.restore(self.root / 'restored', scope=self.scope, receipt=bad)
            self.assertFalse((self.root / 'restored').exists())

    def test_chunk_of_another_scope_is_not_a_valid_current_manifest_member(self):
        receipt = self.save()
        document = json.loads(self.replica.get(receipt.manifest).content)
        snapshot = _snapshot('chunk', {**self.scope, 'owner_ref': 'other'}, b'print(42)\n')
        foreign = self.replica.save(snapshot)
        document['files'][1]['chunks'][0] = foreign.__dict__
        bad = replace(receipt, manifest=self.replica.save(_snapshot('manifest', self.scope, _json(document))))
        with self.assertRaises(ArchiveError):
            self.archive.restore(self.root / 'restored', scope=self.scope, receipt=bad)
        self.assertFalse((self.root / 'restored').exists())

    def test_storage_exception_does_not_expose_paths_or_credentials(self):
        self.replica.save = lambda snapshot: (_ for _ in ()).throw(OSError('private-path-and-token-fixture'))
        with self.assertRaises(ArchiveError) as caught:
            self.save()
        self.assertEqual('archive_unavailable', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
