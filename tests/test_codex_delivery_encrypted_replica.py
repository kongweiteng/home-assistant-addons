"""Local crypto/CAS/mount fixtures, never acceptance of a real off-device NAS."""
from __future__ import annotations

from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery.durability import ReplicaReceipt,Snapshot,digest
from codex_delivery.codex_delivery.encrypted_replica import (
    EncryptedReplica,MAGIC,MAX_HEADER,NetworkMountObjectStore,ObjectStoreUnsupported,ReplicaError,
)


# Explicit test-only deterministic key, never production credential material.
FIXTURE_KEY=b'\x42'*32


class MemoryObjects:
    def __init__(self):
        self.objects={}; self.writes=[]; self.reads=[]; self.lock=threading.Lock()

    def get(self,ref):
        with self.lock:
            self.reads.append(ref)
            return self.objects.get(ref)

    def put_if_absent(self,ref,content):
        with self.lock:
            self.writes.append((ref,content))
            if ref in self.objects: return False
            self.objects[ref]=content
            return True


class EncryptedReplicaTests(unittest.TestCase):
    def setUp(self):
        self.objects=MemoryObjects()
        self.replica=self.adapter()
        content=b'SQLite fixture: private task text, not production data'
        self.snapshot=Snapshot('EV-fixture',content,digest(content))

    def adapter(self,**changes):
        return EncryptedReplica(changes.pop('store',self.objects),changes.pop('encryption_key',FIXTURE_KEY),
                                **{'key_ref':'fixture-key-v1','failure_domain':'test-independent',  # gitleaks:allow -- synthetic test key reference
                                   'maxbytes':4096,**changes})

    def test_encrypt_read_decrypt_and_opaque_reference(self):
        receipt=self.replica.save(self.snapshot)
        envelope=self.objects.objects[receipt.object_ref]
        self.assertTrue(envelope.startswith(MAGIC))
        self.assertNotIn(self.snapshot.content,envelope)
        self.assertNotIn(self.snapshot.event_id,receipt.object_ref)
        self.assertLessEqual(len(receipt.object_ref),200)
        self.assertEqual(self.replica.get(receipt),self.snapshot)
        self.assertEqual(self.adapter().decrypt(receipt,envelope),self.snapshot)
        self.assertEqual(receipt.failure_domain,'test-independent')

    def test_repeated_save_gets_before_encrypt_and_does_not_rewrite(self):
        receipt=self.replica.save(self.snapshot)
        with patch('codex_delivery.codex_delivery.encrypted_replica.os.urandom',side_effect=AssertionError('no new nonce')):
            self.assertEqual(self.replica.save(self.snapshot),receipt)
        self.assertEqual(len(self.objects.writes),1)

    def test_same_plaintext_different_event_gets_new_random_nonce(self):
        first=self.replica.save(self.snapshot)
        second=self.replica.save(replace(self.snapshot,event_id='EV-second'))
        first_blob=self.objects.objects[first.object_ref]; second_blob=self.objects.objects[second.object_ref]
        def nonce(blob):
            length=struct.unpack('>I',blob[len(MAGIC):len(MAGIC)+4])[0]
            return blob[len(MAGIC)+4+length:len(MAGIC)+4+length+12]
        self.assertNotEqual(nonce(first_blob),nonce(second_blob))

    def test_lost_write_response_reconciles_exact_object_without_second_put(self):
        original=self.objects.put_if_absent
        def lost(ref,data):
            original(ref,data)
            raise TimeoutError('fixture lost response')
        self.objects.put_if_absent=lost
        receipt=self.replica.save(self.snapshot)
        self.assertEqual(self.replica.get(receipt),self.snapshot)
        self.assertEqual(len(self.objects.writes),1)
        self.assertEqual(set(self.objects.reads),{receipt.object_ref})

    def test_unknown_missing_and_unreadable_are_never_treated_as_safe_overwrite(self):
        def lost_before(ref,data):
            self.objects.writes.append((ref,data)); raise TimeoutError('fixture')
        self.objects.put_if_absent=lost_before
        with self.assertRaises(ReplicaError) as error: self.replica.save(self.snapshot)
        self.assertEqual(error.exception.code,'replica_write_unknown')
        self.assertEqual(len(self.objects.writes),1)
        self.objects.get=lambda ref: (_ for _ in ()).throw(OSError('fixture read unavailable'))
        with self.assertRaises(ReplicaError): self.replica.save(self.snapshot)
        self.assertEqual(len(self.objects.writes),1)

    def test_concurrent_cas_loser_decrypts_existing_random_ciphertext(self):
        barrier=threading.Barrier(2)
        original=self.objects.get
        count=0; lock=threading.Lock()
        def simultaneous_get(ref):
            nonlocal count
            value=original(ref)
            with lock: count+=1; initial=count<=2
            if initial: barrier.wait(3)
            return value
        self.objects.get=simultaneous_get
        receipts=[]; errors=[]
        def save():
            try: receipts.append(self.adapter().save(self.snapshot))
            except BaseException as error: errors.append(error)
        threads=[threading.Thread(target=save),threading.Thread(target=save)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertFalse(any(t.is_alive() for t in threads)); self.assertEqual(errors,[])
        self.assertEqual(receipts[0],receipts[1])
        self.assertEqual(len(self.objects.objects),1)
        self.assertEqual(len(self.objects.writes),2)
        self.assertNotEqual(self.objects.writes[0][1],self.objects.writes[1][1])

    def test_corrupt_existing_object_is_not_absence_or_overwritten(self):
        receipt=self.replica.save(self.snapshot)
        self.objects.objects[receipt.object_ref]=b'corrupted'
        with self.assertRaises(ReplicaError): self.replica.save(self.snapshot)
        self.assertEqual(len(self.objects.writes),1)
        self.assertEqual(self.objects.objects[receipt.object_ref],b'corrupted')

    def test_key_domain_event_digest_and_ciphertext_tamper_fail_authentication(self):
        receipt=self.replica.save(self.snapshot); envelope=self.objects.objects[receipt.object_ref]
        for adapter in [self.adapter(encryption_key=b'\x43'*32),self.adapter(key_ref='other-key'),
                        self.adapter(failure_domain='other-domain')]:
            with self.assertRaises(ReplicaError): adapter.decrypt(receipt,envelope)
        for changed in [replace(receipt,event_id='EV-other'),replace(receipt,sha256='sha256:'+'0'*64),
                        replace(receipt,object_ref='../outside'),replace(receipt,failure_domain='other')]:
            with self.assertRaises(ReplicaError): self.replica.decrypt(changed,envelope)
        for changed in [envelope[:-1],envelope[:-1]+bytes([envelope[-1]^1]),b'bad'+envelope,
                        MAGIC+struct.pack('>I',MAX_HEADER+1)+envelope]:
            with self.assertRaises(ReplicaError): self.replica.decrypt(receipt,changed)

    def test_strict_duplicate_json_and_nonfinite_headers_are_rejected(self):
        receipt=self.replica.save(self.snapshot); envelope=self.objects.objects[receipt.object_ref]
        size=struct.unpack('>I',envelope[len(MAGIC):len(MAGIC)+4])[0]
        header=envelope[len(MAGIC)+4:len(MAGIC)+4+size]
        tail=envelope[len(MAGIC)+4+size:]
        malformed=[header.replace(b'"version":1',b'"version":1,"version":1'),
                   header.replace(b'"version":1',b'"version":NaN'),
                   header.replace(b'"version":1',b'"version":true'),
                   header.replace(b'"plain_size":'+str(len(self.snapshot.content)).encode(),b'"plain_size":Infinity')]
        for value in malformed:
            with self.assertRaises(ReplicaError):
                self.replica.decrypt(receipt,MAGIC+struct.pack('>I',len(value))+value+tail)

    def test_bounds_wrong_snapshot_and_unsupported_store_semantics_fail_closed(self):
        for key in [b'',b'x'*31,b'x'*33,'not-bytes']:
            with self.assertRaises(ReplicaError): self.adapter(encryption_key=key)
        for snapshot in [replace(self.snapshot,sha256='sha256:'+'0'*64),
                         replace(self.snapshot,content=b''),replace(self.snapshot,content=b'x'*4097)]:
            with self.assertRaises(ReplicaError): self.replica.save(snapshot)
        self.assertEqual(self.objects.writes,[])
        self.objects.put_if_absent=lambda ref,data:None
        with self.assertRaises(ObjectStoreUnsupported): self.replica.save(self.snapshot)

    def test_actual_sqlite_snapshot_encrypt_decrypt_restore_stays_recovery_only(self):
        from codex_delivery.codex_delivery.store import DeliveryStore
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); root.chmod(0o700)
            replica=self.adapter(maxbytes=4*1024*1024)
            store=DeliveryStore(root/'original.sqlite3',local_domain='primary',
                replica_domain='test-independent',replica=replica)
            project=store.create_project('fixture-owner','create-fixture',project_alias='fixture',name='Private fixture')
            self.assertEqual(project['status'],'accepted')
            snapshot=store.snapshot(project['event_id']); receipt=replica.save(snapshot)
            recovered=replica.get(receipt)
            self.assertEqual(recovered,snapshot)
            restored=DeliveryStore.restore(root/'restored.sqlite3',recovered,receipt,
                local_domain='restored-primary',replica_domain='test-independent')
            self.assertTrue(restored.recovery_mode)
            self.assertEqual(restored.verify_event_chain(),1)


def escaped(path):
    return str(path).replace('\\','\\134').replace(' ','\\040').replace('\t','\\011').replace('\n','\\012')


class NetworkMountObjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.mount=Path(self.temp.name).resolve(); self.mount.chmod(0o700)
        self.root=self.mount/'objects'; self.root.mkdir(mode=0o700)
        dev=self.root.stat().st_dev
        self.device=str(os.major(dev))+':'+str(os.minor(dev))
        self.base=f'1 0 {self.device} / / rw - ext4 /dev/local rw\n'
        self.network=f'2 1 {self.device} / {escaped(self.mount)} rw,relatime - nfs4 nas.example:/replica rw\n'
        self.mountinfo=self.base+self.network
        self.patcher=patch.object(NetworkMountObjectStore,'_mountinfo',side_effect=lambda:self.mountinfo)
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.ref=EncryptedReplica.object_ref('EV-fixture',digest(b'ciphertext fixture'))

    def store(self,**overrides):
        return NetworkMountObjectStore(**{'root':self.root,'expected_fstype':'nfs4',
            'expected_source':'nas.example:/replica','expected_mountpoint':self.mount,'maxbytes':8192,**overrides})

    def test_fixture_network_cas_is_no_replace_bounded_and_private(self):
        store=self.store()
        self.assertIsNone(store.get(self.ref))
        self.assertTrue(store.put_if_absent(self.ref,b'first'))
        self.assertFalse(store.put_if_absent(self.ref,b'second'))
        self.assertEqual(store.get(self.ref),b'first')
        self.assertEqual((self.root/self.ref).stat().st_mode&0o777,0o600)
        self.assertEqual([p.name for p in self.root.iterdir()],[self.ref])
        with self.assertRaises(ReplicaError): store.put_if_absent(self.ref,b'x'*8193)

    def test_real_local_directory_is_rejected_without_mountinfo_fixtures(self):
        self.patcher.stop()
        with self.assertRaises(ReplicaError): self.store()
        self.assertEqual(list(self.root.iterdir()),[])

    def test_local_mount_wrong_identity_readonly_and_nested_overmount_are_rejected(self):
        values=[self.base,self.mountinfo.replace('nas.example:/replica','other.example:/replica'),
                self.mountinfo.replace(' - nfs4 ',' - ext4 '),
                self.mountinfo.replace('rw,relatime','ro,relatime'),
                self.base+self.network.replace(' / '+escaped(self.mount),' /undeclared-bind '+escaped(self.mount)),
                self.mountinfo+f'3 2 {self.device} / {escaped(self.root)} rw - tmpfs tmpfs rw\n',
                self.mountinfo+self.network]
        for text in values:
            with self.subTest(text=text):
                self.mountinfo=text
                with self.assertRaises(ReplicaError): self.store()

    def test_device_mismatch_and_symlink_in_any_path_component_are_rejected(self):
        self.mountinfo=self.base+self.network.replace(self.device,'999:999')
        with self.assertRaises(ReplicaError): self.store()
        self.mountinfo=self.base+self.network
        alias=self.mount/'alias'; alias.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ReplicaError): self.store(root=alias)
        parent_alias=self.mount/'parent-alias'; parent_alias.symlink_to(self.mount,target_is_directory=True)
        fake_mount=parent_alias
        self.mountinfo=self.base+f'4 1 {self.device} / {escaped(fake_mount)} rw - nfs4 nas.example:/replica rw\n'
        with self.assertRaises(ReplicaError): self.store(root=parent_alias/'objects',expected_mountpoint=parent_alias)

    def test_object_symlink_and_unsafe_permissions_do_not_read_target(self):
        store=self.store()
        target=self.mount/'private-fixture'; target.write_bytes(b'private fixture'); target.chmod(0o600)
        (self.root/self.ref).symlink_to(target)
        with self.assertRaises(ReplicaError): store.get(self.ref)
        self.assertEqual(target.read_bytes(),b'private fixture')
        (self.root/self.ref).unlink()
        os.mkfifo(self.root/self.ref,0o600)
        with self.assertRaises(ReplicaError): store.get(self.ref)
        (self.root/self.ref).unlink()
        self.root.chmod(0o755)
        with self.assertRaises(ReplicaError): store.get(self.ref)

    def test_mount_removed_after_read_never_returns_cached_bytes_or_local_absence(self):
        store=self.store(); store.put_if_absent(self.ref,b'stable')
        original=store._read
        def removed(fd,ref):
            result=original(fd,ref); self.mountinfo=self.base; return result
        with patch.object(store,'_read',side_effect=removed):
            with self.assertRaises(ReplicaError): store.get(self.ref)
        with self.assertRaises(ReplicaError): store.get(self.ref)

    def test_root_replacement_is_detected_with_held_directory_fd(self):
        store=self.store(); store.put_if_absent(self.ref,b'stable')
        original=store._read; moved=self.mount/'old-objects'
        def changed(fd,ref):
            result=original(fd,ref); self.root.rename(moved); self.root.mkdir(mode=0o700); return result
        with patch.object(store,'_read',side_effect=changed):
            with self.assertRaises(ReplicaError): store.get(self.ref)
        self.assertEqual((moved/self.ref).read_bytes(),b'stable')
        with self.assertRaises(ReplicaError): store.get(self.ref)

    def test_hardlink_and_directory_fsync_unsupported_fail_closed(self):
        store=self.store()
        with patch('codex_delivery.codex_delivery.encrypted_replica.os.link',side_effect=OSError(errno.ENOTSUP,'fixture')):
            with self.assertRaises(ObjectStoreUnsupported): store.put_if_absent(self.ref,b'ciphertext')
        self.assertEqual(list(self.root.iterdir()),[])
        with patch('codex_delivery.codex_delivery.encrypted_replica.os.fsync',side_effect=OSError(errno.EINVAL,'fixture')):
            with self.assertRaises(ObjectStoreUnsupported): store.put_if_absent(self.ref,b'ciphertext')
        self.assertEqual(list(self.root.iterdir()),[])

    def test_encrypted_adapter_uses_fixture_mount_and_recovers_exact_snapshot(self):
        # This exercises actual local fsync/link/read plus crypto, with a fake
        # mountinfo identity; it is explicitly not a production NAS proof.
        replica=EncryptedReplica(self.store(),FIXTURE_KEY,key_ref='fixture-key',failure_domain='test-independent',maxbytes=4096)
        plain=b'fixture SQLite bytes'; snapshot=Snapshot('EV-local-fixture',plain,digest(plain))
        receipt=replica.save(snapshot)
        self.assertNotIn(plain,(self.root/receipt.object_ref).read_bytes())
        self.assertEqual(replica.get(receipt),snapshot)


if __name__=='__main__': unittest.main()
