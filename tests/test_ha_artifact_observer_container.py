"""Opt-in artifact-only image checks; no network, host mounts or credentials."""
import os
import subprocess
import unittest

IMAGE=os.environ.get('OBSERVER_TEST_IMAGE','')
PLATFORM=os.environ.get('OBSERVER_TEST_PLATFORM','linux/amd64')

PROBE=r'''
import http.client
import time
for attempt in range(150):
    try:
        connection=http.client.HTTPConnection('127.0.0.1',8099,timeout=1)
        connection.request('GET','/healthz');response=connection.getresponse()
        assert response.status==200;response.read();connection.close();break
    except (OSError,http.client.HTTPException):time.sleep(.1)
else:raise AssertionError('health timeout')
'''

SCRIPT=r'''
import http.client
import json
from pathlib import Path
import subprocess
import sys
import time

root=Path('/data');root.mkdir(exist_ok=True)
options=root/'options.json';options.write_text('{}')
assert not Path('/var/run/docker.sock').exists()
assert not Path('/opt/ha-artifact-observer/codex_controller').exists()
assert not Path('/opt/ha-artifact-observer/codex_delivery').exists()
process=subprocess.Popen([sys.executable,'-B','-m','ha_artifact_observer.main'],
    stdout=subprocess.PIPE,stderr=subprocess.PIPE)
try:
    for attempt in range(100):
        assert process.poll() is None,'main exited'
        try:
            connection=http.client.HTTPConnection('127.0.0.1',8099,timeout=1)
            connection.request('GET','/healthz');response=connection.getresponse()
            assert response.status==200
            assert json.loads(response.read())=={'status':'ok','version':1,'observation_enabled':False}
            connection.close();break
        except (OSError,http.client.HTTPException):time.sleep(.03)
    else:raise AssertionError('main health timeout')
    for method,path in [('GET','/v1/artifacts/fixture'),
                        ('POST','/internal/v1/work-preview/qualification')]:
        connection=http.client.HTTPConnection('127.0.0.1',8099,timeout=1)
        connection.request(method,path);response=connection.getresponse()
        assert response.status==404;response.read();connection.close()
finally:
    process.terminate();stdout,stderr=process.communicate(timeout=3)
assert stdout==b'' and stderr==b''
options.write_text('{private-options-fixture')
result=subprocess.run([sys.executable,'-B','-m','ha_artifact_observer.main'],
    capture_output=True,timeout=3)
assert result.returncode==2
assert result.stdout==b'' and result.stderr==b'observer_json_invalid\n'
assert b'private-options-fixture' not in result.stdout+result.stderr
print('artifact-only-main-and-disabled-preview-passed')
'''


@unittest.skipUnless(IMAGE,'set OBSERVER_TEST_IMAGE to a built artifact-only image')
class ArtifactObserverContainerTests(unittest.TestCase):
    def test_actual_image_main_has_no_unfinished_dependencies(self):
        result=subprocess.run(['docker','run','--rm','--network','none','--platform',PLATFORM,
            '--entrypoint','python3','-i',IMAGE,'-B','-'],input=SCRIPT,text=True,
            capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('artifact-only-main-and-disabled-preview-passed',result.stdout)

    def test_actual_init_and_run_script_start_without_host_access(self):
        bootstrap="from pathlib import Path; import os; Path('/data').mkdir(exist_ok=True); Path('/data/options.json').write_text('{}'); os.execv('/init', ['/init', '/run.sh'])"
        result=subprocess.run(['docker','run','--detach','--network','none','--platform',PLATFORM,
            '--entrypoint','python3',IMAGE,'-B','-c',bootstrap],text=True,capture_output=True,
            timeout=15,check=True)
        container=result.stdout.strip()
        try:
            result=subprocess.run(['docker','exec',container,'python3','-B','-c',PROBE],
                text=True,capture_output=True,timeout=25)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result=subprocess.run(['docker','inspect','--format',
                '{{.HostConfig.NetworkMode}}|{{json .HostConfig.PortBindings}}|{{json .Mounts}}',container],
                text=True,capture_output=True,timeout=5,check=True)
            self.assertEqual(result.stdout.strip(),'none|{}|[]')
        finally:
            subprocess.run(['docker','rm','--force',container],capture_output=True,timeout=10,check=True)


if __name__=='__main__':unittest.main()
