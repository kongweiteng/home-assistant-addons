"""Fixed build inputs and opt-in isolated image smoke tests; no HA acceptance."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / 'ha_artifact_registry'
MODULES = {'__init__', 'image', 'service', 'http', 'main', 'tls'}
BASES = {
    'amd64': 'ghcr.io/home-assistant/amd64-base-debian@sha256:84023298f975360b4350fe76118a1a5b117ff8f7ca5249d6398381659051aba3',
    'arm64': 'ghcr.io/home-assistant/aarch64-base-debian@sha256:4ecdb87bbf2e24c220140aedefdc521f7e3c499bc116c886f432261dc247d2f8',
}


class RegistryPackagingTests(unittest.TestCase):
    def test_Supervisor_fallback_cannot_override_fixed_architecture_bases(self):
        self.assertFalse((ADDON / 'build.yaml').exists())
        instructions = [line.strip() for line in (ADDON / 'Dockerfile').read_text().splitlines()
                        if line.strip() and not line.lstrip().startswith('#')]
        self.assertFalse(any('BUILD_FROM' in line for line in instructions))
        stages = {}
        for line in instructions:
            match = re.fullmatch(r'FROM (\S+) AS base-(\w+)', line)
            if match:
                stages[match[2]] = match[1]
        self.assertEqual(stages, BASES)
        self.assertIn('ARG TARGETARCH', instructions)
        self.assertEqual([line for line in instructions if line.startswith('FROM ')][-1],
                         'FROM base-${TARGETARCH} AS final')

    def test_production_package_is_self_contained_and_copy_is_exact(self):
        package = ADDON / 'ha_artifact_registry'
        self.assertEqual({path.stem for path in package.glob('*.py')}, MODULES)
        allowed = {'cryptography'} | __import__('sys').stdlib_module_names
        for path in package.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    self.assertTrue(all(alias.name.split('.')[0] in allowed for alias in node.names), path)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        self.assertEqual(node.level, 1, path)
                        self.assertIn(node.module, MODULES, path)
                    else:
                        self.assertIn(node.module.split('.')[0], allowed, path)
        copies = [line.split()[1:] for line in (ADDON / 'Dockerfile').read_text().splitlines()
                  if line.startswith('COPY ')]
        self.assertEqual(copies, [
            ['ha_artifact_registry/' + name + '.py' for name in
             ('__init__', 'image', 'service', 'http', 'main', 'tls')]
            + ['/opt/registry/ha_artifact_registry/'], ['run.sh', '/run.sh']])

    def test_runtime_dependency_and_public_permission_boundaries(self):
        dockerfile = (ADDON / 'Dockerfile').read_text()
        self.assertIn('install -y --no-install-recommends python3 python3-cryptography', dockerfile)
        self.assertIn('timeout --signal=TERM --kill-after=5 240 apt-get', dockerfile)
        self.assertIn('timeout --signal=TERM --kill-after=5 360 apt-get', dockerfile)
        self.assertIn('Acquire::Retries=0', dockerfile)
        self.assertIn('Acquire::https::Timeout=15', dockerfile)
        config = (ADDON / 'config.yaml').read_text()
        for line in ('arch: [amd64, aarch64]', 'boot: manual', 'host_network: false',
                     'docker_api: false', 'hassio_api: false', 'homeassistant_api: false'):
            self.assertIn(line, config.splitlines())
        for forbidden in ('privileged:', 'full_access:', 'ports:', 'map:', 'ingress: true'):
            self.assertNotIn(forbidden, config)

    def test_build_context_admits_only_the_reviewed_runtime_closure(self):
        self.assertEqual((ADDON / 'Dockerfile.dockerignore').read_text().splitlines(), [
            '**', '!Dockerfile', '!run.sh', '!ha_artifact_registry/', 'ha_artifact_registry/**',
            *['!ha_artifact_registry/' + name + '.py' for name in
              ('__init__', 'image', 'service', 'http', 'main', 'tls')]])


@unittest.skipUnless(os.environ.get('REGISTRY_PACKAGING_IMAGE'), 'Set REGISTRY_PACKAGING_IMAGE for local image checks')
class RegistryImageTests(unittest.TestCase):
    def docker(self, *arguments, check=False):
        result = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only',
                                 '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                                 '--pids-limit', '32', '--memory', '128m', '--cpus', '1',
                                 '--platform', os.environ.get('REGISTRY_PACKAGING_PLATFORM', 'linux/amd64'),
                                 '--tmpfs', '/data:rw,nosuid,nodev,noexec,mode=0700',
                                 '--entrypoint', 'python3', os.environ['REGISTRY_PACKAGING_IMAGE'],
                                 *arguments], capture_output=True, text=True, timeout=30)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_image_has_exact_modules_and_real_crypto_dependencies(self):
        result = self.docker('-c', """
from pathlib import Path
import hashlib, importlib, json, platform, ssl, sys
import cryptography
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
names = {'__init__', 'image', 'service', 'http', 'main', 'tls'}
root = Path('/opt/registry/ha_artifact_registry')
assert {p.name for p in root.iterdir()} == {n+'.py' for n in names}
for name in names: importlib.import_module('ha_artifact_registry.'+name)
key = Ed25519PrivateKey.generate(); key.public_key().verify(key.sign(b'fixture'), b'fixture')
assert not Path('/var/run/docker.sock').exists()
assert not Path('/opt/registry/codex_delivery').exists()
print(json.dumps({'architecture': platform.machine(), 'python': sys.version.split()[0],
                  'cryptography': cryptography.__version__, 'ssl': ssl.OPENSSL_VERSION,
                  'modules': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}}))
""", check=True)
        observed = json.loads(result.stdout)
        expected = {'linux/amd64': 'x86_64', 'linux/arm64': 'aarch64'}
        self.assertEqual(observed['architecture'], expected[os.environ.get('REGISTRY_PACKAGING_PLATFORM', 'linux/amd64')])
        self.assertEqual(observed['modules'], {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (ADDON / 'ha_artifact_registry').glob('*.py')})

    def test_missing_private_registration_fails_closed_with_generic_output(self):
        result = self.docker('-m', 'ha_artifact_registry.main')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, 'artifact registry startup unavailable\n')
        self.assertEqual(result.stderr, '')

    def test_synthetic_disabled_registration_opens_no_listener_and_stops_cleanly(self):
        self.docker('-c', """
import base64, json, os, signal, subprocess, sys
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
# Disposable synthetic policy only; no signing key or production registration.
key = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
digest = 'sha256:'+'a'*64
policy = {'version': 1, 'enabled': False, 'docker_bridge_host': '10.123.0.1',
          'public_key': base64.b64encode(key).decode(),
          'writer_token': 'fixture-writer-bearer-never-a-real-credential',
          'reader_token': 'fixture-reader-bearer-never-a-real-credential',
          'bindings': [{'target_ref': 'fixture-target', 'instance_ref': 'fixture-instance',
                        'slug': 'local_fixture_canary', 'repository': 'codex-work/amd64-canary',
                        'architecture': 'amd64', 'recipe_digest': digest,
                        'permissions_digest': digest, 'qualification_digest': digest}], 'grants': []}
os.umask(0o077)
path = Path('/data/registry-policy.json'); path.write_text(json.dumps(policy)); path.chmod(0o600)
child = subprocess.Popen([sys.executable, '-m', 'ha_artifact_registry.main'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
try:
    assert child.stdout.readline() == 'artifact registry candidate disabled\\n'
    for table in ('/proc/net/tcp', '/proc/net/tcp6'):
        assert not any(line.split()[3] == '0A' for line in Path(table).read_text().splitlines()[1:])
    child.send_signal(signal.SIGTERM)
    output, errors = child.communicate(timeout=5)
    assert child.returncode == 0 and not output and not errors
finally:
    if child.poll() is None: child.kill(); child.wait(timeout=5)
""", check=True)


if __name__ == '__main__':
    unittest.main()
