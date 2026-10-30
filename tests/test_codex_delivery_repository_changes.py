"""Real Git candidates from bounded guest data, without host project execution."""
from contextlib import closing
import io
import json
import stat
import unittest
from unittest.mock import patch
import zipfile

import test_codex_delivery_repo_manager as fixture
from codex_delivery.codex_delivery.durability import digest
from codex_delivery.codex_delivery.repository_changes import parse_changes
from codex_delivery.codex_delivery.store import DeliveryError, canonical
from codex_delivery.codex_delivery.work import WorkStore


def change_package(repo,baseline,files):
    changes=[];payloads={}
    for index,(name,value) in enumerate(sorted(files.items())):
        if value is None:
            changes.append(dict(path=name,operation='delete'))
            continue
        if isinstance(value,tuple):mode,raw=value
        else:mode,raw=0o644,value
        entry='files/'+str(index)
        payloads[entry]=raw
        changes.append(dict(path=name,operation='write',mode=mode,entry=entry,sha256=digest(raw)))
    document=dict(version=1,repo_ref=repo,baseline_oid=baseline,changes=changes)
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('changes.json',canonical(document).encode())
        for name,raw in payloads.items():archive.writestr(name,raw)
    return output.getvalue()


class ChangeTests(unittest.TestCase):
    fixture_git=fixture.RepoManagerTests.fixture_git
    authorize=fixture.RepoManagerTests.authorize
    validate=fixture.RepoManagerTests.validate
    open=fixture.RepoManagerTests.open
    sync_source=fixture.RepoManagerTests.sync_source
    restart=fixture.RepoManagerTests.restart

    def setUp(self):
        fixture.RepoManagerTests.setUp(self)
        self.sync_source()

    def package(self,files):return change_package(self.repo,self.source['baseline_oid'],files)

    def assemble(self,content,key='assemble'):
        return self.manager.assemble_candidate('owner',key,task_ref=self.task,expected_revision=1,
            repo_ref=self.repo,content=content,expected_digest=digest(content))

    def import_result(self,result):
        return self.manager.import_candidate('owner','import-assembled',task_ref=self.task,expected_revision=1,
            repo_ref=self.repo,bundle_ref=result['bundle_ref'],expected_digest=result['bundle_sha256'],
            candidate_oid=result['candidate_oid'])

    def test_new_code_and_changes_compile_to_real_descendant_import_and_one_publisher_push(self):
        marker=self.root/'must-not-execute'
        content=self.package({'README.md':b'changed from guest\n','new/application.py':b'print(42)\n',
            'run.sh':(0o755,('#!/bin/sh\ntouch '+str(marker)+'\n').encode())})
        result=self.assemble(content)
        self.assertEqual('accepted',result['status'])
        self.assertEqual(self.source['baseline_oid'],result['baseline_oid'])
        self.assertFalse(marker.exists())
        candidate=self.import_result(result)
        prepared=self.manager.prepare_publish('owner','prepare-assembled',task_ref=self.task,expected_revision=1,
            repo_ref=self.repo,candidate_ref=candidate['candidate_ref'],target_alias='integration')
        published=self.manager.publish('owner','publish-assembled',publication_ref=prepared['publication_ref'])
        self.assertEqual(published['state'],'confirmed')
        self.assertEqual(self.runtime.pushes,1)
        self.assertEqual(self.fixture_git(['rev-parse','refs/heads/main'],self.remote).decode().strip(),result['candidate_oid'])
        self.assertEqual(self.fixture_git(['show','refs/heads/main:new/application.py'],self.remote),b'print(42)\n')
        self.assertEqual(self.fixture_git(['show','refs/heads/main:README.md'],self.remote),b'changed from guest\n')
        self.assertFalse(marker.exists())
        self.assertEqual((self.author/'README.md').read_bytes(),b'baseline fixture\n')
        self.assertFalse((self.author/'new').exists())

    def test_original_baseline_deletion_and_nested_binary_files_have_exact_git_tree(self):
        content=self.package({'README.md':None,'nested/value.bin':b'\x00\x01\xff','empty':b''})
        result=self.assemble(content)
        self.import_result(result)
        mirror=self.manager.root/'repositories'/self.repo/'mirror.git'
        names=self.fixture_git(['ls-tree','-r','--name-only',result['candidate_oid']],mirror).decode().splitlines()
        self.assertEqual(names,['empty','nested/value.bin'])
        self.assertEqual(self.fixture_git(['show',result['candidate_oid']+':nested/value.bin'],mirror),b'\x00\x01\xff')

    def test_idempotent_retry_and_restart_reuse_original_candidate_without_git_or_bundle_rewrite(self):
        content=self.package({'new.py':b'print(42)'})
        original=self.assemble(content)
        self.manager=self.restart()
        with patch.object(self.runtime,'run',side_effect=AssertionError('no repeat assembly')):
            self.assertEqual(self.assemble(content),original)
        with self.assertRaisesRegex(DeliveryError,'idempotency_conflict'):
            self.assemble(self.package({'new.py':b'changed'}))

    def test_revision_owner_scope_and_work_mode_cannot_authorize_candidate_writes(self):
        content=self.package({'new.py':b'print(42)'})
        with self.assertRaises(DeliveryError):
            self.manager.assemble_candidate('other','assemble',task_ref=self.task,expected_revision=1,
                repo_ref=self.repo,content=content,expected_digest=digest(content))
        store=WorkStore(self.store.path,local_domain='primary',replica_domain='replica',replica=self.replica)
        self.manager.store=self.runtime.store=store
        work=store.create_work('owner','ordinary-work',conversation_ref='chat-work',source_event_ref='source-work',
            input_revision=1,requirement='Read code',project_id=self.project,expected_project_revision=3,
            spec=dict(profile='code',runtime_id='fixture-python',program='print(42)',output_names=['report.txt'],input_refs=[]))
        with self.assertRaisesRegex(DeliveryError,'repository_development_mode_required'):
            self.manager.assemble_candidate('owner','unapproved-work-write',task_ref=work['task_id'],expected_revision=1,
                repo_ref=self.repo,content=content,expected_digest=digest(content))
        self.assertEqual(self.runtime.pushes,0)

    def test_foreign_baseline_zip_path_collision_unsupported_git_and_wrong_digest_are_rejected(self):
        bad=[change_package(self.repo,'0'*40,{'a':b'bad'}),
            change_package('foreign-repo',self.source['baseline_oid'],{'a':b'bad'}),
            self.package({'../escape':b'bad'}),self.package({'.git/config':b'bad'}),
            self.package({'a':b'one','a/b':b'two'}),self.package({'README.md/more':b'bad'}),
            self.package({'missing':None}),self.package({'.gitmodules':b'[submodule "x"]'}),
            self.package({'large-lfs':b'version https://git-lfs.github.com/spec/v1\nsize 1\n'}),
            self.package({'.gitattributes':b'* filter=untrusted\n'})]
        for index,content in enumerate(bad):
            with self.subTest(index=index),self.assertRaises(DeliveryError):
                self.assemble(content,key='bad-'+str(index))
        with self.assertRaisesRegex(DeliveryError,'repository_changes_invalid'):
            self.manager.assemble_candidate('owner','digest-mismatch',task_ref=self.task,expected_revision=1,
                repo_ref=self.repo,content=self.package({'a':b'ok'}),expected_digest=digest(b'other'))
        self.assertEqual(self.runtime.pushes,0)

    def test_independent_validation_failure_blocks_import_and_never_implicitly_publishes(self):
        result=self.assemble(self.package({'new.py':b'print(42)'}))
        self.tests_result=False
        with self.assertRaisesRegex(DeliveryError,'repository_validation_failed'):
            self.import_result(result)
        self.assertEqual(self.runtime.pushes,0)
        with closing(self.store._connect()) as connection:
            self.assertEqual(0,connection.execute('SELECT COUNT(*) FROM repository_publications').fetchone()[0])


if __name__=='__main__':unittest.main()
