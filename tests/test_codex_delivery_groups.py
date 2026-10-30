"""Local historical group facts and real Store durability; no release qualification.

Trusted fixtures write original-shaped events through Store._mutation. They do
not build, push or deploy and never serve as production qualification evidence.
"""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_delivery.codex_delivery.delivery_groups import DeliveryGroups
from codex_delivery.codex_delivery.durability import Snapshot, ReplicaReceipt, digest
from codex_delivery.codex_delivery.store import (
    DeliveryError, DeliveryStore, SCHEMA_VERSION, canonical, _install_group_schema,
)
from codex_delivery.codex_delivery.work import WorkStore
from test_codex_delivery_store import ReplicaFixture


class GroupTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name); self.path=self.root/'delivery.sqlite3'
        self.replica=ReplicaFixture(); self.store=self.open(); self.groups=DeliveryGroups(self.store)
        self.counter=0
        self.members=[self.member('alpha'),self.member('beta')]

    def open(self,**kwargs):
        return DeliveryStore(self.path,local_domain='primary',replica_domain='replica',replica=self.replica,**kwargs)

    def mutate(self,kind,args,result,apply=None,key=None):
        self.counter+=1
        return self.store._mutation('owner',key or 'fixture-'+str(self.counter),kind,args,
            apply or (lambda c,timestamp:result))

    def member(self,name,identity_kind='docker_image_id'):
        # Real project/task creation plus synthetic, explicit trusted event producer.
        repo=self.store.enroll_repository('owner','enroll-'+name,repo_alias=name,remote_identity='github:example/'+name)['repo_id']
        project=self.store.create_project('owner','project-'+name,project_alias=name,name=name)['project_id']
        self.store.bind_repository('owner','bind-'+name,project_id=project,repo_id=repo,expected_revision=1,access='write')
        self.store.project_control('owner','active-'+name,project_id=project,expected_revision=2,action='activate')
        task=self.store.plan('owner','task-'+name,project_id=project,expected_project_revision=3,
            conversation_ref='fixture-conversation',source_event_ref='source-'+name,requirement='fixture only',mode='deliver')['task_id']
        sha=digest(name.encode()); oid=sha[7:47]; candidate='candidate-'+name; pub='publication-'+name
        def prepare(c,timestamp):
            c.execute('INSERT INTO repository_artifacts VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('artifact-'+name,'owner',task,1,repo,'candidate_input',sha,1,'{}',timestamp))
            c.execute('INSERT INTO repository_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (candidate,'owner',task,1,repo,'0'*40,oid,'artifact-'+name,sha,'refs/heads/fixture',sha,'{}',timestamp))
            c.execute('INSERT INTO repository_publications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (pub,'owner',task,1,repo,candidate,'refs/heads/fixture',None,oid,sha,sha,'prepared',None,timestamp,timestamp))
            return dict(task_id=task,repo_id=repo,publication_ref=pub,state='prepared',candidate_oid=oid)
        self.mutate('repository.publish.prepare',dict(task_ref=task,expected_revision=1,repo_ref=repo,
            candidate_ref=candidate,target_alias='fixture'),None,prepare)
        def sending(c,timestamp):
            c.execute("UPDATE repository_publications SET state='sending' WHERE publication_ref=?",(pub,))
            return dict(task_id=task,repo_id=repo,publication_ref=pub,state='sending')
        self.mutate('repository.publish.claim',dict(publication_ref=pub),None,sending)
        def confirmed(c,timestamp):
            c.execute("UPDATE repository_publications SET state='confirmed',remote_oid=? WHERE publication_ref=?",(oid,pub))
            return dict(task_id=task,repo_id=repo,publication_ref=pub,state='confirmed',remote_oid=oid)
        self.mutate('repository.publish.result',dict(publication_ref=pub,state='confirmed',remote_oid=oid),None,confirmed)
        source=dict(repo_id=repo,baseline_oid='0'*40,source_bundle_ref='source-'+name,source_bundle_digest=sha,policy_digest=sha)
        binding=dict(owner_ref='owner',task_ref=task,revision=1,project_ref=project,code_task_ref='code-'+name,
            code_revision=1,code_spec_digest=sha,release_ref='release-'+name,version='1.0.0',target_ref='target-'+name,
            permissions_digest=sha,publish_targets={repo:'fixture'},repositories=[source])
        binding_ref='SB-'+digest(canonical(binding).encode())[7:]
        self.mutate('software.bind',dict(fixture=name),dict(task_id=task,binding_ref=binding_ref,binding=binding))
        request=dict(binding_ref=binding_ref,owner_ref='owner',task_ref=task,revision=1,project_ref=project,
            target_ref=binding['target_ref'],release_ref=binding['release_ref'],version='1.0.0',source_digest=sha,
            permissions_digest=sha,candidates=[dict(source,candidate_ref=candidate,candidate_oid=oid,validation_digest=sha)],archive={})
        identity=dict(kind=identity_kind,digest=sha)
        if identity_kind=='oci_repo_digest':identity['repository']='registry.example.invalid/fixture/canary'
        obs=dict(request_digest=digest(canonical(request).encode()),build_ref='build-'+name,
            artifact_identity=identity,permissions_digest=sha,
            qualification_digest=sha,evidence_digest=sha)
        self.mutate('software.build',dict(request_digest=obs['request_digest']),
            dict(task_id=task,request=request,observation=obs),key='software-build:'+binding_ref)
        release=dict(schema_version=2,release_ref=binding['release_ref'],version='1.0.0',source_digest=sha,
            artifact_identity=obs['artifact_identity'],permissions_digest=sha,qualification_digest=sha)
        self.mutate('software.release',dict(binding_ref=binding_ref,release_digest=digest(canonical(release).encode())),
            dict(task_id=task,binding=binding,request=request,observation=obs,
                 publications=[dict(repo_id=repo,publication_ref=pub)],release=release),key='software-release:'+binding_ref)
        return dict(member_ref=name,project_ref=project,task_ref=task,task_revision=1,
                    release_ref=binding['release_ref'],version='1.0.0',release_digest=digest(canonical(release).encode()),goal='published')

    def create(self,**changes):
        args=dict(members=self.members,edges=[dict(before='alpha',after='beta')],acceptance_ref='acceptance-fixture')
        args.update(changes)
        return self.groups.create('owner','create-group',**args)

    def test_group_uses_original_store_facts_without_false_combination_acceptance(self):
        group=self.create(); view=self.groups.status('owner',group['group_ref'])
        self.assertEqual(view['state'],'awaiting_acceptance'); self.assertFalse(view['combination_accepted'])
        self.assertTrue(all(m['state']=='succeeded' and m['historical_success'] for m in view['members']))
        self.assertTrue(all(m['authorization_state']=='not_evaluated' for m in view['members']))
        self.assertEqual(len(view['members'][0]['facts']),5)
        self.assertEqual(self.create(),group)
        with closing(self.store._connect()) as c:
            event=c.execute("SELECT project_id,task_id FROM events WHERE kind='group.create'").fetchone()
            self.assertEqual(tuple(event),(None,None))

    def test_cross_owner_and_cross_project_task_are_hidden(self):
        group=self.create()['group_ref']
        for method in (self.groups.status,self.groups.events):
            with self.assertRaisesRegex(DeliveryError,'not_found'): method('other',group)
        self.assertEqual(self.groups.list('other')['items'],[])
        members=deepcopy(self.members);members[0]['project_ref']=members[1]['project_ref']
        # Restore a distinct project count so the canonical ownership lookup is exercised.
        members[1]['project_ref']='missing-project'
        with self.assertRaisesRegex(DeliveryError,'not_found'):
            self.groups.create('owner','wrong-project',members=members,edges=[],acceptance_ref='fixture')

    def test_dag_limits_duplicate_and_immutable_members(self):
        for edges in ([dict(before='alpha',after='alpha')],
                      [dict(before='alpha',after='beta'),dict(before='beta',after='alpha')],
                      [dict(before='missing',after='beta')],
                      [dict(before='alpha',after='beta')]*2):
            with self.subTest(edges=edges),self.assertRaisesRegex(DeliveryError,'group_dependency_invalid'):
                self.create(edges=edges)
        for members in (self.members[:1],self.members*17):
            with self.assertRaisesRegex(DeliveryError,'group_arguments_invalid'):self.create(members=members)
        group=self.create()['group_ref']
        with closing(self.store._connect()) as c:
            for sql in ('UPDATE delivery_group_members SET task_revision=2','DELETE FROM delivery_group_members',
                        "UPDATE delivery_group_edges SET before_ref='beta'",'DELETE FROM delivery_group_edges',
                        "UPDATE delivery_groups SET acceptance_ref='replace'",'DELETE FROM delivery_groups',
                        'UPDATE delivery_groups SET sealed=0'):
                with self.assertRaises(sqlite3.IntegrityError):c.execute(sql)
            with self.assertRaisesRegex(sqlite3.IntegrityError,'sealed_group_edges'):
                c.execute('INSERT INTO delivery_group_edges VALUES(?,?,?)',(group,'beta','alpha'))
            with self.assertRaisesRegex(sqlite3.IntegrityError,'sealed_group_members'):
                m=self.members[0]
                c.execute('INSERT INTO delivery_group_members VALUES(?,?,?,?,?,?,?)',
                    (group,'later','owner',m['project_ref'],m['task_ref'],1,canonical(m)))

    def test_unavailable_ha_goal_and_fake_success_never_accepted(self):
        for patch_values,code in ((dict(goal='deployed'), 'group_goal_unavailable'),
                                  (dict(success=True),'group_arguments_invalid'),
                                  (dict(approved=True),'group_arguments_invalid'),
                                  (dict(release_digest=digest(b'wrong')),'group_evidence_unavailable')):
            members=deepcopy(self.members);members[0].update(patch_values)
            with self.subTest(patch_values=patch_values),self.assertRaisesRegex(DeliveryError,code):self.create(members=members)

    def test_original_release_binding_and_build_are_required(self):
        with closing(self.store._connect()) as c:
            c.execute("UPDATE commands SET result_json='{}' WHERE idempotency_key LIKE 'software-build:%'")
        with self.assertRaisesRegex(DeliveryError,'group_evidence_unavailable'):self.create()

    def test_release_from_other_member_cannot_satisfy_task_binding(self):
        members=deepcopy(self.members)
        members[0]['release_ref']=members[1]['release_ref']
        members[0]['release_digest']=members[1]['release_digest']
        with self.assertRaisesRegex(DeliveryError,'group_evidence_unavailable'):self.create(members=members)

    def test_exact_oci_repository_digest_release_model_is_supported(self):
        member=self.member('gamma',identity_kind='oci_repo_digest')
        group=self.create(members=[member,self.members[1]],edges=[])['group_ref']
        self.assertEqual(self.groups.status('owner',group)['state'],'awaiting_acceptance')

    def test_owner_work_cannot_join_project_group(self):
        work=WorkStore(self.path,local_domain='primary',replica_domain='replica',replica=self.replica)
        task=work.create_work('owner','owner-work',conversation_ref='fixture',source_event_ref='owner-work-event',
            input_revision=1,requirement='fixture',spec=dict(profile='data',runtime_id='fixture-python',
                program="print(1)",output_names=['fixture.txt'],input_refs=[]))
        members=deepcopy(self.members);members[0]['task_ref']=task['task_id']
        with self.assertRaisesRegex(DeliveryError,'not_found'):self.create(members=members)

    def test_cas_controls_do_not_touch_member_tasks_or_authorize(self):
        group=self.create()['group_ref']
        with closing(self.store._connect()) as c: before=[tuple(r) for r in c.execute('SELECT * FROM tasks ORDER BY task_id')]
        first=self.groups.control('owner','pause',group_ref=group,expected_revision=1,action='pause')
        self.assertEqual(first['revision'],2)
        self.assertEqual(self.groups.control('owner','pause',group_ref=group,expected_revision=1,action='pause'),first)
        with self.assertRaisesRegex(DeliveryError,'revision_conflict'):
            self.groups.control('owner','stale',group_ref=group,expected_revision=1,action='resume')
        self.assertEqual(self.groups.status('owner',group)['state'],'paused')
        self.groups.control('owner','cancel',group_ref=group,expected_revision=2,action='cancel')
        with self.assertRaisesRegex(DeliveryError,'group_scope_invalid'):
            self.groups.control('owner','resume',group_ref=group,expected_revision=3,action='resume')
        with closing(self.store._connect()) as c: self.assertEqual(before,[tuple(r) for r in c.execute('SELECT * FROM tasks ORDER BY task_id')])

    def test_version_change_blocks_without_erasing_prior_success(self):
        group=self.create()['group_ref']; m=self.members[0]
        self.store.revise('owner','change-task',project_id=m['project_ref'],task_id=m['task_ref'],expected_revision=1,supplement='changed')
        view=self.groups.status('owner',group)
        self.assertEqual(view['state'],'partially_completed')
        self.assertEqual(view['members'][0]['state'],'blocked')
        self.assertTrue(view['members'][0]['historical_success'])
        self.assertFalse(view['combination_accepted'])

    def test_unknown_retains_historical_fact_and_observation_does_not_replay(self):
        group=self.create()['group_ref']
        # A missing original result is unknown, never an instruction to push.
        with closing(self.store._connect()) as c:
            c.execute("DELETE FROM commands WHERE event_id IN (SELECT event_id FROM events WHERE kind='repository.publish.result' AND json_extract(body_json,'$.result.publication_ref')='publication-alpha')")
        result=self.groups.observe('owner','observe',group_ref=group,expected_revision=1)
        self.assertEqual(result['state'],'partially_completed');self.assertTrue(result['requires_reconciliation'])
        self.assertTrue(result['members'][0]['historical_success'])
        self.assertEqual(self.groups.observe('owner','observe',group_ref=group,expected_revision=1),result)
        self.assertEqual(self.groups.status('owner',group)['members'][0]['facts'],result['members'][0]['facts'])

    def test_create_pending_restart_reconciles_same_snapshot_and_blocks_followup(self):
        self.replica.timeout_after_save=True;group=self.create()
        self.assertEqual(group['status'],'durability_pending')
        original=self.replica.objects[group['event_id']]
        self.assertEqual(self.groups.status('owner',group['group_ref'])['durability'],'local_only')
        with self.assertRaisesRegex(DeliveryError,'durability_pending'):
            self.groups.control('owner','pause',group_ref=group['group_ref'],expected_revision=1,action='pause')
        self.store=self.open();self.groups=DeliveryGroups(self.store);self.replica.timeout_after_save=False
        self.assertEqual(self.create()['status'],'accepted')
        self.assertEqual(self.replica.objects[group['event_id']].content,original.content)

    def test_pending_observation_never_hides_earlier_confirmed_success(self):
        group=self.create()['group_ref']
        with closing(self.store._connect()) as c:
            c.execute("DELETE FROM commands WHERE event_id IN (SELECT event_id FROM events WHERE kind='repository.publish.result' AND json_extract(body_json,'$.result.publication_ref')='publication-alpha')")
        self.replica.timeout_after_save=True
        observation=self.groups.observe('owner','pending-observe',group_ref=group,expected_revision=1)
        self.assertEqual(observation['status'],'durability_pending')
        view=self.groups.status('owner',group)
        self.assertEqual(view['durability'],'local_only')
        self.assertEqual(view['state'],'partially_completed')
        self.assertTrue(view['members'][0]['historical_success'])
        self.assertTrue(view['members'][0]['facts'])

    def test_dependency_projection_preserves_unknown_and_existing_effects(self):
        # A missing original result before creation yields no confirmed group
        # success to inherit. Dependents are blocked without hiding uncertainty.
        with closing(self.store._connect()) as c:
            c.execute("DELETE FROM commands WHERE event_id IN (SELECT event_id FROM events WHERE kind='repository.publish.result')")
        group=self.create()['group_ref'];view=self.groups.status('owner',group)
        self.assertEqual(view['state'],'recovery_required')
        self.assertFalse(view['members'][0]['dependency_blocked'])
        self.assertTrue(view['members'][1]['dependency_blocked'])
        self.assertEqual(view['members'][1]['state'],'unknown')

    def test_queries_only_read_and_paginate_owner_scoped_events(self):
        group=self.create()['group_ref'];statements=[];connect=self.store._connect
        def traced():
            c=connect();c.set_trace_callback(statements.append);return c
        with patch.object(self.store,'_connect',traced),patch('socket.socket',side_effect=AssertionError('network')):
            DeliveryGroups(self.store);self.groups.status('owner',group)
            self.assertEqual(self.groups.list('owner',limit=1)['total'],1)
            self.assertEqual(self.groups.events('owner',group,limit=1)['total'],1)
        self.assertFalse(any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE','DROP','ALTER')) for s in statements))

    def legacy5(self):
        with closing(self.store._connect()) as c:
            for name in ('delivery_group_edges','delivery_group_members','delivery_groups'):c.execute('DROP TABLE '+name)
            c.execute('PRAGMA user_version=5')

    def snapshot(self):
        image=self.root/'image.sqlite3'
        with closing(sqlite3.connect(self.path)) as c,closing(sqlite3.connect(image)) as b:
            c.backup(b);b.execute('PRAGMA journal_mode=DELETE')
        content=image.read_bytes();image.unlink()
        s=Snapshot('fixture-restore',content,digest(content))
        return s,ReplicaReceipt(s.event_id,s.sha256,'replica','fixture')

    def test_v5_default_refusal_explicit_private_backup_and_no_implicit_ddl(self):
        self.legacy5()
        with self.assertRaisesRegex(DeliveryError,'schema_upgrade_required'):self.open()
        self.assertEqual(list(self.root.glob('*.v5-*.sqlite3')),[])
        with self.assertRaisesRegex(DeliveryError,'schema_group_invalid'):DeliveryGroups(self.store)
        upgraded=self.open(upgrade_schema=True)
        self.assertEqual(upgraded.schema_backup.stat().st_mode&0o777,0o600)
        with closing(sqlite3.connect(upgraded.schema_backup)) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],5)
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name='delivery_groups'").fetchone()[0],0)
        self.assertEqual(upgraded.verify_event_chain(),self.store.verify_event_chain())

    def test_v5_corruption_not_repaired_and_migration_failure_rolls_back(self):
        self.legacy5()
        def broken(c):_install_group_schema(c);raise RuntimeError('fixture migration failure')
        with patch('codex_delivery.codex_delivery.store._install_group_schema',broken):
            with self.assertRaisesRegex(RuntimeError,'fixture migration failure'):self.open(upgrade_schema=True)
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],5)
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name='delivery_groups'").fetchone()[0],0)
            c.execute('DROP TABLE ha_release_steps')
        with self.assertRaisesRegex(DeliveryError,'schema_release_invalid'):self.open(upgrade_schema=True)

    def test_v5_migration_retains_unknown_ha_steps_locks_and_event_history(self):
        def record(c,timestamp):
            c.execute("INSERT INTO ha_release_operations VALUES('deploy-fixture','owner','task','project','target','digest','{}','recovery_required','{}',?,?)",(timestamp,timestamp))
            c.execute("INSERT INTO ha_release_locks VALUES('resource-fixture','deploy-fixture')")
            c.execute("INSERT INTO ha_release_steps VALUES('deploy-fixture','update','operation-fixture','unknown','{}',?,?)",(timestamp,timestamp))
            return dict(operation_ref='operation-fixture')
        self.mutate('fixture.ha.claim',{},None,record)
        self.legacy5()
        tables=('events','commands','durability','notification_outbox','ha_release_operations','ha_release_locks','ha_release_steps')
        with closing(self.store._connect()) as c:
            before={t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in tables}
        self.open(upgrade_schema=True)
        with closing(self.store._connect()) as c:
            self.assertEqual(before,{t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in tables})

    def test_v1_through_v4_without_group_tables_install_in_original_migration(self):
        self.legacy5()
        for version in (1,2,3,4):
            with self.subTest(version=version):
                with closing(self.store._connect()) as c:c.execute('PRAGMA user_version='+str(version))
                self.store=self.open(upgrade_schema=True)
                DeliveryGroups(self.store)
                with closing(self.store._connect()) as c:
                    self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
                self.legacy5()

    def test_current_schema_drift_is_not_repaired(self):
        with closing(self.store._connect()) as c:c.execute('DROP TRIGGER delivery_groups_no_delete')
        with self.assertRaisesRegex(DeliveryError,'schema_group_invalid'):self.open(upgrade_schema=True)

    def test_v5_restore_requires_upgrade_and_stays_read_only(self):
        self.legacy5();snapshot,receipt=self.snapshot();destination=self.root/'restored.sqlite3'
        with self.assertRaisesRegex(DeliveryError,'schema_upgrade_required'):
            DeliveryStore.restore(destination,snapshot,receipt,local_domain='replacement',replica_domain='replica')
        self.assertFalse(destination.exists())
        restored=DeliveryStore.restore(destination,snapshot,receipt,local_domain='replacement',replica_domain='replica',upgrade_schema=True)
        self.assertTrue(restored.recovery_mode)
        with self.assertRaisesRegex(DeliveryError,'recovery_read_only'):
            DeliveryGroups(restored).create('owner','restored-create',members=self.members,edges=[],acceptance_ref='fixture')
        with closing(restored._connect()) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)

    def test_v5_restore_preflight_rejects_corrupt_schema_without_destination(self):
        self.legacy5()
        with closing(self.store._connect()) as c:c.execute('DROP TABLE ha_release_steps')
        snapshot,receipt=self.snapshot();destination=self.root/'refused.sqlite3'
        with self.assertRaisesRegex(DeliveryError,'schema_release_invalid'):
            DeliveryStore.restore(destination,snapshot,receipt,local_domain='replacement',replica_domain='replica',upgrade_schema=True)
        self.assertFalse(destination.exists())

    def test_group_snapshot_restore_preserves_members_and_refuses_writes(self):
        group=self.create()['group_ref'];snapshot,receipt=self.snapshot()
        restored=DeliveryStore.restore(self.root/'restored.sqlite3',snapshot,receipt,local_domain='replacement',replica_domain='replica')
        helper=DeliveryGroups(restored)
        self.assertTrue(helper.status('owner',group)['recovery_read_only'])
        for call in (lambda:helper.control('owner','control',group_ref=group,expected_revision=1,action='pause'),
                     lambda:helper.observe('owner','observe',group_ref=group,expected_revision=1)):
            with self.assertRaisesRegex(DeliveryError,'recovery_read_only'):call()


if __name__=='__main__':unittest.main()
