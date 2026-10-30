"""Default image shape does not need optional Registry sibling imports."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery import software_registration as boot
from codex_delivery.codex_delivery.store import DeliveryError


class OptionalSoftwareTests(unittest.TestCase):
    def test_absent_registration_never_imports_optional_plan_or_reads_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(boot, 'REGISTRY_REGISTRATION', Path(directory)/'registry'), \
                    patch.object(boot, 'REPOSITORY_REGISTRATION', Path(directory)/'repository'), \
                    patch.object(boot, 'GIT_REGISTRATION', Path(directory)/'git'), \
                    patch.object(boot, '_file') as read:
                self.assertIsNone(boot.load_registry_plan(forbidden_credentials=()))
                self.assertIsNone(boot.load_repository_plan(forbidden_credentials=()))
                self.assertIsNone(boot.load_git_plan(forbidden_credentials=()))
                read.assert_not_called()

    def test_exact_disabled_is_checked_for_provenance_and_never_loads_policy(self):
        with tempfile.NamedTemporaryFile() as registration:
            for method, field in ((boot.load_registry_plan, 'REGISTRY_REGISTRATION'),
                                  (boot.load_repository_plan, 'REPOSITORY_REGISTRATION'),
                                  (boot.load_git_plan, 'GIT_REGISTRATION')):
                with self.subTest(field=field), patch.object(boot, field, Path(registration.name)), \
                        patch.object(boot, '_file', return_value=b'{"version":1,"enabled":false}') as read:
                    self.assertIsNone(method(forbidden_credentials=()))
                    read.assert_called_once_with(Path(registration.name), private=True, optional=True)

    def test_existing_broken_registration_cannot_be_treated_as_disabled(self):
        with tempfile.NamedTemporaryFile() as registration:
            for method, field in ((boot.load_registry_plan, 'REGISTRY_REGISTRATION'),
                                  (boot.load_repository_plan, 'REPOSITORY_REGISTRATION'),
                                  (boot.load_git_plan, 'GIT_REGISTRATION')):
                with self.subTest(field=field), patch.object(boot, field, Path(registration.name)), \
                        patch.object(boot, '_file', side_effect=DeliveryError('provenance_fixture')):
                    with self.assertRaises(DeliveryError): method(forbidden_credentials=())

    def test_isolated_default_package_config_check_never_imports_registry_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = Path(__file__).resolve().parents[1]/'codex_delivery'
            for package in ('codex_delivery', 'codex_work_runtime'):
                shutil.copytree(source/package, root/package, ignore=shutil.ignore_patterns('__pycache__'))
            options = root/'options.json'
            options.write_text(json.dumps(dict(extension_bearer='x'*32, origin_key='y'*32,
                allowed_actors=['actor:'+'a'*64], worker_enabled=False)))
            script = '''
import importlib.abc, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
class ForbidRegistry(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('ha_artifact_registry'):
            raise RuntimeError('optional Registry imported by metadata-only image')
sys.meta_path.insert(0, ForbidRegistry())
from codex_delivery import software_registration as boot
boot.REGISTRY_REGISTRATION=pathlib.Path(sys.argv[1])/'absent-registry'
boot.REPOSITORY_REGISTRATION=pathlib.Path(sys.argv[1])/'absent-repository'
boot.GIT_REGISTRATION=pathlib.Path(sys.argv[1])/'absent-git'
from codex_delivery.main import main
sys.argv=['delivery','--check-config']
raise SystemExit(main())
'''
            completed = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(root)],
                cwd=root, env=dict(os.environ, CODEX_DELIVERY_OPTIONS_FILE=str(options),
                    CODEX_DELIVERY_STATE_ROOT=str(root/'state')), capture_output=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(json.loads(completed.stdout), {'config_valid':True, 'execution_enabled':False})
            self.assertFalse((root/'state').exists())
            self.assertFalse((root/'ha_artifact_registry').exists())


if __name__ == '__main__': unittest.main()
