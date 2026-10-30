"""Compatibility re-exports and actual standalone minimal-package imports."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from codex_delivery.codex_delivery import contracts, fnos_file_settings, store


MINIMAL_RUNTIME_FILES = (
    '__init__.py', 'contracts.py', 'durability.py', 'encrypted_replica.py',
    'fnos_file_replica.py', 'fnos_file_port.py', 'fnos_oauth_provider.py', 'fnos_oauth_runtime.py',
)


class ContractTests(unittest.TestCase):
    def test_store_and_settings_reexport_the_same_exact_objects(self):
        self.assertIs(store.DeliveryError, contracts.DeliveryError)
        self.assertIs(store.REF, contracts.REF)
        self.assertIs(store.reference, contracts.reference)
        self.assertIs(fnos_file_settings.DeliveryError, contracts.DeliveryError)
        self.assertIs(fnos_file_settings.reference, contracts.reference)
        self.assertIs(fnos_file_settings.fixed_endpoint, contracts.fixed_endpoint)
        error = store.DeliveryError('fixture-code')
        self.assertEqual((str(error), error.code, error.args),
                         ('fixture-code', 'fixture-code', ('fixture-code',)))

    def test_reference_exact_behavior_and_string_identity_are_preserved(self):
        class String(str): pass
        for value in ('a', 'actor:'+'a'*64, 'a'+'.:_-012ABCxyz'*9, 'a'*128, String('valid')):
            self.assertIs(contracts.reference(value), value)
        for value in ('', 'a'*129, '-a', 'a/b', 'a b', '含中文', 'a\n', None, 1, b'a'):
            with self.subTest(value=value), self.assertRaises(store.DeliveryError) as caught:
                store.reference(value)
            self.assertEqual((caught.exception.code, str(caught.exception)),
                             ('reference_invalid', 'reference_invalid'))

    def minimal_process(self, files, code):
        source = Path(contracts.__file__).parent
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary).resolve() / 'standalone_replica'
            package.mkdir()
            for name in files:
                shutil.copyfile(source / name, package / name)
            result = subprocess.run([sys.executable, '-I', '-B', '-c',
                'import sys\nsys.path.insert(0, '+repr(str(package.parent))+')\n'+code],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'ok\n')

    def test_contracts_alone_has_no_delivery_application_or_crypto_dependency(self):
        self.minimal_process(('__init__.py', 'contracts.py'), '''
from standalone_replica.contracts import DeliveryError, reference, fixed_endpoint
assert reference('fixture-ref') == 'fixture-ref'
assert fixed_endpoint('https://nas.invalid:5667') == 'https://nas.invalid:5667'
assert not any(name.startswith('standalone_replica.') and name != 'standalone_replica.contracts'
               for name in sys.modules)
assert 'sqlite3' not in sys.modules and 'cryptography' not in sys.modules
print('ok')
''')

    def test_minimal_actual_oauth_runtime_package_imports_without_store_or_settings(self):
        self.minimal_process(MINIMAL_RUNTIME_FILES, '''
from standalone_replica.fnos_oauth_provider import OAuthBinding, FnosOAuthProvider
from standalone_replica.fnos_oauth_runtime import FileSessionLoader, FixedCLIRefreshPort
from standalone_replica.fnos_file_port import FnosHTTPFilePort
binding = OAuthBinding('credential-fixture', 'backup', 'https://nas.invalid:5667',
    1000, 'device-fixture', ('trim.file.read', 'trim.file.write'))
provider = FnosOAuthProvider(binding)
loader = FileSessionLoader(binding, '/unconfigured-private-runtime')
assert provider.binding is binding and loader.binding is binding
for suffix in ('store', 'repo_manager', 'service', 'main', 'fnos_file_settings'):
    assert 'standalone_replica.' + suffix not in sys.modules
assert 'sqlite3' not in sys.modules
print('ok')
''')

    def test_settings_are_an_optional_ninth_file_without_store_dependencies(self):
        self.minimal_process(MINIMAL_RUNTIME_FILES+('fnos_file_settings.py',), '''
from standalone_replica.fnos_file_settings import FnosFileSettings, fixed_endpoint
from standalone_replica.contracts import fixed_endpoint as pure_endpoint
assert fixed_endpoint is pure_endpoint
assert FnosFileSettings.from_dict({'enabled':False}) is None
for suffix in ('store', 'repo_manager', 'service', 'main'):
    assert 'standalone_replica.' + suffix not in sys.modules
assert 'sqlite3' not in sys.modules
print('ok')
''')


if __name__ == '__main__':
    unittest.main()
