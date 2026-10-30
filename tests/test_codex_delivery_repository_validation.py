"""Actual disposable bare Git and source bytes; no remote or VM acceptance."""
from contextlib import closing
from dataclasses import replace
import io
import tarfile
import unittest

import test_codex_delivery_repo_manager as fixture
from codex_delivery.codex_delivery.durability import digest
from codex_delivery.codex_delivery.repository_validation import (
    CandidateReader, RepositoryValidationGate, SourceScanner,
)
from codex_delivery.codex_delivery.store import DeliveryError,canonical


class LocalRepositoryFixture(unittest.TestCase):
    setUp=fixture.RepoManagerTests.setUp
    fixture_git=fixture.RepoManagerTests.fixture_git
    open=fixture.RepoManagerTests.open
    authorize=fixture.RepoManagerTests.authorize
    validate=fixture.RepoManagerTests.validate
    sync_source=fixture.RepoManagerTests.sync_source
    make_candidate=fixture.RepoManagerTests.make_candidate
    imported=fixture.RepoManagerTests.imported
    error=fixture.RepoManagerTests.error

    def validation_context(self):
        context,_=self.manager._context('owner',self.task,1,self.repo,write=True)
        return dict(context,bundle_sha256=digest(self.bundle),baseline_oid=self.baseline,candidate_oid=self.candidate)


class RepositoryValidationTests(LocalRepositoryFixture):
    def test_actual_bundle_tracked_tip_and_history_are_scanned(self):
        self.sync_source();self.make_candidate()
        (self.author/'untracked.txt').write_text('untracked fixture must not enter view')
        scanner=SourceScanner();view=CandidateReader(self.manager,scanner).read(self.validation_context())
        self.assertEqual(dict(view.files),{'README.md':b'candidate fixture\n'})
        with tarfile.open(fileobj=io.BytesIO(view.package())) as archive:
            self.assertEqual(archive.getnames(),['README.md'])
            self.assertEqual(archive.extractfile('README.md').read(),b'candidate fixture\n')
        with self.assertRaises(TypeError):view.files['other']=b'changed'

    def test_removed_historical_secret_still_rejects_candidate(self):
        self.sync_source()
        (self.author/'removed.txt').write_text('-----BEGIN PRIVATE KEY-----\nfixture\n')
        self.fixture_git(['add','removed.txt'],self.author)
        self.fixture_git(['commit','-m','fixture historical secret'],self.author)
        (self.author/'removed.txt').unlink()
        self.make_candidate()
        self.error('repository_scan_rejected',lambda:CandidateReader(self.manager,SourceScanner()).read(self.validation_context()))

    def test_scanner_rejects_binary_sensitive_paths_and_known_patterns(self):
        scanner=SourceScanner()
        for files in ({'.env':b'normal'}, {'README.md':b'\xff'}, {'a':b'\0'},
                {'a':b'password="fixture-long-value"'}, {'a':b'ghp_'+b'a'*30}):
            with self.subTest(paths=list(files)):
                with self.assertRaises(DeliveryError):scanner.scan(files)

    def test_validation_gate_binds_same_candidate_and_scan_to_deferred_tests(self):
        self.sync_source();self.make_candidate();context=self.validation_context()
        class Tests:
            def request(port,actual,view):
                self.assertEqual(actual,context);self.assertEqual(dict(view.files),{'README.md':b'candidate fixture\n'})
                return digest(b'fixed actual test receipt fixture')
        gate=RepositoryValidationGate(CandidateReader(self.manager,SourceScanner()),Tests())
        receipt=gate(context)
        self.assertEqual(receipt.context_digest,digest(canonical(context).encode()))
        self.assertEqual(receipt.bundle_sha256,digest(self.bundle))
        self.assertTrue(receipt.tests_passed)

    def test_pending_preflight_import_never_allocates_quarantine_or_new_identity(self):
        self.sync_source();self.make_candidate()
        calls=[]
        def pending(context):
            calls.append(context)
            raise DeliveryError('repository_tests_pending')
        self.manager.validation_gate=pending
        for _ in range(3):self.error('repository_validation_failed',self.imported)
        self.assertEqual(len(calls),3)
        self.assertEqual(list(self.manager.root.rglob('quarantine-*')),[])
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM repository_candidates').fetchone()[0],0)

    def test_old_synchronous_gate_remains_compatible_and_is_rechecked(self):
        self.sync_source();self.make_candidate();calls=[]
        def validate(context):
            calls.append(context);return self.validate(context)
        self.manager.validation_gate=validate
        self.assertEqual(self.imported()['status'],'accepted')
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0],calls[1])

    def test_changed_validation_cannot_be_committed_after_git_checks(self):
        self.sync_source();self.make_candidate();calls=[]
        def validate(context):
            calls.append(context);value=self.validate(context)
            return value if len(calls)==1 else replace(value,tests_digest=digest(b'changed tests'))
        self.manager.validation_gate=validate
        self.error('repository_validation_changed',self.imported)
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM repository_candidates').fetchone()[0],0)

    def test_bundle_drift_is_rejected_before_test_or_quarantine(self):
        self.sync_source();self.make_candidate()
        self.manager._artifact_path(digest(self.bundle)).write_bytes(b'changed fixture bundle')
        self.error('repository_artifact_changed',self.imported)
        self.assertEqual(list(self.manager.root.rglob('quarantine-*')),[])

    def test_source_revision_change_during_preflight_cannot_commit_candidate(self):
        self.sync_source();self.make_candidate()
        def validate(context):
            receipt=self.validate(context)
            self.store.control('owner','fixture-pause-source',project_id=self.project,task_id=self.task,
                expected_revision=1,action='pause')
            return receipt
        self.manager.validation_gate=validate
        self.error('revision_conflict',self.imported)
        count=len(list(self.manager.root.rglob('quarantine-*')))
        self.error('revision_conflict',self.imported)
        self.assertEqual(len(list(self.manager.root.rglob('quarantine-*'))),count)
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM repository_candidates').fetchone()[0],0)


if __name__=='__main__':unittest.main()
