"""Opt-in image composition: real crypto/worker, no network or user mounts."""
import base64
import json
import os
import subprocess
import unittest


@unittest.skipUnless(os.environ.get('CODEX_DELIVERY_TEST_IMAGES'), 'explicit candidate image(s) required')
class ContainerCompositionTests(unittest.TestCase):
    def test_native_parent_hard_limits_and_single_thread_start(self):
        script = r'''
import errno, json, mmap, os, pathlib, resource, sys, threading
sys.path.insert(0,'/opt/codex-delivery')
from codex_delivery.bootstrap import prepare_and_drop, prepare_resources
from codex_delivery.store import DeliveryError
from codex_work_runtime.qemu_work import QemuHostPolicy
prepare_and_drop()
policy=QemuHostPolicy(resource_profile='native-resource-v1')
cpu_before=resource.getrlimit(resource.RLIMIT_CPU)
if sys.argv[1]=='threaded':
    stop=threading.Event(); worker=threading.Thread(target=stop.wait); worker.start()
    try:
        try: prepare_resources(policy)
        except DeliveryError as error: assert error.code=='execution_resource_bootstrap_invalid'
        else: raise AssertionError('late resource setup accepted')
    finally: stop.set(); worker.join(2)
    print('{"late_setup_rejected":true}')
else:
    prepare_resources(policy)
    assert resource.getrlimit(resource.RLIMIT_AS)==(policy.native_max_address_bytes,)*2
    assert resource.getrlimit(resource.RLIMIT_NPROC)==(policy.native_max_threads,)*2
    assert resource.getrlimit(resource.RLIMIT_NOFILE)==(128,128)
    assert resource.getrlimit(resource.RLIMIT_CORE)==(0,0)
    assert resource.getrlimit(resource.RLIMIT_CPU)==cpu_before
    assert len(os.sched_getaffinity(0))==policy.native_cpu_count
    try: resource.setrlimit(resource.RLIMIT_AS,(policy.native_max_address_bytes+4096,)*2)
    except (ValueError,PermissionError): pass
    else: raise AssertionError('hard address limit raised')
    try: mmap.mmap(-1,policy.native_max_address_bytes+4096)
    except OSError as error: assert error.errno==errno.ENOMEM
    else: raise AssertionError('address overflow accepted')
    print('{"native_parent_bounded":true,"cumulative_cpu_unchanged":true}')
'''
        for image in os.environ['CODEX_DELIVERY_TEST_IMAGES'].split(','):
            for mode in ('new','threaded'):
                with self.subTest(image=image,mode=mode):
                    process=subprocess.run(['docker','run','--rm','--network','none','--read-only',
                        '--cap-drop','ALL','--cap-add','CHOWN','--cap-add','SETUID','--cap-add','SETGID',
                        '--cap-add','SETPCAP','--security-opt','no-new-privileges','--memory','256m',
                        '--pids-limit','128','--tmpfs','/data:rw,noexec,nosuid,nodev,size=16m,mode=0755',
                        '--entrypoint','python3',image,'-I','-B','-c',script,mode],
                        capture_output=True,text=True,timeout=30)
                    self.assertEqual(0,process.returncode,process.stderr[-2000:])
                    expected=({'late_setup_rejected':True} if mode=='threaded' else
                              {'native_parent_bounded':True,'cumulative_cpu_unchanged':True})
                    self.assertEqual(expected,json.loads(process.stdout))

    def test_fixed_bootstrap_drops_irreversibly_and_preserves_existing_root_data(self):
        script = r'''
import ctypes, json, os, pathlib, sys
sys.path.insert(0,'/opt/codex-delivery')
from codex_delivery.bootstrap import prepare_and_drop
from codex_delivery.store import DeliveryError
state=pathlib.Path('/data/state')
if sys.argv[1]=='existing':
    state.mkdir(mode=0o700)
    (state/'preserve.txt').write_text('synthetic existing data')
    try: prepare_and_drop()
    except DeliveryError as error: assert error.code=='execution_bootstrap_invalid'
    else: raise AssertionError('existing root-owned state was silently migrated')
    assert os.getuid()==0 and (state/'preserve.txt').read_text()=='synthetic existing data'
    print('{"existing_preserved":true}')
else:
    options=pathlib.Path('/data/options.json')
    options.write_text('synthetic private settings'); options.chmod(0o600)
    assert prepare_and_drop()==state
    assert os.getresuid()==(65534,65534,65534) and os.getgroups()==[]
    assert ctypes.CDLL(None).prctl(3,0,0,0,0)==0
    try: os.setuid(0)
    except PermissionError: pass
    else: raise AssertionError('uid restored')
    try: options.read_bytes()
    except PermissionError: pass
    else: raise AssertionError('private options became readable')
    (state/'checkpoint.txt').write_text('synthetic checkpoint')
    assert (state/'checkpoint.txt').stat().st_mode&0o777==0o600
    print('{"uid":65534,"privileges_dropped":true}')
'''
        for image in os.environ['CODEX_DELIVERY_TEST_IMAGES'].split(','):
            for mode in ('new', 'existing'):
                with self.subTest(image=image, mode=mode):
                    process = subprocess.run(['docker', 'run', '--rm', '--network', 'none',
                        '--read-only', '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'SETUID', '--cap-add', 'SETGID',
                        '--cap-add', 'SETPCAP', '--security-opt', 'no-new-privileges',
                        '--memory', '256m', '--pids-limit', '32',
                        '--tmpfs', '/data:rw,noexec,nosuid,nodev,size=16m,mode=0755',
                        '--entrypoint', 'python3', image, '-I', '-B', '-c', script, mode],
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(process.returncode, 0, process.stderr[-2000:])
                    expected = {'existing_preserved': True} if mode == 'existing' else {'uid': 65534, 'privileges_dropped': True}
                    self.assertEqual(expected, json.loads(process.stdout))

    def test_deployed_package_crypto_worker_and_fail_closed_mount_qualification(self):
        script = r'''
import json, pathlib, sys, urllib.request
sys.path.insert(0,'/opt/codex-delivery')
from codex_delivery.service import ServiceConfig, DeliveryService
options=pathlib.Path('/data/options.json')
options.write_text(sys.stdin.read()); options.chmod(0o600)
config=ServiceConfig.load(options)
service=DeliveryService(config,pathlib.Path('/data/state'),address=('127.0.0.1',0))
service.start()
try:
    url='http://127.0.0.1:'+str(service.server.server_address[1])+'/healthz'
    health=json.loads(urllib.request.urlopen(url,timeout=2).read())
    assert health['replica_state']=='unqualified' and not health['replica_qualified']
    assert not health['execution_enabled']
    result=service.store.create_project('fixture-owner','fixture-project',project_alias='fixture',name='Fixture')
    assert result['status']=='durability_pending'
    assert service.health()['replica_state']=='fused'
    assert service.store.reconcile(result['event_id'])['status']=='durability_pending'
    assert service.health()['replica_state']=='fused'
    assert not service.store.replica.status()['may_write']
    print(json.dumps({'composition_valid':True,'execution_enabled':False,'replica_state':'fused'}))
finally:
    service.close()
'''
        options = {'extension_bearer': 'fixture-provider-' + 'a'*32,
            'origin_key': 'fixture-origin-' + 'b'*32, 'allowed_actors': ['actor:' + 'c'*64],
            'replica_domain': 'independent', 'worker_enabled': False,
            'replica': {'enabled': True, 'root': '/mnt/fixture-share/objects', 'fstype': 'nfs4',
                'source': 'fixture.invalid:/export', 'mountpoint': '/mnt/fixture-share',
                'key_ref': 'fixture-replica-key', 'key': base64.b64encode(b'fixture-'*4).decode(),
                'timeout_seconds': 5}}
        for image in os.environ['CODEX_DELIVERY_TEST_IMAGES'].split(','):
            with self.subTest(image=image):
                process = subprocess.run(['docker', 'run', '--rm', '-i', '--network', 'none',
                    '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--memory', '256m', '--pids-limit', '32',
                    '--tmpfs', '/data:rw,noexec,nosuid,nodev,size=16m,mode=0700',
                    '--entrypoint', 'python3', image, '-I', '-B', '-c', script],
                    input=json.dumps(options), capture_output=True, text=True, timeout=30)
                self.assertEqual(process.returncode, 0, process.stderr[-2000:])
                self.assertEqual(json.loads(process.stdout),
                    {'composition_valid': True, 'execution_enabled': False, 'replica_state': 'fused'})


if __name__ == '__main__':
    unittest.main()
