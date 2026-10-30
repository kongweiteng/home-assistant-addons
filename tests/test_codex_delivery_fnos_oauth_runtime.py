"""Isolated file-session and actual native-subprocess fixtures; no real CLI/NAS.

Unprivileged compiled fixtures mock only the root-ownership predicate. Production
has no root-pin bypass option. A separate real root-owned system binary exercises
the unmocked complete pin and subprocess path.
"""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery.fnos_oauth_provider import (
    CLIENT_ID, FnosOAuthProvider, OAuthBinding, OAuthProviderError)
from codex_delivery.codex_delivery.fnos_oauth_runtime import (
    SESSION_LIMIT, FileSessionLoader, FixedCLIRefreshPort)


RUNTIME_MODULE = 'codex_delivery.codex_delivery.fnos_oauth_runtime'
FIXTURE = r'''
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
extern char **environ;
int main(int argc, char **argv) {
    umask(0077);
    FILE *out = fopen("observed", "w");
    if (!out) return 80;
    for (int i=0; i<argc; i++) fprintf(out,"ARG:%s\n",argv[i]);
    for (char **item=environ; *item; item++) fprintf(out,"ENV:%s\n",*item);
    char cwd[8192];
    if (!getcwd(cwd,sizeof(cwd))) return 81;
    fprintf(out,"CWD:%s\n",cwd);
    char input;
    fprintf(out,"STDIN:%d\n",(int)read(0,&input,1));
    fclose(out);
    printf("fixture-sensitive-value\n");
    fprintf(stderr,"fixture-sensitive-value\n");
    if (argc!=5 || strcmp(argv[1],"--profile") || strcmp(argv[2],"backup")
            || strcmp(argv[3],"login") || strcmp(argv[4],"--refresh")) return 82;
    char mode[32]="";
    FILE *in=fopen("mode","r");
    if (in) { fgets(mode,sizeof(mode),in); fclose(in); }
    if (!strcmp(mode,"nonzero")) return 7;
    if (!strcmp(mode,"unchanged")) return 0;
    if (!strcmp(mode,"timeout")) {
        pid_t child=fork();
        if (child<0) return 83;
        usleep(500000);
        FILE *late=fopen("late-side-effect","a");
        if (late) { fprintf(late,"late\n"); fclose(late); }
        return 0;
    }
    in=fopen("next.json","r");
    if (!in) return 84;
    int fd=open("profiles/backup.new",O_WRONLY|O_CREAT|O_EXCL,0600);
    if (fd<0) return 85;
    char buffer[8192]; size_t size;
    while ((size=fread(buffer,1,sizeof(buffer),in))>0) {
        if (write(fd,buffer,size)!=(ssize_t)size) return 86;
    }
    fclose(in);
    if (fsync(fd)) return 87;
    close(fd);
    if (rename("profiles/backup.new","profiles/backup.json")) return 88;
    return 0;
}
'''


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc')
        if compiler is None:
            raise unittest.SkipTest('native C compiler unavailable')
        cls.build = tempfile.TemporaryDirectory()
        build = Path(cls.build.name).resolve()
        source = build / 'fixture.c'
        source.write_text(FIXTURE)
        cls.executable = build / 'trim-cli-fixture'
        subprocess.run([compiler, str(source), '-o', str(cls.executable)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cls.executable.chmod(0o755)
        cls.executable_sha = sha(cls.executable)
        # Warm macOS's first-execution validation outside the deadline test.
        subprocess.run([str(cls.executable), '--profile', 'backup', 'login', '--refresh'],
            cwd=build, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=5, check=False)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        (self.root / 'profiles').mkdir(mode=0o700)
        self.profile = self.root / 'profiles' / 'backup.json'
        self.binding = OAuthBinding('oauth-runtime', 'backup', 'https://nas.invalid:5667',
            1000, 'device-fixture', ('trim.file.read', 'trim.file.write'))
        self.session = dict(host='nas.invalid', port=5667, username='fixture',
            accessToken='fixture-access-token', scheme='https', tls_insecure=False,
            uid=1000, admin=False, refreshToken='fixture-refresh-token',
            accessTokenExpiresAt=int(time.time())+30, oauthClientId=CLIENT_ID,
            oauthDeviceId='device-fixture', oauthScope='trim.file.read trim.file.write',
            updatedAt='2026-10-29T08:30:00+08:00')
        self.write_session(self.session)
        self.loader = FileSessionLoader(self.binding, self.root, allow_file_backend=True)
        renewed = {**self.session, 'accessToken':'fixture-renewed-access-token',
                   'refreshToken':'fixture-rotated-refresh-token',
                   'accessTokenExpiresAt':int(time.time())+3600}
        (self.root / 'next.json').write_text(json.dumps(renewed))
        (self.root / 'next.json').chmod(0o600)

    def tearDown(self):
        self.temporary.cleanup()

    def write_session(self, value):
        self.profile.write_text(json.dumps(value))
        self.profile.chmod(0o600)

    def load(self, loader=None):
        return (loader or self.loader).load(deadline=time.monotonic()+2)

    def port(self, **kwargs):
        return FixedCLIRefreshPort(self.loader, self.executable, self.executable_sha,
            allow_file_backend=True, **kwargs)

    @contextmanager
    def fixture_root(self):
        # Only this ownership fact is simulated; SHA, native format, ancestry,
        # mode/link checks, actual Popen/env/deadline/group cleanup stay real.
        with patch(RUNTIME_MODULE+'._root_owned', return_value=True):
            yield

    def test_file_backend_is_disabled_by_default_without_reading_session(self):
        disabled = FileSessionLoader(self.binding, self.root)
        self.profile.unlink()
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_file_backend_disabled$'):
            self.load(disabled)
        port = FixedCLIRefreshPort(self.loader, self.executable, self.executable_sha)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_file_backend_disabled$'):
            port.refresh_once(deadline=time.monotonic()+2)
        self.assertFalse((self.root / 'observed').exists())

    def test_access_only_session_preserves_pins_and_does_not_expose_refresh_token(self):
        result = self.load()
        self.assertEqual(result.access_token, self.session['accessToken'])
        self.assertEqual(result.scopes, self.binding.scopes)
        self.assertFalse(hasattr(result, 'refresh_token'))
        self.assertNotIn(self.session['accessToken'], repr(result))
        self.assertNotIn(self.session['refreshToken'], repr(result))

    def test_session_can_be_atomically_replaced_without_reenrolling_runtime(self):
        self.load()
        temporary = self.root / 'profiles' / 'replacement'
        temporary.write_text(json.dumps({**self.session, 'accessToken':'fixture-second-access-token'}))
        temporary.chmod(0o600)
        temporary.replace(self.profile)
        self.assertEqual(self.load().access_token, 'fixture-second-access-token')

    def test_all_endpoint_identity_scope_and_schema_drifts_fail_closed(self):
        changes = [dict(host='other.invalid'), dict(port=443), dict(port=True),
            dict(scheme='http'), dict(tls_insecure=True), dict(tls_insecure=0), dict(uid=1001),
            dict(uid=True), dict(admin='false'), dict(oauthClientId='other'),
            dict(oauthDeviceId='other'), dict(oauthScope='trim.file.read'),
            dict(oauthScope='trim.file.read trim.file.write trim.admin'),
            dict(oauthScope='trim.file.read trim.file.read'),
            dict(oauthScope='trim.file.read  trim.file.write'),
            dict(accessToken='injected\r\nheader'), dict(accessTokenExpiresAt=True),
            dict(refreshToken='bad\nrefresh'), dict(updatedAt='bad\nvalue'),
            dict(username='bad\x00name'), dict(unknown='field')]
        for change in changes:
            self.write_session({**self.session, **change})
            with self.subTest(change=change), self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_session_invalid$'):
                self.load()
        self.write_session({key:item for key,item in self.session.items() if key != 'refreshToken'})
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_session_invalid$'):
            self.load()

    def test_duplicate_nonfinite_oversized_json_and_nonregular_file_are_rejected(self):
        for raw in (b'{"host":"x","host":"y"}', b'{"n":NaN}', b'\xff', b'',
                    b' '*(SESSION_LIMIT+1)):
            self.profile.write_bytes(raw)
            with self.subTest(size=len(raw)), self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_(session|runtime)_invalid$'):
                self.load()
        self.profile.unlink()
        os.mkfifo(self.profile, 0o600)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_runtime_invalid$'):
            self.load()

    def test_symlink_hardlink_owner_permissions_and_dirty_paths_are_rejected(self):
        self.profile.chmod(0o644)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_runtime_invalid$'):
            self.load()
        self.profile.chmod(0o600)
        other = self.root / 'hardlinked'
        os.link(self.profile, other)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_runtime_invalid$'):
            self.load()
        other.unlink()
        self.profile.rename(other)
        self.profile.symlink_to(other)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_runtime_invalid$'):
            self.load()
        for root in (str(self.root)+'/', str(self.root)+'/./', str(self.root)+'/../other', 'relative'):
            with self.subTest(root=root), self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_runtime_invalid$'):
                FileSessionLoader(self.binding, root, allow_file_backend=True)

    def test_runtime_and_profiles_permissions_and_inode_drift_are_rejected(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_runtime_invalid$'):
            self.load()
        self.root.chmod(0o700)
        profiles = self.root / 'profiles'
        profiles.chmod(0o755)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_runtime_invalid$'):
            self.load()
        profiles.chmod(0o700)
        self.load()
        profiles.rename(self.root / 'old-profiles')
        profiles.mkdir(mode=0o700)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_runtime_drift$'):
            self.load()

    def test_missing_registered_root_never_discovers_keychain_or_another_profile(self):
        loader = FileSessionLoader(self.binding, self.root / 'absent', allow_file_backend=True)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_session_unavailable$'):
            self.load(loader)
        self.profile.rename(self.root / 'profiles' / 'different.json')
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_session_invalid$'):
            self.load()

    def test_real_root_owned_binary_pin_and_real_subprocess_without_ownership_mock(self):
        executable = Path('/usr/bin/true')
        port = FixedCLIRefreshPort(self.loader, executable, sha(executable), allow_file_backend=True)
        port.refresh_once(deadline=time.monotonic()+2)
        self.assertEqual(self.load().access_token, self.session['accessToken'])

    def test_startup_validation_reads_actual_native_pin_without_invoking_or_rotating(self):
        original = self.profile.read_bytes()
        port = self.port()
        with self.fixture_root(), patch(RUNTIME_MODULE+'.subprocess.Popen') as child:
            port.validate_runtime(deadline=time.monotonic()+2)
        child.assert_not_called()
        self.assertEqual(self.profile.read_bytes(), original)
        self.assertFalse((self.root/'observed').exists())
        # A read-only preflight does not consume the one refresh attempt.
        with self.fixture_root():
            port.refresh_once(deadline=time.monotonic()+2)
        self.assertEqual(self.load().access_token, 'fixture-renewed-access-token')

    def test_startup_validation_rejects_missing_or_changed_cli_before_any_spawn(self):
        for executable, pinned in ((self.root/'missing-cli', self.executable_sha),
                                   (self.executable, '0'*64)):
            port = FixedCLIRefreshPort(self.loader, executable, pinned, allow_file_backend=True)
            with self.fixture_root(), patch(RUNTIME_MODULE+'.subprocess.Popen') as child:
                with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_cli_pin_invalid$'):
                    port.validate_runtime(deadline=time.monotonic()+2)
            child.assert_not_called()
        self.assertFalse((self.root/'observed').exists())

    def test_user_owned_executable_cannot_run_despite_correct_sha(self):
        if os.geteuid() == 0:
            self.skipTest('user-owned rejection requires unprivileged test user')
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_cli_pin_invalid$'):
            self.port().refresh_once(deadline=time.monotonic()+2)
        self.assertFalse((self.root / 'observed').exists())

    def test_native_fixture_argv_environment_output_and_real_session_refresh(self):
        calls = []
        real_popen = subprocess.Popen
        def observed(*args, **kwargs):
            calls.append((args, kwargs))
            return real_popen(*args, **kwargs)
        port = self.port()
        with self.fixture_root(), patch(RUNTIME_MODULE+'.subprocess.Popen', observed), patch.dict(os.environ, {
            'FORBIDDEN_SECRET':'fixture-secret', 'HTTP_PROXY':'http://forbidden.invalid',
            'PYTHONPATH':'/forbidden', 'TRIM_CLI_SESSION_STORAGE':'ask-file'}):
            provider = FnosOAuthProvider(self.binding, self.loader, port,
                journal_path=self.root / 'oauth-journal.json')
            result = provider(deadline=time.monotonic()+3)
        self.assertEqual(result, 'fixture-renewed-access-token')
        args, kwargs = calls[0]
        self.assertEqual(args[0], [str(self.executable), '--profile', 'backup', 'login', '--refresh'])
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        lines = (self.root / 'observed').read_text().splitlines()
        self.assertEqual([line for line in lines if line.startswith('ARG:')],
            ['ARG:'+str(self.executable), 'ARG:--profile', 'ARG:backup', 'ARG:login', 'ARG:--refresh'])
        env = dict(line[4:].split('=', 1) for line in lines if line.startswith('ENV:'))
        self.assertEqual(env, {'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8', 'LC_ALL':'C.UTF-8',
            'TZ':'Asia/Shanghai', 'TRIM_CLI_SESSION_STORAGE':'file', 'TRIM_CLI_CONFIG_DIR':str(self.root)})
        self.assertIn('STDIN:0', lines)
        self.assertIn('CWD:'+str(self.root), lines)
        self.assertNotIn('fixture-secret', '\n'.join(lines))
        self.assertNotIn(self.session['refreshToken'], json.dumps(kwargs))
        self.assertEqual(json.loads((self.root / 'oauth-journal.json').read_text())['state'], 'ready')

    def test_nonzero_exit_is_stable_unknown_and_cannot_be_replayed(self):
        (self.root / 'mode').write_text('nonzero')
        (self.root / 'mode').chmod(0o600)
        port = self.port()
        with self.fixture_root():
            with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_refresh_unknown$') as caught:
                port.refresh_once(deadline=time.monotonic()+2)
            self.assertIsNone(caught.exception.__cause__)
            (self.root / 'observed').unlink()
            with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_refresh_unknown$'):
                port.refresh_once(deadline=time.monotonic()+2)
        self.assertFalse((self.root / 'observed').exists())

    def test_deadline_kills_actual_process_group_and_provider_stays_fused(self):
        (self.root / 'mode').write_text('timeout')
        (self.root / 'mode').chmod(0o600)
        port = self.port()
        provider = FnosOAuthProvider(self.binding, self.loader, port,
            journal_path=self.root / 'oauth-journal.json')
        started = time.monotonic()
        with self.fixture_root():
            with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_refresh_unknown$'):
                provider(deadline=started+0.12)
        self.assertLess(time.monotonic()-started, 0.5)
        self.assertTrue((self.root / 'observed').exists())
        time.sleep(0.6)
        self.assertFalse((self.root / 'late-side-effect').exists())
        self.assertEqual(json.loads((self.root / 'oauth-journal.json').read_text())['state'], 'fused')
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_refresh_unknown$'):
            provider(deadline=time.monotonic()+1)

    def test_successful_exit_without_persisted_new_session_is_not_provider_success(self):
        (self.root / 'mode').write_text('unchanged')
        (self.root / 'mode').chmod(0o600)
        provider = FnosOAuthProvider(self.binding, self.loader, self.port(),
            journal_path=self.root / 'oauth-journal.json')
        with self.fixture_root(), self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_refresh_unknown$'):
            provider(deadline=time.monotonic()+2)
        self.assertEqual(json.loads((self.root / 'oauth-journal.json').read_text())['state'], 'fused')

    def test_sha_mode_hardlink_symlink_and_script_pins_are_rejected_before_process(self):
        wrong = FixedCLIRefreshPort(self.loader, self.executable, '0'*64, allow_file_backend=True)
        with self.fixture_root(), self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_cli_pin_invalid$'):
            wrong.refresh_once(deadline=time.monotonic()+2)
        copy = Path(self.build.name).resolve() / ('cli-copy-'+self.root.name)
        shutil.copyfile(self.executable, copy)
        copy.chmod(0o777)
        unsafe = FixedCLIRefreshPort(self.loader, copy, sha(copy), allow_file_backend=True)
        with self.fixture_root(), self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_cli_pin_invalid$'):
            unsafe.refresh_once(deadline=time.monotonic()+2)
        copy.chmod(0o755)
        os.link(copy, copy.with_name(copy.name+'-hardlink'))
        with self.fixture_root(), self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_cli_pin_invalid$'):
            unsafe.refresh_once(deadline=time.monotonic()+2)
        linked = copy.with_name(copy.name+'-symlink')
        linked.symlink_to(self.executable)
        symlinked = FixedCLIRefreshPort(self.loader, linked, self.executable_sha, allow_file_backend=True)
        with self.fixture_root(), self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_cli_pin_invalid$'):
            symlinked.refresh_once(deadline=time.monotonic()+2)
        script = copy.with_name(copy.name+'-wrapper')
        script.write_text('#!/bin/sh\nexit 0\n')
        script.chmod(0o755)
        wrapper = FixedCLIRefreshPort(self.loader, script, sha(script), allow_file_backend=True)
        with self.fixture_root(), self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_cli_pin_invalid$'):
            wrapper.refresh_once(deadline=time.monotonic()+2)
        self.assertFalse((self.root / 'observed').exists())

    def test_expired_deadline_or_identity_drift_never_starts_process(self):
        port = self.port()
        with self.fixture_root(), self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_deadline$'):
            port.refresh_once(deadline=time.monotonic()-1)
        self.write_session({**self.session, 'uid':1001})
        with self.fixture_root(), self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_session_invalid$'):
            port.refresh_once(deadline=time.monotonic()+2)
        self.assertFalse((self.root / 'observed').exists())

    def test_runtime_owner_drift_is_rejected_before_session_contents_are_read(self):
        real_stat = os.stat
        def foreign_owner(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == 'backup.json':
                values = list(info)
                values[4] = info.st_uid + 1
                return os.stat_result(values)
            return info
        with patch(RUNTIME_MODULE+'.os.stat', foreign_owner), self.assertRaisesRegex(
                OAuthProviderError, '^fnos_oauth_runtime_invalid$'):
            self.load()

    def test_official_lock_and_device_data_are_private_and_unsafe_auxiliaries_fail(self):
        lock = self.root / 'profiles' / 'backup.refresh.lock'
        lock.write_bytes(b'')
        lock.chmod(0o600)
        device = self.root / 'oauth-device-id'
        device.write_text('device-fixture')
        device.chmod(0o600)
        self.load()
        lock.unlink()
        lock.symlink_to(self.profile)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_runtime_invalid$'):
            self.load()
        lock.unlink()
        os.link(self.profile, lock)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_runtime_invalid$'):
            self.load()
        lock.unlink()
        helper = self.root / 'unregistered-helper'
        helper.write_text('not a CLI data file')
        helper.chmod(0o755)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_runtime_invalid$'):
            self.load()

    def test_runtime_tree_budget_is_bounded(self):
        extra = self.root / 'extra'
        extra.mkdir(mode=0o700)
        for index in range(256):
            path = extra / str(index)
            path.write_bytes(b'')
            path.chmod(0o600)
        with self.assertRaisesRegex(OAuthProviderError, '^fnos_oauth_runtime_invalid$'):
            self.load()

    def test_late_preflight_cannot_dispatch_process(self):
        port = self.port()
        def late(**kwargs):
            time.sleep(0.04)
        with patch.object(port, '_check_cli', late), self.assertRaisesRegex(
                OAuthProviderError, '^fnos_oauth_deadline$'):
            port.refresh_once(deadline=time.monotonic()+0.01)
        self.assertFalse((self.root / 'observed').exists())

    def test_cli_replaced_after_exit_withholds_new_session_and_persistently_fuses(self):
        copy = Path(self.build.name).resolve() / ('post-exit-'+self.root.name)
        shutil.copyfile(self.executable, copy)
        copy.chmod(0o755)
        port = FixedCLIRefreshPort(self.loader, copy, sha(copy), allow_file_backend=True)
        provider = FnosOAuthProvider(self.binding, self.loader, port,
            journal_path=self.root / 'oauth-journal.json')
        real_popen = subprocess.Popen
        def replaced(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            real_wait = process.wait
            changed = False
            def wait(*args, **kwargs):
                nonlocal changed
                result = real_wait(*args, **kwargs)
                if not changed:
                    changed = True
                    other = copy.with_name(copy.name+'-new')
                    shutil.copyfile(self.executable, other)
                    other.chmod(0o755)
                    other.replace(copy)
                return result
            process.wait = wait
            return process
        with self.fixture_root(), patch(RUNTIME_MODULE+'.subprocess.Popen', replaced), self.assertRaisesRegex(
                OAuthProviderError, '^fnos_oauth_refresh_unknown$'):
            provider(deadline=time.monotonic()+2)
        self.assertEqual(self.load().access_token, 'fixture-renewed-access-token')
        self.assertEqual(json.loads((self.root / 'oauth-journal.json').read_text())['state'], 'fused')


if __name__ == '__main__':
    unittest.main()
