"""Real local bare Git only; no production Git credentials or network access."""
from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery.durability import digest
from codex_delivery.codex_delivery.repo_manager import (
    AuthorizationReceipt, GitRuntime, RepoManager, RepositoryPolicy,
    ValidationReceipt, install_repository_schema,
)
from codex_delivery.codex_delivery.store import DeliveryError, DeliveryStore, canonical
from test_codex_delivery_store import ReplicaFixture


class LocalGit(GitRuntime):
    """TEST ONLY: production has no local/file transport configuration switch."""
    def __init__(self, remote, store):
        super().__init__(shutil.which('git'), timeout_seconds=10)
        self.remote, self.store = remote, store
        self.pushes = 0
        self.fetches = 0
        self.reads = 0
        self.failure = None
        self.after_fetch = None
        self.before_push = None
        self.started = None
        self.release = None
        self.commands = []

    def run(self, home, repository, arguments, budget, **kwargs):
        self.commands.append(list(arguments))
        return super().run(home, repository, arguments, budget, **kwargs)

    def _configuration(self, home):
        return [*super()._configuration(home), '-c', 'protocol.file.allow=always']

    def _remote(self, policy):
        return str(self.remote)

    def fetch(self, *args):
        self.fetches += 1
        super().fetch(*args)
        if self.after_fetch:
            self.after_fetch()

    def ls_remote(self, *args):
        self.reads += 1
        return super().ls_remote(*args)

    def push(self, *args):
        self.pushes += 1
        with closing(self.store._connect()) as connection:
            publication = connection.execute("SELECT publication_ref FROM repository_publications WHERE state='sending'").fetchone()
            assert publication is not None
            evidence = connection.execute("SELECT d.state FROM commands c JOIN durability d ON d.event_id=c.event_id WHERE json_extract(c.result_json,'$.publication_ref')=? AND json_extract(c.result_json,'$.state')='sending'", (publication['publication_ref'],)).fetchone()
            assert evidence is not None and evidence['state'] == 'confirmed', 'push before replicated claim'
        if self.before_push:
            self.before_push()
        if self.started is not None:
            self.started.set()
            self.release.wait(2)
        if self.failure == 'before':
            raise DeliveryError('repository_git_timeout')
        super().push(*args)
        if self.failure == 'after':
            raise DeliveryError('repository_git_timeout')
        if self.failure == 'crash':
            raise SystemExit('test-only parent crash after actual Git push')


class RepoManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.author = self.root / 'trusted-author-fixture'
        self.remote = self.root / 'remote-fixture.git'
        self.fixture_git(['init', '-b', 'main', str(self.author)])
        (self.author / 'README.md').write_text('baseline fixture\n')
        self.fixture_git(['add', 'README.md'], self.author)
        self.fixture_git(['commit', '-m', 'fixture baseline'], self.author)
        self.baseline = self.fixture_git(['rev-parse', 'HEAD'], self.author).decode().strip()
        self.fixture_git(['init', '--bare', str(self.remote)])
        self.fixture_git(['remote', 'add', 'origin', str(self.remote)], self.author)
        self.fixture_git(['push', 'origin', 'main'], self.author)
        self.replica = ReplicaFixture()
        self.store = DeliveryStore(self.root / 'metadata' / 'delivery.sqlite3',
                                   local_domain='primary', replica_domain='replica', replica=self.replica)
        self.repo = self.store.enroll_repository('owner', 'enroll-repo', repo_alias='fixture',
                                                 remote_identity='github:example/fixture')['repo_id']
        project = self.store.create_project('owner', 'create-project', project_alias='fixture', name='Fixture')
        self.project = project['project_id']
        self.store.bind_repository('owner', 'bind', project_id=self.project, repo_id=self.repo,
                                    expected_revision=1, access='write')
        self.store.project_control('owner', 'activate', project_id=self.project, expected_revision=2, action='activate')
        self.task = self.store.plan('owner', 'plan', project_id=self.project, expected_project_revision=3,
            conversation_ref='fixture-chat', source_event_ref='fixture-source', requirement='Test local Git', mode='deliver')['task_id']
        self.policy = RepositoryPolicy('github:example/fixture', 'https://git.example.invalid/example/fixture.git')
        self.runtime = LocalGit(self.remote, self.store)
        self.denied = set()
        self.bad_validation = False
        self.changed_validation = False
        self.scan_result = True
        self.tests_result = True
        self.auth_calls = []
        self.manager = self.open()

    def fixture_git(self, arguments, cwd=None, *, content=None):
        process = subprocess.run([shutil.which('git'), '-c', 'core.hooksPath=/dev/null',
            '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', *arguments],
            cwd=cwd or self.root, input=content, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={'PATH': '/usr/bin:/bin', 'HOME': str(self.root), 'GIT_CONFIG_NOSYSTEM': '1',
                 'GIT_CONFIG_GLOBAL': '/dev/null', 'LANG': 'C'}, timeout=5)
        if process.returncode:
            raise AssertionError('local fixture Git failed: ' + process.stderr.decode(errors='replace'))
        return process.stdout

    def authorize(self, context):
        self.auth_calls.append(context)
        if context['action'] in self.denied:
            raise ValueError('fixture denied')
        return AuthorizationReceipt(digest(canonical(context).encode()), 'fixture-grant')

    def validate(self, context):
        return ValidationReceipt(digest(canonical(context).encode()), context['bundle_sha256'],
            context['baseline_oid'], '0' * 40 if self.bad_validation else context['candidate_oid'],
            digest(b'fixture-only qualified scan'),
            digest(b'changed fixture tests' if self.changed_validation else b'fixture-only qualified tests'),
            self.scan_result, self.tests_result)

    def open(self, **changes):
        return RepoManager(self.store, self.root / 'manager',
            **{'policies': (self.policy,), 'git_runtime': self.runtime,
               'authorization_gate': self.authorize, 'validation_gate': self.validate, **changes})

    def restart(self):
        self.store = DeliveryStore(self.store.path, local_domain='primary', replica_domain='replica', replica=self.replica)
        self.runtime.store = self.store
        return self.open()

    def sync_source(self):
        synced = self.manager.sync('owner', 'sync', repo_ref=self.repo, expected_revision=1)
        self.assertEqual(synced['status'], 'accepted')
        self.source = self.manager.export_bundle('owner', 'export', task_ref=self.task,
                                                expected_revision=1, repo_ref=self.repo)
        return self.source

    def make_candidate(self, *, special=None):
        self.fixture_git(['checkout', '-b', 'codex/' + self.task], self.author)
        if special == 'historical-lfs':
            old = self.author / 'removed-lfs.bin'
            old.write_text('version https://git-lfs.github.com/spec/v1\noid sha256:' + '0'*64 + '\nsize 1\n')
            self.fixture_git(['add', '-A'], self.author)
            self.fixture_git(['commit', '-m', 'fixture historical LFS'], self.author)
            old.unlink()
        (self.author / 'README.md').write_text('candidate fixture\n')
        if special == 'symlink':
            (self.author / 'external').symlink_to('/outside-fixture')
        elif special == 'lfs':
            (self.author / 'lfs.bin').write_text('version https://git-lfs.github.com/spec/v1\noid sha256:' + '0'*64 + '\nsize 1\n')
        elif special == 'filter':
            (self.author / '.gitattributes').write_text('*.bin filter=untrusted\n')
        elif special == 'reused-filter':
            (self.author / 'a.txt').write_text('* filter=untrusted\n')
            (self.author / 'sub').mkdir()
            (self.author / 'sub' / '.gitattributes').write_text('* filter=untrusted\n')
        elif special == 'gitmodules':
            (self.author / '.gitmodules').write_text('[submodule "vendor"]\npath=vendor\nurl=https://unregistered.invalid/repo\n')
        self.fixture_git(['add', '-A'], self.author)
        if special == 'submodule':
            self.fixture_git(['update-index', '--add', '--cacheinfo', '160000,' + self.baseline + ',vendor'], self.author)
        self.fixture_git(['commit', '-m', 'fixture candidate'], self.author)
        self.candidate = self.fixture_git(['rev-parse', 'HEAD'], self.author).decode().strip()
        if special == 'orphan':
            tree = self.fixture_git(['rev-parse', 'HEAD^{tree}'], self.author).decode().strip()
            self.candidate = self.fixture_git(['commit-tree', tree], self.author,
                                              content=b'fixture unrelated root\n').decode().strip()
            self.fixture_git(['update-ref', 'refs/heads/codex/' + self.task, self.candidate], self.author)
        path = self.root / 'runner-candidate.bundle'
        self.fixture_git(['bundle', 'create', str(path), 'refs/heads/codex/' + self.task,
                          *(['main'] if special == 'extra-ref' else []),
                          *(['^' + self.baseline] if special == 'thin' else [])], self.author)
        self.bundle = path.read_bytes()
        staged = self.manager.stage_bundle('owner', 'stage', task_ref=self.task, expected_revision=1,
            repo_ref=self.repo, content=self.bundle, expected_digest=digest(self.bundle))
        self.bundle_ref = staged['artifact_ref']
        return self.candidate

    def imported(self, **changes):
        return self.manager.import_candidate('owner', 'import',
            **{'task_ref': self.task, 'expected_revision': 1, 'repo_ref': self.repo,
               'bundle_ref': self.bundle_ref, 'expected_digest': digest(self.bundle),
               'candidate_oid': self.candidate, **changes})

    def prepared(self):
        self.sync_source()
        self.make_candidate()
        candidate = self.imported()
        self.plan = self.manager.prepare_publish('owner', 'prepare', task_ref=self.task, expected_revision=1,
            repo_ref=self.repo, candidate_ref=candidate['candidate_ref'], target_alias='integration')
        return self.plan['publication_ref']

    def remote_oid(self):
        return self.fixture_git(['--git-dir=' + str(self.remote), 'rev-parse', 'refs/heads/main']).decode().strip()

    def advance_remote(self):
        tree = self.fixture_git(['--git-dir=' + str(self.remote), 'rev-parse', self.baseline + '^{tree}']).decode().strip()
        other = self.fixture_git(['--git-dir=' + str(self.remote), 'commit-tree', tree, '-p', self.baseline],
                                  content=b'fixture independent advance\n').decode().strip()
        self.fixture_git(['--git-dir=' + str(self.remote), 'update-ref', 'refs/heads/main', other, self.baseline])
        return other

    def error(self, code, action):
        with self.assertRaises(DeliveryError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_real_bare_sync_bundle_candidate_publish_and_events_single_task_source(self):
        publication = self.prepared()
        result = self.manager.publish('owner', 'publish', publication_ref=publication)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(self.remote_oid(), self.candidate)
        self.assertEqual(self.runtime.pushes, 1)
        self.assertEqual(self.manager.publication_status('owner', publication)['durability'], 'replicated')
        self.manager.publish('owner', 'publish', publication_ref=publication)
        self.open().publish('owner', 'different-command', publication_ref=publication)
        self.assertEqual(self.runtime.pushes, 1)
        self.assertFalse(any(path.name == '.git' for path in (self.root / 'manager').rglob('.git')))
        self.assertFalse(any(command[0] in {'checkout', 'switch', 'reset', 'stash', 'clean',
                              'pull', 'merge', 'rebase', 'gc', 'prune', 'submodule'} for command in self.runtime.commands))
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)
            self.assertGreaterEqual(connection.execute("SELECT count(*) FROM events WHERE kind LIKE 'repository.%'").fetchone()[0], 8)
        self.assertGreater(self.store.verify_event_chain(), 0)

    def test_lost_push_response_restart_only_ls_remote_confirms_no_second_push(self):
        publication = self.prepared()
        self.runtime.failure = 'after'
        result = self.manager.publish('owner', 'publish', publication_ref=publication)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(self.remote_oid(), self.candidate)
        restarted = self.restart()
        restarted.publish('owner', 'never-replay', publication_ref=publication)
        self.assertEqual(self.runtime.pushes, 1)
        reads = self.runtime.reads
        result = restarted.reconcile_publish('owner', 'reconcile', publication_ref=publication)
        self.assertEqual(result['state'], 'confirmed')
        self.assertGreater(self.runtime.reads, reads)
        self.assertEqual(self.runtime.pushes, 1)
        reads = self.runtime.reads
        self.assertEqual(restarted.reconcile_publish('owner', 'reconcile', publication_ref=publication), result)
        self.assertEqual(self.runtime.reads, reads)

    def test_parent_crash_after_push_leaves_sending_and_restart_never_repushes(self):
        publication = self.prepared()
        self.runtime.failure = 'crash'
        with self.assertRaises(SystemExit):
            self.manager.publish('owner', 'publish', publication_ref=publication)
        restarted = self.restart()
        self.assertEqual(restarted.publication_status('owner', publication)['state'], 'unknown')
        restarted.publish('owner', 'new-command', publication_ref=publication)
        self.assertEqual(self.runtime.pushes, 1)
        self.assertEqual(restarted.reconcile_publish('owner', 'recover', publication_ref=publication)['state'], 'confirmed')

    def test_unknown_still_baseline_never_repushes_or_allows_new_plan(self):
        publication = self.prepared()
        self.runtime.failure = 'before'
        self.assertEqual(self.manager.publish('owner', 'publish', publication_ref=publication)['state'], 'unknown')
        self.assertEqual(self.remote_oid(), self.baseline)
        restarted = self.restart()
        self.assertEqual(restarted.reconcile_publish('owner', 'recover', publication_ref=publication)['state'], 'unknown')
        restarted.publish('owner', 'retry-forbidden', publication_ref=publication)
        self.assertEqual(self.runtime.pushes, 1)
        with closing(self.store._connect()) as connection:
            candidate = connection.execute('SELECT candidate_ref FROM repository_candidates').fetchone()[0]
        self.error('repository_publish_unknown', lambda: restarted.prepare_publish('owner', 'new-plan',
            task_ref=self.task, expected_revision=1, repo_ref=self.repo, candidate_ref=candidate, target_alias='integration'))

    def test_unknown_other_remote_oid_is_conflict_preserving_refs(self):
        publication = self.prepared()
        self.runtime.failure = 'before'
        self.manager.publish('owner', 'publish', publication_ref=publication)
        other = self.advance_remote()
        result = self.open().reconcile_publish('owner', 'reconcile', publication_ref=publication)
        self.assertEqual(result['state'], 'conflict')
        self.assertEqual(result['remote_oid'], other)
        self.assertEqual(self.runtime.pushes, 1)
        mirror = self.root / 'manager' / 'repositories' / self.repo / 'mirror.git'
        refs = self.fixture_git(['--git-dir=' + str(mirror), 'for-each-ref']).decode()
        self.assertIn(self.baseline, refs)
        self.assertIn(self.candidate, refs)

    def test_claim_replica_pending_cannot_push_and_restart_is_read_only(self):
        publication = self.prepared()
        self.replica.available = False
        result = self.manager.publish('owner', 'publish', publication_ref=publication)
        self.assertEqual(result['status'], 'durability_pending')
        self.assertEqual(self.runtime.pushes, 0)
        self.assertEqual(self.manager.publication_status('owner', publication)['state'], 'unknown')
        self.replica.available = True
        self.store.reconcile(result['event_id'])
        restarted = self.restart()
        restarted.publish('owner', 'not-a-new-claim', publication_ref=publication)
        result = restarted.reconcile_publish('owner', 'recover', publication_ref=publication)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(self.runtime.pushes, 0)

    def test_concurrent_publisher_has_single_actual_push(self):
        publication = self.prepared()
        self.runtime.started, self.runtime.release = threading.Event(), threading.Event()
        errors = []
        def publish():
            try:
                self.manager.publish('owner', 'publish', publication_ref=publication)
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=publish)
        thread.start()
        self.assertTrue(self.runtime.started.wait(2))
        result = self.open().publish('owner', 'concurrent', publication_ref=publication)
        self.assertEqual(result['state'], 'unknown')
        self.runtime.release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.runtime.pushes, 1)

    def test_baseline_moves_after_prepare_refuses_before_claim_or_push(self):
        publication = self.prepared()
        self.advance_remote()
        self.error('repository_baseline_drift', lambda: self.manager.publish('owner', 'publish', publication_ref=publication))
        self.assertEqual(self.runtime.pushes, 0)
        self.assertEqual(self.manager.publication_status('owner', publication)['state'], 'prepared')

    def test_revoked_authorization_after_claim_no_push_unknown(self):
        publication = self.prepared()
        count = 0
        def gate(context):
            nonlocal count
            if context['action'] == 'publish':
                count += 1
                if count == 2:
                    raise ValueError('revoked after durable claim')
            return self.authorize(context)
        self.manager.authorization_gate = gate
        result = self.manager.publish('owner', 'publish', publication_ref=publication)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(self.runtime.pushes, 0)

    def test_claim_records_exact_grant_and_changed_grant_never_pushes(self):
        publication = self.prepared()
        count = 0
        def gate(context):
            nonlocal count
            if context['action'] == 'publish':
                count += 1
            return AuthorizationReceipt(digest(canonical(context).encode()),
                                        'initial-fixture-grant' if count == 1 else 'changed-fixture-grant')
        self.manager.authorization_gate = gate
        self.assertEqual(self.manager.publish('owner', 'publish', publication_ref=publication)['state'], 'unknown')
        self.assertEqual(self.runtime.pushes, 0)
        with closing(self.store._connect()) as connection:
            body = json.loads(connection.execute("SELECT body_json FROM events WHERE kind='repository.publish.claim'").fetchone()[0])
        self.assertEqual(body['arguments']['authorization']['grant_ref'], 'initial-fixture-grant')

    def test_policy_changes_during_claim_replication_cannot_use_old_remote_authority(self):
        publication = self.prepared()
        save = self.replica.save
        def rotate(snapshot):
            receipt = save(snapshot)
            with closing(self.store._connect()) as connection:
                claimed = connection.execute("SELECT 1 FROM repository_publications WHERE state='sending'").fetchone()
            if claimed:
                self.manager.policies = {self.policy.remote_identity: replace(self.policy, revision=2)}
            return receipt
        self.replica.save = rotate
        result = self.manager.publish('owner', 'publish', publication_ref=publication)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(self.runtime.pushes, 0)

    def test_changed_scan_or_test_receipt_cannot_claim(self):
        publication = self.prepared()
        self.changed_validation = True
        self.error('repository_validation_changed', lambda: self.manager.publish('owner', 'publish', publication_ref=publication))
        self.assertEqual(self.runtime.pushes, 0)

    def test_wrong_bundle_candidate_gate_binding_rejected(self):
        self.sync_source()
        self.make_candidate()
        self.bad_validation = True
        self.error('repository_validation_failed', self.imported)
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM repository_candidates').fetchone()[0], 0)

    def test_failed_scan_or_tests_and_non_boolean_decisions_cannot_import(self):
        self.sync_source()
        self.make_candidate()
        for scan, tests in [(False, True), (True, False), (1, True), (True, 'passed')]:
            self.scan_result, self.tests_result = scan, tests
            self.error('repository_validation_failed', self.imported)

    def unsafe_candidate(self, special, code):
        self.sync_source()
        self.make_candidate(special=special)
        self.error(code, self.imported)
        self.assertEqual(self.runtime.pushes, 0)

    def test_external_symlink_bundle_rejected(self):
        self.unsafe_candidate('symlink', 'repository_recursive_content_unsupported')

    def test_gitlink_bundle_rejected(self):
        self.unsafe_candidate('submodule', 'repository_recursive_content_unsupported')

    def test_gitmodules_bundle_rejected(self):
        self.unsafe_candidate('gitmodules', 'repository_recursive_content_unsupported')

    def test_lfs_bundle_rejected(self):
        self.unsafe_candidate('lfs', 'repository_lfs_unsupported')

    def test_filter_bundle_rejected(self):
        self.unsafe_candidate('filter', 'repository_filters_unsupported')

    def test_filter_attributes_reusing_other_path_blob_still_rejected(self):
        self.unsafe_candidate('reused-filter', 'repository_filters_unsupported')

    def test_removed_historical_lfs_still_rejected_before_exporting_history(self):
        self.unsafe_candidate('historical-lfs', 'repository_lfs_unsupported')

    def test_thin_bundle_prerequisites_satisfied_only_by_frozen_source(self):
        self.sync_source()
        self.make_candidate(special='thin')
        self.assertEqual(self.imported()['candidate_oid'], self.candidate)

    def test_paused_task_and_dangling_mirror_symlink_cannot_write(self):
        publication = self.prepared()
        with self.store._transaction() as connection:
            connection.execute("UPDATE tasks SET state='paused' WHERE task_id=?", (self.task,))
        self.error('repository_task_inactive', lambda: self.manager.publish('owner', 'publish', publication_ref=publication))
        self.assertEqual(self.runtime.pushes, 0)
        other = self.store.enroll_repository('owner', 'other-repo', repo_alias='other', remote_identity='github:example/other')['repo_id']
        policy = RepositoryPolicy('github:example/other', 'https://git.example.invalid/example/other.git')
        manager = self.open(policies=(self.policy, policy))
        directory = manager.root / 'repositories' / other
        directory.mkdir(mode=0o700)
        symlink = directory / 'mirror.git'
        symlink.symlink_to(self.root / 'missing-user-target')
        self.error('repository_mirror_invalid', lambda: manager.sync('owner', 'other-sync', repo_ref=other, expected_revision=1))
        self.assertTrue(symlink.is_symlink())

    def test_unrelated_root_candidate_rejected(self):
        self.unsafe_candidate('orphan', 'repository_candidate_not_descendant')

    def test_extra_bundle_head_rejected(self):
        self.unsafe_candidate('extra-ref', 'repository_bundle_refs_invalid')

    def test_fetch_remote_race_keeps_last_known_baseline_and_all_retained_refs(self):
        self.sync_source()
        self.runtime.after_fetch = self.advance_remote
        self.error('repository_baseline_drift', lambda: self.manager.sync('owner', 'race-sync', repo_ref=self.repo, expected_revision=1))
        with closing(self.store._connect()) as connection:
            row = connection.execute('SELECT baseline_oid FROM repository_mirrors WHERE repo_id=?', (self.repo,)).fetchone()
        self.assertEqual(row['baseline_oid'], self.baseline)
        mirror = self.root / 'manager' / 'repositories' / self.repo / 'mirror.git'
        refs = self.fixture_git(['--git-dir=' + str(mirror), 'for-each-ref']).decode()
        self.assertEqual(refs.count('refs/codex/fetch/'), 2)

    def test_foreign_owner_path_scope_revision_and_recovery_fail_before_git(self):
        self.error('not_found', lambda: self.manager.export_bundle('other', 'export', task_ref=self.task, expected_revision=1, repo_ref=self.repo))
        self.error('revision_conflict', lambda: self.manager.export_bundle('owner', 'export', task_ref=self.task, expected_revision=2, repo_ref=self.repo))
        with self.store._transaction() as connection:
            connection.execute("UPDATE task_repositories SET paths_json='[\"allowed\"]' WHERE task_id=?", (self.task,))
        self.error('path_isolation_unavailable', lambda: self.manager.export_bundle('owner', 'export', task_ref=self.task, expected_revision=1, repo_ref=self.repo))
        self.store.recovery_mode = True
        self.error('recovery_read_only', lambda: self.manager.sync('owner', 'sync', repo_ref=self.repo, expected_revision=1))
        self.assertEqual(self.runtime.fetches, 0)

    def test_digest_ref_and_artifact_symlink_tampering_rejected(self):
        self.sync_source()
        self.make_candidate()
        self.error('repository_artifact_invalid', lambda: self.imported(expected_digest=digest(b'wrong')))
        self.error('repository_bundle_refs_invalid', lambda: self.imported(candidate_oid=self.baseline))
        path = self.manager._artifact_path(digest(self.bundle))
        path.unlink()
        outside = self.root / 'outside-bundle'
        outside.write_bytes(self.bundle)
        outside.chmod(0o600)
        path.symlink_to(outside)
        self.error('repository_storage_unavailable', self.imported)
        self.assertEqual(outside.read_bytes(), self.bundle)

    def test_dirty_untracked_and_old_refs_survive_sync_and_publish(self):
        publication = self.prepared()
        (self.author / 'README.md').write_text('dirty user content\n')
        (self.author / 'untracked.txt').write_text('untracked user content\n')
        self.manager.publish('owner', 'publish', publication_ref=publication)
        self.manager.sync('owner', 'sync-again', repo_ref=self.repo, expected_revision=1)
        self.assertEqual((self.author / 'README.md').read_text(), 'dirty user content\n')
        self.assertEqual((self.author / 'untracked.txt').read_text(), 'untracked user content\n')
        mirror = self.root / 'manager' / 'repositories' / self.repo / 'mirror.git'
        refs = self.fixture_git(['--git-dir=' + str(mirror), 'for-each-ref']).decode()
        self.assertIn(self.baseline, refs)
        self.assertIn(self.candidate, refs)
        self.assertEqual(refs.count('refs/codex/fetch/'), 2)

    def test_global_hooks_helpers_environment_and_config_drift_fail_closed(self):
        publication = self.prepared()
        marker = self.root / 'hook-was-executed'
        mirror = self.root / 'manager' / 'repositories' / self.repo / 'mirror.git'
        hook = mirror / 'hooks' / 'pre-push'
        hook.parent.mkdir(exist_ok=True)
        hook.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n')
        hook.chmod(0o700)
        poisoned = self.root / 'poisoned.gitconfig'
        poisoned.write_text('[core]\n hooksPath=' + str(hook.parent) + '\n[credential]\n helper=!touch ' + str(marker) + '\n')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(poisoned), 'HOME': str(self.root),
                                     'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.hooksPath',
                                     'GIT_CONFIG_VALUE_0': str(hook.parent), 'MACHINE_TOKEN': 'fixture-not-inherited'}):
            self.manager.publish('owner', 'publish', publication_ref=publication)
        self.assertFalse(marker.exists())
        config = mirror / 'config'
        with config.open('a') as output:
            output.write('\n[url "ssh://unregistered.invalid/"]\n insteadOf=https://git.example.invalid/\n')
        self.error('repository_mirror_config_drift', lambda: self.manager.sync('owner', 'sync-drift', repo_ref=self.repo, expected_revision=1))

    def test_mandatory_gates_production_https_and_migration_transaction(self):
        self.error('repository_manager_invalid', lambda: self.open(authorization_gate=None))
        self.error('repository_manager_invalid', lambda: self.open(validation_gate=None))
        for url in ['file:///tmp/repo', 'ssh://host/repo', 'http://host/repo',
                    'https://user:fixture@host/repo', 'https://host/repo?token=fixture',
                    'https://host/repo#fragment', 'https://host/../repo']:
            self.error('repository_policy_invalid', lambda u=url: replace(self.policy, https_url=u))
        with closing(self.store._connect()) as connection:
            self.error('repository_migration_transaction_required', lambda: install_repository_schema(connection))

    def test_publication_identity_and_unknown_claim_cannot_be_rewritten_or_deleted(self):
        publication = self.prepared()
        self.runtime.failure = 'before'
        self.manager.publish('owner', 'publish', publication_ref=publication)
        for statement in ["UPDATE repository_publications SET state='prepared' WHERE publication_ref=?",
                          "UPDATE repository_publications SET destination_ref='refs/heads/other' WHERE publication_ref=?",
                          'DELETE FROM repository_publications WHERE publication_ref=?']:
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store._transaction() as connection:
                    connection.execute(statement, (publication,))
        self.assertEqual(self.manager.publication_status('owner', publication)['state'], 'unknown')

    def test_no_implicit_netrc_or_hooks_and_wrong_authorization_context(self):
        self.manager.authorization_gate = lambda _: AuthorizationReceipt(digest(b'wrong-context'), 'fixture-grant')
        self.error('repository_authorization_denied', lambda: self.manager.sync('owner', 'sync', repo_ref=self.repo, expected_revision=1))
        self.assertEqual(self.runtime.fetches, 0)
        self.manager.authorization_gate = self.authorize
        (self.manager.home / '.netrc').write_text('test-only empty credential fixture\n')
        self.error('repository_runtime_drift', lambda: self.manager.sync('owner', 'sync', repo_ref=self.repo, expected_revision=1))

    def test_output_and_timeout_budgets_clean_process_environment(self):
        executable = self.root / 'trusted-fixture-git'
        executable.write_text('#!/bin/sh\n/usr/bin/head -c 4096 /dev/zero\n')
        executable.chmod(0o700)
        runtime = GitRuntime(str(executable), max_output=1024, timeout_seconds=2)
        with self.manager._locked(self.repo) as (_, lockfd):
            self.error('repository_output_limit', lambda: runtime.run(self.manager.home, None, ['version'], runtime.budget(), lockfd=lockfd))
        executable.write_text('#!/bin/sh\n/bin/sleep 2\n')
        runtime = GitRuntime(str(executable), max_output=1024, timeout_seconds=0.2)
        started = time.monotonic()
        with self.manager._locked(self.repo) as (_, lockfd):
            self.error('repository_git_timeout', lambda: runtime.run(self.manager.home, None, ['version'], runtime.budget(), lockfd=lockfd))
        self.assertLess(time.monotonic() - started, 0.55)
        runtime = self.runtime
        original = subprocess.Popen
        calls = []
        def observed(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)
        with patch.dict(os.environ, {'MACHINE_TOKEN': 'fixture-not-inherited', 'GIT_SSH_COMMAND': 'untrusted'}), \
                patch('codex_delivery.codex_delivery.repo_manager.subprocess.Popen', observed):
            with self.manager._locked(self.repo) as (_, lockfd):
                runtime.run(self.manager.home, None, ['version'], runtime.budget(), lockfd=lockfd)
        environment = calls[0][1]['env']
        self.assertNotIn('MACHINE_TOKEN', environment)
        self.assertNotIn('GIT_SSH_COMMAND', environment)
        self.assertEqual(environment['GIT_CONFIG_GLOBAL'], '/dev/null')
        self.assertEqual(environment['GIT_TERMINAL_PROMPT'], '0')


if __name__ == '__main__':
    unittest.main()
