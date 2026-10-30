"""Access-only trusted Ports and persistent no-retry OAuth refresh claims."""
from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery.fnos_oauth_provider import (
    CLIENT_ID, FnosOAuthProvider, OAuthAccessSession, OAuthBinding, OAuthProviderError)


class Loader:
    def __init__(self, session):
        self.session, self.calls = session, 0

    def load(self, *, deadline):
        self.calls += 1
        return self.session


class Refresh:
    def __init__(self, loader):
        self.loader, self.calls = loader, 0

    def refresh_once(self, *, deadline):
        self.calls += 1
        self.loader.session = replace(self.loader.session,
            access_token='renewed-fixture-token', expires_at=int(time.time())+3600)


def child_unknown(path, binding, session, connection):
    try:
        loader = Loader(session)
        refresh = Refresh(loader)
        provider = FnosOAuthProvider(binding, loader, refresh, journal_path=path)
        try:
            provider(deadline=time.monotonic()+2)
            connection.send(('accepted', refresh.calls))
        except OAuthProviderError as error:
            connection.send((error.code, refresh.calls))
    finally:
        connection.close()


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.path = self.root / 'oauth-state.json'
        self.binding = OAuthBinding('oauth-runtime', 'backup', 'https://nas.invalid:5667',
            1000, 'device-fixture', ('trim.file.read', 'trim.file.write'))
        self.session = OAuthAccessSession(self.binding.endpoint, self.binding.uid,
            self.binding.client_id, self.binding.device_id, self.binding.scopes,
            'access-fixture-token', int(time.time())+3600)
        self.loader = Loader(self.session)
        self.refresh = Refresh(self.loader)

    def tearDown(self):
        self.temporary.cleanup()

    def provider(self, **kwargs):
        return FnosOAuthProvider(self.binding, self.loader, self.refresh,
                                 journal_path=self.path, **kwargs)

    def call(self, provider):
        return provider(deadline=time.monotonic()+2)

    def expire(self):
        self.loader.session = replace(self.session, expires_at=int(time.time())+30)

    def test_no_implicit_session_discovery_or_storage_without_loader(self):
        provider = FnosOAuthProvider(self.binding)
        self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_disabled$'):
            self.call(provider)
        self.assertNotIn('nas.invalid', repr(self.binding))
        self.assertNotIn('access-fixture', repr(self.session))

    def test_nonexpiring_access_uses_loader_only_and_journal_never_contains_tokens(self):
        provider = self.provider()
        self.assertEqual(self.call(provider), self.session.access_token)
        self.assertEqual((self.loader.calls, self.refresh.calls), (1, 0))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        raw = self.path.read_text()
        self.assertNotIn(self.session.access_token, raw)
        self.assertNotIn('refreshToken', raw)
        self.assertEqual(json.loads(raw)['state'], 'ready')

    def test_one_claimed_official_refresh_then_independent_new_session_read(self):
        self.expire()
        provider = self.provider()
        self.assertEqual(self.call(provider), 'renewed-fixture-token')
        self.assertEqual((self.loader.calls, self.refresh.calls), (2, 1))
        self.assertEqual(self.call(provider), 'renewed-fixture-token')
        self.assertEqual(self.refresh.calls, 1)
        self.assertEqual(json.loads(self.path.read_text())['state'], 'ready')

    def test_expiring_token_without_official_port_never_attempts_implicit_login(self):
        self.expire()
        provider = FnosOAuthProvider(self.binding, self.loader, journal_path=self.path)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_refresh_unavailable$'):
            self.call(provider)
        self.assertEqual(json.loads(self.path.read_text())['state'], 'ready')

    def test_exact_scope_identity_endpoint_and_access_token_pins(self):
        changes = [dict(endpoint='https://other.invalid'), dict(uid=1001), dict(uid=True),
            dict(client_id='other'), dict(device_id='other'), dict(scopes=('trim.file.read',)),
            dict(scopes=self.binding.scopes+('trim.admin',)),
            dict(scopes=('trim.file.read', 'trim.file.read')),
            dict(access_token='bad\r\nAuthorization: secret'), dict(expires_at=True)]
        provider = self.provider()
        for change in changes:
            self.loader.session = replace(self.session, **change)
            with self.subTest(change=change), self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_session_invalid$'):
                self.call(provider)
        self.assertEqual(self.refresh.calls, 0)

    def test_binding_schema_is_closed_and_cannot_accept_arbitrary_oauth_client(self):
        changes = [dict(profile='../bad'), dict(profile='..'), dict(profile=''),
            dict(profile='含中文'), dict(endpoint='http://nas.invalid'), dict(uid=True),
            dict(client_id='unregistered'), dict(scopes=('trim.file.read',)),
            dict(scopes=('trim.file.read', 'trim.file.write', 'bad scope')),
            dict(device_id='bad\nvalue'), dict(credential_ref='bad/token')]
        for change in changes:
            with self.subTest(change=change), self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_config_invalid$'):
                replace(self.binding, **change)

    def test_lost_refresh_reply_is_persistently_unknown_even_if_new_session_written(self):
        self.expire()
        original = self.refresh.refresh_once
        def lost(**kwargs):
            original(**kwargs)
            raise TimeoutError('refreshToken and raw secret must not escape')
        self.refresh.refresh_once = lost
        provider = self.provider()
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_refresh_unknown$') as caught:
            self.call(provider)
        self.assertIsNone(caught.exception.__cause__)
        for candidate in (provider,):
            with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_refresh_unknown$'):
                self.call(candidate)
        self.assertEqual(self.refresh.calls, 1)
        self.assertEqual(json.loads(self.path.read_text())['state'], 'fused')
        # Spawn is a genuinely new process, without the process-local poison set.
        context = multiprocessing.get_context('spawn')
        incoming, outgoing = context.Pipe(duplex=False)
        child = context.Process(target=child_unknown,
            args=(str(self.path), self.binding, self.loader.session, outgoing))
        child.start()
        outgoing.close()
        try:
            self.assertTrue(incoming.poll(5))
            self.assertEqual(incoming.recv(), ('fnos_oauth_refresh_unknown', 0))
        finally:
            child.join(5)
            if child.is_alive(): child.terminate(); child.join(5)
            incoming.close()
        self.assertEqual(child.exitcode, 0)

    def test_success_exit_without_new_valid_session_never_retries_old_refresh(self):
        self.expire()
        self.refresh.refresh_once = lambda **_: None
        provider = self.provider()
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_refresh_unknown$'):
            self.call(provider)
        self.assertEqual(json.loads(self.path.read_text())['state'], 'fused')

    def test_post_refresh_session_read_failure_is_unknown(self):
        self.expire()
        original = self.loader.load
        def load(**kwargs):
            if self.refresh.calls:
                raise OSError('private raw session failure')
            return original(**kwargs)
        self.loader.load = load
        provider = self.provider()
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_refresh_unknown$'):
            self.call(provider)
        self.assertEqual(self.refresh.calls, 1)

    def test_post_refresh_journal_failure_preserves_persistent_inflight(self):
        self.expire()
        provider = self.provider()
        original = provider._journal.write
        def fail_after_refresh(directory, state):
            if self.refresh.calls:
                raise OSError('disk unavailable')
            return original(directory, state)
        with patch.object(provider._journal, 'write', fail_after_refresh):
            with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_refresh_unknown$'):
                self.call(provider)
        self.assertEqual(json.loads(self.path.read_text())['state'], 'inflight')
        self.assertEqual(self.refresh.calls, 1)

    def test_late_loader_cannot_dispatch_refresh_after_deadline(self):
        self.expire()
        def late(**kwargs):
            time.sleep(0.04)
            return self.loader.session
        self.loader.load = late
        provider = self.provider()
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_deadline$'):
            provider(deadline=time.monotonic()+0.01)
        self.assertEqual(self.refresh.calls, 0)
        self.assertEqual(json.loads(self.path.read_text())['state'], 'ready')

    def test_late_refresh_is_unknown_and_late_success_cannot_clear_claim(self):
        self.expire()
        original = self.refresh.refresh_once
        def late(**kwargs):
            original(**kwargs)
            time.sleep(0.04)
        self.refresh.refresh_once = late
        provider = self.provider()
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_refresh_unknown$'):
            provider(deadline=time.monotonic()+0.01)
        self.assertEqual(json.loads(self.path.read_text())['state'], 'fused')

    def test_concurrent_provider_instance_cannot_read_or_refresh_active_claim(self):
        self.expire()
        first = self.provider()
        second = self.provider()
        entered, released = threading.Event(), threading.Event()
        results = []
        original = self.refresh.refresh_once
        def blocked(**kwargs):
            entered.set()
            released.wait(2)
            original(**kwargs)
        self.refresh.refresh_once = blocked
        def run():
            try: results.append(self.call(first))
            except Exception as error: results.append(str(error))
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_busy$'):
                self.call(second)
        finally:
            released.set(); worker.join(2)
        self.assertEqual(results, ['renewed-fixture-token'])
        self.assertEqual(self.refresh.calls, 1)

    def test_permissions_symlink_binding_drift_and_journal_deletion_fail_closed(self):
        provider = self.provider()
        self.path.chmod(0o644)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_journal_invalid$'):
            self.call(provider)
        self.path.chmod(0o600)
        other = replace(self.binding, credential_ref='different-credential')
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_journal_invalid$'):
            FnosOAuthProvider(other, self.loader, self.refresh, journal_path=self.path)
        self.path.unlink()
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_journal_invalid$'):
            self.call(provider)
        target = self.root / 'target'
        target.write_text('private')
        target.chmod(0o600)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_journal_invalid$'):
            self.provider()
        self.assertEqual(target.read_text(), 'private')

    def test_malformed_journal_is_not_reset_and_inflight_does_not_load_session(self):
        provider = self.provider()
        value = json.loads(self.path.read_text())
        value['state'] = 'inflight'
        self.path.write_bytes(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_refresh_unknown$'):
            self.call(provider)
        self.assertEqual(self.loader.calls, 0)
        self.path.write_bytes(b'{"version":1,"version":1}')
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_journal_invalid$'):
            self.provider()
        self.assertEqual(self.path.read_bytes(), b'{"version":1,"version":1}')


if __name__ == '__main__':
    unittest.main()
