"""Disposable Docker Desktop pull proof; never targets HA or runs the image.

Run from the Add-on repository with PYTHONPATH=.:tests. The approved candidate
image must already have been built locally. All grants/tokens/data are fixtures.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import os
import sys


IMAGE='codex-local/ha-artifact-registry:review-20261029'
TARGET='127.0.0.1:5079/codex-work/amd64-canary:0.1.0'
NAME='codex-registry-fixture-'+str(os.getpid())
TLS='--loopback-tls' in sys.argv[1:]
DOCKER=['docker','--context','desktop-linux']


def run(*args,check=True,timeout=30):
    return subprocess.run([*DOCKER,*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          check=check,timeout=timeout)


def main():
    attempted=False;fixture=None;stage='local_context'
    try:
        context=json.loads(run('context','inspect','desktop-linux').stdout)[0]
        if not context['Endpoints']['docker']['Host'].startswith('unix://'):raise RuntimeError('nonlocal context')
        if run('image','inspect',TARGET,check=False).returncode==0:raise RuntimeError('target already exists')
        if run('container','inspect',NAME,check=False).returncode==0:raise RuntimeError('fixture name already exists')
        run('image','inspect',IMAGE)
        stage='source_oci_fixture'
        from test_ha_artifact_registry import RegistryTests
        RegistryTests.setUpClass();fixture=RegistryTests();fixture.setUp()
        with tempfile.TemporaryDirectory(prefix='codex-local-registry-') as temporary:
            root=Path(temporary).resolve();root.chmod(0o700)
            policy={'public_key':list(fixture.public),'binding':fixture.binding.__dict__,'grant':fixture.envelope,
                    'writer':fixture.writer,'reader':fixture.reader}
            for name,raw in (('policy.json',json.dumps(policy).encode()),('artifact.tar',fixture.artifact)):
                path=root/name;path.write_bytes(raw);path.chmod(0o600)
            program='''
import json,time
from pathlib import Path
from ha_artifact_registry.service import Binding,RegistryStore
from ha_artifact_registry.http import make_server
p=json.loads(Path('/fixture/policy.json').read_bytes())
s=RegistryStore('/data/registry',public_key=bytes(p['public_key']),bindings=(Binding(**p['binding']),),
grants=(p['grant'],),writer_token=p['writer'],reader_token=p['reader'],enabled=True)
s.publish('grant-B',Path('/fixture/artifact.tar').read_bytes(),'Bearer '+p['writer'],deadline=time.monotonic()+30)
from ha_artifact_registry.tls import ephemeral_context
context=ephemeral_context(Path('/data')) if __TLS__ else None
server=make_server(('0.0.0.0',5079),s,host_authority='127.0.0.1:5079',tls_context=context)
print('fixture-ready',flush=True)
server.serve_forever()
'''
            program=program.replace('__TLS__',repr(TLS))
            stage='isolated_registry_start'
            # No host networking, Docker socket, capabilities or writable host
            # mount. Distribution is mapped only to Engine/Desktop loopback.
            attempted=True
            run('run','--detach','--name',NAME,'--read-only','--cap-drop','ALL',
                '--security-opt','no-new-privileges','--publish','127.0.0.1:5079:5079',
                '--tmpfs','/data:rw,nosuid,nodev,noexec,mode=0700,size=8m',
                '--mount','type=bind,src='+str(root)+',dst=/fixture,readonly',
                '--entrypoint','python3',IMAGE,'-c',program)
            limit=time.monotonic()+15
            while time.monotonic()<limit:
                if b'fixture-ready' in run('logs',NAME).stdout:break
                time.sleep(.2)
            else:raise RuntimeError('registry not ready')
            stage='engine_pull'
            # Pull only. The deliberately synthetic base is never executed.
            result=run('pull','--platform','linux/amd64',TARGET,check=False,timeout=45)
            if result.returncode:
                print(json.dumps({'fixture_engine_error':result.stderr.decode(errors='replace')[-1200:]}))
                raise RuntimeError('pull rejected')
            stage='independent_engine_inspect'
            actual=json.loads(run('image','inspect',TARGET).stdout)[0]
            expected='127.0.0.1:5079/codex-work/amd64-canary@'+fixture.grant['manifest_digest']
            if expected not in actual.get('RepoDigests',[]):raise RuntimeError('repository digest mismatch')
            if actual.get('Architecture')!='amd64' or actual.get('Os')!='linux':raise RuntimeError('platform mismatch')
            print(json.dumps({'status':'verified_local_engine_pull_only','docker_image_id':actual['Id'],
                'repository_digest':expected,'source_request_digest':fixture.build_evidence.request_digest,
                'transport':'ephemeral_loopback_tls' if TLS else 'http',
                'runtime_or_supervisor_qualified':False},sort_keys=True))
            return 0
    except Exception:
        print(json.dumps({'status':'not_verified','stage':stage},sort_keys=True));return 1
    finally:
        if attempted:run('rm','--force',NAME,check=False)
        # Reconcile this exact fixture target even if pull timed out. It was
        # proven absent before this run; no arbitrary image cleanup is allowed.
        if attempted and run('image','inspect',TARGET,check=False).returncode==0:
            run('image','rm',TARGET,check=False)
        if fixture is not None:
            fixture.doCleanups()
            type(fixture).doClassCleanups()


if __name__=='__main__':raise SystemExit(main())
