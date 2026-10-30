"""Real exec/IPC/deadline tests, with an explicit local fake mount worker.

The fixtures never qualify a real NFS/CIFS mount or claim NAS durability.
"""
from dataclasses import asdict, replace
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

from codex_delivery.codex_delivery import bounded_replica as module
from codex_delivery.codex_delivery.bounded_replica import BoundedReplicaError, DeadlineReplica, MountSpec
from codex_delivery.codex_delivery.durability import Snapshot, digest
from codex_delivery.codex_delivery.encrypted_replica import EncryptedReplica, OVERHEAD, ReplicaError


# Explicit nonproduction fixture key, never a real credential.
KEY = b'fixture-bound-key-XXXXXXXXXXXXXX'
assert len(KEY) == 32


WORKER = r'''
import json,os,resource,sys,time
from pathlib import Path
sys.path.insert(0, REPOSITORY)
from codex_delivery.codex_delivery import bounded_replica as b

class FakeMount:
    def __init__(self,root,**options):
        self.root=Path(root)
        self.config=json.loads((self.root/'behavior.json').read_text())
        info=self.root.stat()
        identity=(self.config.get('mount_id',42),1,os.major(info.st_dev),os.minor(info.st_dev),
                  '/',options['expected_mountpoint'],options['expected_fstype'],options['expected_source'],'rw','rw')
        self._pinned=(identity,(info.st_dev,info.st_ino))
        (self.root/'environment.json').write_text(json.dumps(dict(
            environment=dict(os.environ),argv=sys.argv,core=list(resource.getrlimit(resource.RLIMIT_CORE)),pid=os.getpid())))
        self.audit('open')
        time.sleep(self.config.get('open_delay',0))
    def audit(self,op):
        journal=json.loads(Path(JOURNAL).read_text())
        with (self.root/'audit.jsonl').open('a') as output:
            output.write(json.dumps(dict(op=op,pin=journal['pin'] is not None,
                                        may_write=journal['may_write'],pid=os.getpid()))+'\n')
    def get(self,ref):
        self.audit('get')
        time.sleep(self.config.get('get_delay',0))
        try: return (self.root/ref).read_bytes()
        except FileNotFoundError: return None
    def put_if_absent(self,ref,content):
        self.audit('put')
        time.sleep(self.config.get('put_delay',0))
        if self.config.get('put_unsupported'): return None
        try: fd=os.open(self.root/ref,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        except FileExistsError: return False
        with os.fdopen(fd,'wb') as output:
            output.write(content);output.flush();os.fsync(output.fileno())
        return True

b.NetworkMountObjectStore=FakeMount
raise SystemExit(b._worker_main(int(sys.argv[-1])))
'''


class BoundedReplicaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.network = self.root / 'network-fixture'
        self.network.mkdir(mode=0o700)
        self.local = self.root / 'local-journal'
        self.local.mkdir(mode=0o700)
        self.journal = self.local / 'replica.json'
        self.worker = self.root / 'fixture-worker.py'
        repository = str(Path(module.__file__).resolve().parents[2])
        self.worker.write_text('REPOSITORY=' + repr(repository) + '\nJOURNAL=' + repr(str(self.journal)) + '\n' + WORKER)
        self.worker.chmod(0o600)
        self.mount = MountSpec(str(self.network), 'nfs4', 'nas.example:/fixture', str(self.network), 65536)
        self.snapshot = Snapshot('EV-bounded-fixture', b'private plaintext must remain in parent',
                                 digest(b'private plaintext must remain in parent'))
        self.processes = []
        self.wire = []
        self.behavior()
        original_rpc = module._Session.rpc
        def rpc(session, request, payload=b''):
            self.wire.append((dict(request), payload))
            return original_rpc(session, request, payload)
        self.rpc_patch = patch.object(module._Session, 'rpc', rpc)
        self.rpc_patch.start()
        self.addCleanup(self.rpc_patch.stop)
        self.launch_patch = patch.object(DeadlineReplica, '_launch', self.launch)
        self.launch_patch.start()
        self.addCleanup(self.launch_patch.stop)
        self.addCleanup(self.reap)

    def behavior(self, **config):
        (self.network / 'behavior.json').write_text(json.dumps(config))

    def launch(self, lockfd):
        process = subprocess.Popen([sys.executable, '-I', '-B', str(self.worker), str(lockfd)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, close_fds=True,
            pass_fds=(lockfd,), bufsize=0, start_new_session=True)
        self.processes.append(process)
        return process

    def reap(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()

    def replica(self, **changes):
        return DeadlineReplica(changes.pop('mountspec', self.mount), changes.pop('key', KEY),
            **{'key_ref': 'fixture-key-v1', 'failure_domain': 'fixture-independent',  # gitleaks:allow -- synthetic test key reference
               'journal_path': self.journal, 'timeout_seconds': 2, 'maxbytes': 8192, **changes})

    def audit(self):
        path = self.network / 'audit.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_real_exec_encrypt_read_restore_and_pin_before_object_io(self):
        replica = self.replica()
        self.assertEqual(replica.status()['state'], 'unqualified')
        receipt = replica.save(self.snapshot)
        self.assertEqual(replica.get(receipt), self.snapshot)
        audit = self.audit()
        self.assertEqual([item['op'] for item in audit], ['open', 'get', 'put', 'get', 'open', 'get'])
        self.assertTrue(all(item['pin'] for item in audit if item['op'] != 'open'))
        self.assertTrue(all(item['may_write'] for item in audit if item['op'] == 'put'))
        self.assertEqual(replica.status()['state'], 'ready')
        self.assertFalse(replica.status()['worker_active'])
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)
        for path in self.local.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.snapshot.content, self.journal.read_bytes())
        self.assertNotIn(KEY, self.journal.read_bytes())
        self.assertNotIn(self.snapshot.content, (self.network / receipt.object_ref).read_bytes())

    def test_exact_ciphertext_digest_authenticates_bounded_read_without_put(self):
        replica=self.replica();receipt=replica.save(self.snapshot)
        before=sum(item['op']=='put' for item in self.audit())
        expected=digest((self.network/receipt.object_ref).read_bytes())
        self.assertEqual(replica.ciphertext_digest(receipt),expected)
        self.assertEqual(sum(item['op']=='put' for item in self.audit()),before)
        forged=replace(receipt,sha256='sha256:'+'0'*64)
        with self.assertRaisesRegex(ReplicaError,'replica_receipt_invalid'):replica.ciphertext_digest(forged)
        content=(self.network/receipt.object_ref).read_bytes()
        (self.network/receipt.object_ref).write_bytes(content[:-1]+bytes([content[-1]^1]))
        with self.assertRaisesRegex(ReplicaError,'replica_envelope_invalid'):replica.ciphertext_digest(receipt)

    def test_ciphertext_digest_cannot_clear_unknown_journal_or_grant_a_write(self):
        replica=self.replica(timeout_seconds=0.15);receipt=replica.save(self.snapshot)
        self.behavior(get_delay=1)
        with self.assertRaises(ReplicaError):replica.get(receipt)
        before=len(self.processes);puts=sum(item['op']=='put' for item in self.audit())
        with self.assertRaises(ReplicaError):replica.ciphertext_digest(receipt)
        self.assertEqual(len(self.processes),before)
        self.assertEqual(sum(item['op']=='put' for item in self.audit()),puts)
        self.assertNotEqual(replica.status()['state'],'ready')

    def test_child_receives_no_key_plaintext_or_environment_credentials(self):
        # Exercise the production launcher/env/argv, with only the trusted
        # entry path redirected to this explicit local fake-mount fixture.
        self.launch_patch.stop()
        original_popen = subprocess.Popen
        launched = []
        def observe(*args, **kwargs):
            launched.append((args, kwargs))
            process = original_popen(*args, **kwargs)
            self.processes.append(process)
            return process
        poison = self.root / 'poison'
        poison.mkdir()
        marker = self.root / 'imported-user-code'
        (poison / 'sitecustomize.py').write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ").touch()\n")
        secret = 'fixture-machine-secret-must-not-be-inherited'
        with patch.dict(os.environ, {'MACHINE_TOKEN': secret, 'PYTHONPATH': str(poison),
                                     'PYTHONSTARTUP': str(poison / 'sitecustomize.py')}), \
                patch.object(module, '__file__', str(self.worker)), \
                patch.object(module.subprocess, 'Popen', observe):
            replica = self.replica()
            replica.save(self.snapshot)
        self.assertEqual(launched[0][1]['env'], {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        self.assertEqual(launched[0][0][0][1:3], ['-I', '-B'])
        self.assertTrue(launched[0][1]['close_fds'])
        self.assertTrue(launched[0][1]['start_new_session'])
        self.assertEqual(launched[0][1]['stderr'], subprocess.DEVNULL)
        observed = json.loads((self.network / 'environment.json').read_text())
        # Darwin/Python can synthesize these locale-only values after exec.
        self.assertLessEqual(set(observed['environment']),
                             {'PATH', 'LANG', 'LC_CTYPE', '__CF_USER_TEXT_ENCODING'})
        self.assertEqual(observed['environment']['PATH'], '/usr/bin:/bin')
        self.assertEqual(observed['environment']['LANG'], 'C.UTF-8')
        self.assertEqual(observed['core'], [0, 0])
        self.assertNotEqual(observed['pid'], os.getpid())
        self.assertFalse(marker.exists())
        wire = b''.join(module._canonical(request) + payload for request, payload in self.wire)
        self.assertNotIn(KEY, wire)
        self.assertNotIn(self.snapshot.content, wire)
        self.assertNotIn(secret.encode(), wire)
        self.assertNotIn(KEY.decode(), json.dumps(observed))

    def test_total_budget_shared_by_startup_get_cas_and_readback(self):
        self.behavior(open_delay=0.05, get_delay=0.08, put_delay=0.08)
        replica = self.replica(timeout_seconds=0.28)
        started = time.monotonic()
        with self.assertRaises(BoundedReplicaError) as error:
            replica.save(self.snapshot)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.55)
        self.assertEqual(error.exception.reason, 'replica_mount_timeout')
        self.assertEqual(replica.status()['state'], 'fused')
        self.assertEqual(len(self.processes), 1)

    def test_open_timeout_before_qualification_no_pin_no_put(self):
        self.behavior(open_delay=0.6)
        replica = self.replica(timeout_seconds=0.2)
        with self.assertRaises(BoundedReplicaError) as error:
            replica.save(self.snapshot)
        self.assertEqual(error.exception.code, 'replica_mount_timeout')
        state = replica.status()
        self.assertFalse(state['qualified'])
        self.assertFalse(state['may_write'])
        self.assertEqual([row['op'] for row in self.audit()], ['open'])

    def test_unkillable_late_writer_keeps_lock_across_restart_and_reconciles_only_get(self):
        self.behavior(put_delay=0.6)
        replica = self.replica(timeout_seconds=0.25)
        # Simulate an OS worker that has not exited after both kill requests.
        # The real subprocess is allowed to complete its late write, holding flock.
        requests = []
        with patch.object(DeadlineReplica, '_request_stop', lambda _, p: requests.append(p.pid)):
            with self.assertRaises(BoundedReplicaError) as error:
                replica.save(self.snapshot)
            self.assertEqual(error.exception.code, 'replica_write_unknown')
            self.assertEqual(error.exception.reason, 'replica_mount_timeout')
            ref = EncryptedReplica.object_ref(self.snapshot.event_id, self.snapshot.sha256)
            state = replica.status()
            self.assertEqual(state['object_ref'], ref)
            self.assertTrue(state['worker_active'])
            self.assertEqual(len(requests), 1)
            restarted = self.replica()
            with self.assertRaises(BoundedReplicaError):
                restarted.save(self.snapshot)
            with self.assertRaises(BoundedReplicaError) as unsettled:
                restarted.reconcile(self.snapshot)
            self.assertEqual(unsettled.exception.code, 'replica_worker_unsettled')
            self.assertEqual(len(self.processes), 1)
            self.processes[0].wait(timeout=2)
        self.behavior()
        receipt = restarted.reconcile(self.snapshot)
        self.assertEqual(receipt.object_ref, ref)
        self.assertEqual([row['op'] for row in self.audit()].count('put'), 1)
        self.assertEqual(restarted.status()['state'], 'ready')

    def test_killed_unknown_missing_stays_fused_and_never_puts_on_restart(self):
        self.behavior(put_delay=0.8)
        replica = self.replica(timeout_seconds=0.25)
        with self.assertRaises(BoundedReplicaError):
            replica.save(self.snapshot)
        self.processes[0].wait(timeout=2)
        self.behavior()
        restarted = self.replica()
        with self.assertRaises(BoundedReplicaError) as missing:
            restarted.reconcile(self.snapshot)
        self.assertEqual(missing.exception.code, 'replica_write_unknown')
        self.assertEqual(missing.exception.reason, 'replica_object_missing')
        with self.assertRaises(BoundedReplicaError):
            restarted.save(self.snapshot)
        self.assertEqual([row['op'] for row in self.audit()].count('put'), 1)
        self.assertEqual(restarted.status()['state'], 'fused')

    def test_restart_mount_replacement_never_requalifies_and_never_puts(self):
        replica = self.replica()
        replica.save(self.snapshot)
        before = json.loads(self.journal.read_text())['pin']
        self.behavior(mount_id=43)
        restarted = self.replica()
        other = replace(self.snapshot, event_id='EV-other')
        with self.assertRaises(BoundedReplicaError) as error:
            restarted.save(other)
        self.assertEqual(error.exception.reason, 'replica_mount_identity_mismatch')
        self.assertEqual(json.loads(self.journal.read_text())['pin'], before)
        self.assertEqual([row['op'] for row in self.audit()].count('put'), 1)

    def test_get_timeout_circuit_and_exact_reconcile_no_write(self):
        replica = self.replica()
        receipt = replica.save(self.snapshot)
        self.behavior(get_delay=0.6)
        replica = self.replica(timeout_seconds=0.2)
        with self.assertRaises(BoundedReplicaError) as error:
            replica.get(receipt)
        self.assertEqual(error.exception.code, 'replica_mount_timeout')
        self.assertFalse(replica.status()['may_write'])
        self.processes[-1].wait(timeout=2)
        self.behavior()
        restarted = self.replica()
        with self.assertRaises(BoundedReplicaError):
            restarted.reconcile(replace(self.snapshot, event_id='EV-wrong'))
        self.assertEqual(restarted.reconcile(self.snapshot), receipt)
        self.assertEqual([row['op'] for row in self.audit()].count('put'), 1)

    def test_existing_corruption_is_fused_without_rewrite(self):
        replica = self.replica()
        receipt = replica.save(self.snapshot)
        (self.network / receipt.object_ref).write_bytes(b'damaged existing object')
        with self.assertRaises(BoundedReplicaError):
            replica.save(self.snapshot)
        with self.assertRaises(BoundedReplicaError):
            self.replica().reconcile(self.snapshot)
        self.assertEqual([row['op'] for row in self.audit()].count('put'), 1)

    def test_inflight_journal_survives_parent_crash_and_cannot_issue_put(self):
        replica = self.replica()
        receipt = replica.save(self.snapshot)
        state = json.loads(self.journal.read_text())
        state.update(state='inflight', object_ref=receipt.object_ref, may_write=True)
        self.journal.write_bytes(module._canonical(state))
        restarted = self.replica()
        self.assertEqual(restarted.status()['code'], 'replica_write_unknown')
        self.assertEqual(restarted.status()['reason'], 'replica_parent_interrupted')
        self.assertEqual(restarted.reconcile(self.snapshot), receipt)
        self.assertEqual([row['op'] for row in self.audit()].count('put'), 1)

    def test_real_parent_process_crash_leaves_orphan_lock_and_durable_unknown(self):
        self.replica()  # Provision only the local private journal.
        self.behavior(put_delay=0.65)
        driver = self.root / 'fixture-parent.py'
        repository = str(Path(module.__file__).resolve().parents[2])
        script = '''
import subprocess,sys
sys.path.insert(0,REPOSITORY)
from codex_delivery.codex_delivery import bounded_replica as b
from codex_delivery.codex_delivery.durability import Snapshot,digest
def launch(self,fd):
    return subprocess.Popen([sys.executable,'-I','-B',WORKER,str(fd)],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
        close_fds=True,pass_fds=(fd,),bufsize=0,start_new_session=True)
b.DeadlineReplica._launch=launch
replica=b.DeadlineReplica(b.MountSpec(**MOUNT),KEY,key_ref='fixture-key-v1',  # gitleaks:allow -- synthetic test key reference
    failure_domain='fixture-independent',journal_path=JOURNAL,timeout_seconds=3,maxbytes=8192)
content=b'private plaintext must remain in parent'
replica.save(Snapshot('EV-bounded-fixture',content,digest(content)))
'''
        constants = dict(REPOSITORY=repository, WORKER=str(self.worker), MOUNT=asdict(self.mount),
                         KEY=KEY, JOURNAL=str(self.journal))
        driver.write_text('\n'.join(name + '=' + repr(value) for name, value in constants.items()) + '\n' + script)
        driver.chmod(0o600)
        parent = subprocess.Popen([sys.executable, '-I', '-B', str(driver)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        self.processes.append(parent)
        deadline = time.monotonic() + 2
        while not any(row['op'] == 'put' for row in self.audit()) and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(any(row['op'] == 'put' for row in self.audit()))
        parent.kill()
        parent.wait(timeout=2)
        restarted = self.replica()
        self.assertEqual(restarted.status()['code'], 'replica_write_unknown')
        self.assertTrue(restarted.status()['worker_active'])
        with self.assertRaises(BoundedReplicaError) as unsettled:
            restarted.reconcile(self.snapshot)
        self.assertEqual(unsettled.exception.code, 'replica_worker_unsettled')
        deadline = time.monotonic() + 2
        while restarted.status()['worker_active'] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(restarted.status()['worker_active'])
        self.behavior()
        restarted.reconcile(self.snapshot)
        self.assertEqual([row['op'] for row in self.audit()].count('put'), 1)

    def test_invalid_put_response_never_becomes_receipt_via_cached_read(self):
        replica = self.replica()
        original = module._Session.rpc
        def malformed(session, request, payload=b''):
            response, content = original(session, request, payload)
            if request['op'] == 'put':
                response['created'] = 1  # bool-only protocol, even after actual write.
            return response, content
        with patch.object(module._Session, 'rpc', malformed):
            with self.assertRaises(BoundedReplicaError) as error:
                replica.save(self.snapshot)
        self.assertEqual(error.exception.code, 'replica_write_unknown')
        self.assertEqual(error.exception.reason, 'replica_worker_protocol_invalid')
        self.assertEqual([row['op'] for row in self.audit()], ['open', 'get', 'put'])
        self.processes[-1].wait(timeout=2)
        replica.reconcile(self.snapshot)
        self.assertEqual([row['op'] for row in self.audit()].count('put'), 1)

    def test_oversized_protocol_frame_fails_without_mount_access(self):
        replica = self.replica()
        hostile = self.root / 'malformed-frame.py'
        hostile.write_text("import os\nos.write(1,b'\\xff\\xff\\xff\\xff')\n")
        def launch(lockfd):
            process = subprocess.Popen([sys.executable, '-I', '-B', str(hostile)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, close_fds=True,
                pass_fds=(lockfd,), bufsize=0)
            self.processes.append(process)
            return process
        with patch.object(DeadlineReplica, '_launch', lambda _, fd: launch(fd)):
            with self.assertRaises(BoundedReplicaError) as error:
                replica.save(self.snapshot)
        self.assertEqual(error.exception.reason, 'replica_worker_protocol_invalid')
        self.assertFalse(replica.status()['qualified'])
        self.assertEqual(self.audit(), [])

    def test_corrupt_deleted_rebound_and_duplicate_journals_fail_closed(self):
        replica = self.replica()
        original = self.journal.read_bytes()
        for content in [b'not-json', original.replace(b'"version":1', b'"version":1,"version":1'),
                        original.replace(b'"version":1', b'"version":NaN'),
                        original.replace(b'"version":1', b'"version":true')]:
            self.journal.write_bytes(content)
            with self.assertRaises(BoundedReplicaError):
                self.replica()
        self.journal.write_bytes(original)
        with self.assertRaises(BoundedReplicaError):
            self.replica(mountspec=replace(self.mount, expected_source='other.example:/fixture'))
        self.journal.unlink()
        with self.assertRaises(BoundedReplicaError) as error:
            self.replica()
        self.assertEqual(error.exception.code, 'replica_journal_missing')
        self.assertEqual(self.processes, [])

    def test_bad_config_credentials_permissions_symlinks_and_validation_never_start_worker(self):
        for source in ['user@nas.example:/fixture', 'https://nas.example/fixture',
                       'nas.example:/fixture?token=fixture', '//user:fixture@nas.example/share',
                       'nas.example:/password=fixture']:
            with self.assertRaises(ReplicaError):
                replace(self.mount, expected_source=source)
        for timeout in [float('nan'), float('inf'), True, 0, 301]:
            with self.assertRaises(ReplicaError):
                self.replica(timeout_seconds=timeout)
        with self.assertRaises(ReplicaError):
            self.replica(journal_path=self.network / 'forbidden.json')
        self.local.chmod(0o755)
        with self.assertRaises(ReplicaError):
            self.replica()
        self.local.chmod(0o700)
        symlink = self.root / 'local-link'
        symlink.symlink_to(self.local, target_is_directory=True)
        with self.assertRaises(ReplicaError):
            self.replica(journal_path=symlink / 'replica.json')
        replica = self.replica()
        with self.assertRaises(ReplicaError):
            replica.save(replace(self.snapshot, content=b'bad digest'))
        self.assertEqual(self.processes, [])

    def test_thread_and_cross_instance_concurrency_never_starts_second_worker(self):
        self.behavior(open_delay=0.35)
        first = self.replica()
        second = self.replica()
        errors = []
        thread = threading.Thread(target=lambda: self.thread_save(first, errors))
        thread.start()
        deadline = time.monotonic() + 2
        while not self.processes and time.monotonic() < deadline:
            time.sleep(0.005)
        with self.assertRaises(BoundedReplicaError):
            first.save(self.snapshot)
        with self.assertRaises(BoundedReplicaError):
            second.save(self.snapshot)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.processes), 1)

    def thread_save(self, replica, errors):
        try:
            replica.save(self.snapshot)
        except BaseException as error:
            errors.append(error)

    def test_production_worker_local_directory_rejected_parent_never_opens_mount(self):
        self.launch_patch.stop()
        replica = self.replica()
        real_open = os.open
        target = str(self.network)
        def parent_open(path, flags, *args, **kwargs):
            if str(path).startswith(target):
                raise AssertionError('parent touched network mount')
            return real_open(path, flags, *args, **kwargs)
        with patch.object(module.os, 'open', parent_open), \
                patch.object(module, 'NetworkMountObjectStore', side_effect=AssertionError('parent qualification')):
            with self.assertRaises(BoundedReplicaError) as error:
                replica.save(self.snapshot)
        self.assertIn(error.exception.reason, {'replica_mount_unavailable', 'replica_mount_identity_mismatch'})
        self.assertFalse(replica.status()['qualified'])
        self.assertEqual(self.audit(), [])

    def test_fixed_exec_entry_works_in_deployed_single_package_without_pythonpath(self):
        self.launch_patch.stop()
        source = Path(module.__file__).parent
        packaged = self.root / 'deployed' / 'codex_delivery'
        packaged.mkdir(parents=True)
        for filename in ('bounded_replica.py', 'encrypted_replica.py', 'durability.py'):
            shutil.copyfile(source / filename, packaged / filename)
        # The isolated worker must not need __init__, PYTHONPATH, or the outer
        # repository's nested codex_delivery.codex_delivery import layout.
        replica = self.replica()
        with patch.object(module, '__file__', str(packaged / 'bounded_replica.py')):
            with self.assertRaises(BoundedReplicaError) as error:
                replica.save(self.snapshot)
        self.assertIn(error.exception.reason, {'replica_mount_unavailable', 'replica_mount_identity_mismatch'})
        self.assertFalse(replica.status()['qualified'])


if __name__ == '__main__':
    unittest.main()
