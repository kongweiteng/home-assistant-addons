"""Canonical deferred validation Work on the ONE qualified Runner.

No subprocess/VM is started here. A paused test becomes queued only after the
same ObjectArchive confirms its original input bytes. The ordinary Coordinator
executes it; its narrowly registered internal result never calls Gateway.
"""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import time

from .durability import digest
from .object_archive import ArchiveReceipt
from .repository_execution import _confirmed
from .repo_manager import _private,_read
from .repository_validation import CandidateView
from .source_archive import WorkSourceArchive,_put,_sync
from .store import DeliveryError,canonical,identifier,reference

RECIPE='canary-python-v1'
RECIPES={'canary-python-v1':'python3','python-unittest-v1':'python3','node-test-v1':'node'}
_TEMPLATE='''import hashlib,importlib.util,io,json,os,sys,tarfile,tempfile,unittest
from pathlib import Path
bundle=Path(__file__).resolve().parent/'files'
identity=json.loads((bundle/'identity.json').read_bytes())
identity_digest='sha256:'+hashlib.sha256((bundle/'identity.json').read_bytes()).hexdigest()
prefix=PREFIX
with tempfile.TemporaryDirectory(prefix='validation-') as temporary:
 root=Path(temporary)
 with tarfile.open(bundle/'source.tar',mode='r:') as archive:
  entries=archive.getmembers()
  if not 1<=len(entries)<=4096:raise RuntimeError('source_count')
  total=0;seen=set()
  for item in entries:
   parts=item.name.split('/')
   if (not item.isfile() or item.name.startswith('/') or any(p in ('','.','..','.git') for p in parts)
       or item.name in seen or len(parts)>16):raise RuntimeError('source_entry')
   seen.add(item.name);total+=item.size
   if total>33554432:raise RuntimeError('source_size')
   path=root.joinpath(*parts);path.parent.mkdir(parents=True,exist_ok=True)
   path.write_bytes(archive.extractfile(item).read())
 source=root/prefix if prefix else root
 spec=importlib.util.spec_from_file_location('candidate_canary',source/'canary.py')
 candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
 class CanaryTests(unittest.TestCase):
  def test_A(self):
   data=root/'data-A';variant=source/'variants/A.json';candidate.prepare(data,candidate.load_variant(variant))
   value=candidate.health(data,variant);self.assertTrue(value['healthy']);self.assertEqual(value['variant'],'A')
   self.assertEqual(value['marker_sha256'],candidate.digest(candidate.MARKER))
  def test_B(self):
   data=root/'data-B';variant=source/'variants/B.json';candidate.prepare(data,candidate.load_variant(variant))
   value=candidate.health(data,variant);self.assertTrue(value['healthy']);self.assertEqual(value['variant'],'B')
  def test_C(self):
   data=root/'data-C';variant=source/'variants/C.json';candidate.prepare(data,candidate.load_variant(variant))
   value=candidate.health(data,variant);self.assertFalse(value['healthy'])
   self.assertEqual(value['failure_code'],'canary_expected_health_failure')
  def test_B_does_not_repair_C(self):
   data=root/'data-restore';a=source/'variants/C.json';b=source/'variants/B.json'
   candidate.prepare(data,candidate.load_variant(a));candidate.prepare(data,candidate.load_variant(b))
   self.assertFalse(candidate.health(data,b)['healthy'])
  def test_duplicate_json_rejected(self):
   with self.assertRaises(ValueError):candidate.decode(b'{"version":1,"version":1}')
 result=unittest.TextTestRunner(stream=io.StringIO()).run(unittest.defaultTestLoader.loadTestsFromTestCase(CanaryTests))
 value={'version':1,'identity_digest':identity_digest,'recipe':'canary-python-v1',
  'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
  'skipped':len(result.skipped),'passed':result.wasSuccessful() and result.testsRun==5}
 Path('validation.json').write_text(json.dumps(value,sort_keys=True,separators=(',',':')))
 raise SystemExit(0 if value['passed'] else 1)
'''

_PYTHON_TEMPLATE=_TEMPLATE[:_TEMPLATE.index(" spec=importlib.util")] + ''' sys.path.insert(0,str(source))
 value={'version':1,'identity_digest':identity_digest,'recipe':'python-unittest-v1',
  'tests_run':0,'failures':0,'errors':1,'skipped':0,'passed':False}
 try:
  suite=unittest.defaultTestLoader.discover(str(source/'tests'),pattern='test_*.py',top_level_dir=str(source))
  result=unittest.TextTestRunner(stream=io.StringIO()).run(suite)
  value.update(tests_run=result.testsRun,failures=len(result.failures),errors=len(result.errors),
   skipped=len(result.skipped),passed=result.wasSuccessful() and result.testsRun>0 and not result.skipped)
 except Exception:pass
 Path('validation.json').write_text(json.dumps(value,sort_keys=True,separators=(',',':')))
 raise SystemExit(0 if value['passed'] else 1)
'''

# Uses only built-in Node modules and its fixed test runner, never npm scripts,
# install hooks or a command from package.json. Executed inside the original VM.
_NODE_TEMPLATE=r'''const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const crypto=require('node:crypto'),{spawnSync}=require('node:child_process');
const bundle=path.join(path.dirname(fs.realpathSync(__filename)),'files');
const identityRaw=fs.readFileSync(path.join(bundle,'identity.json'));
const identity=JSON.parse(identityRaw);
const sha=b=>'sha256:'+crypto.createHash('sha256').update(b).digest('hex');
const raw=fs.readFileSync(path.join(bundle,'source.tar'));
if(sha(raw)!==identity.package_digest)throw Error('source_identity');
const root=fs.mkdtempSync(path.join(os.tmpdir(),'validation-'));
try{
 let offset=0,total=0;const names=new Set();
 while(offset+512<=raw.length){
  const h=raw.subarray(offset,offset+512);offset+=512;
  if(h.every(x=>x===0))break;
  const str=(a,b)=>h.subarray(a,b).toString('utf8').replace(/\0.*$/s,'');
  const name=(str(345,500)?str(345,500)+'/':'')+str(0,100),parts=name.split('/');
  const sizeText=str(124,136).trim(),size=parseInt(sizeText,8),type=h[156];
  let sum=0;for(let i=0;i<512;i++)sum+=(i>=148&&i<156)?32:h[i];
  if(!/^[0-7]+$/.test(sizeText)||sum!==parseInt(str(148,156).trim(),8)||![0,48].includes(type)
   ||parts.some(x=>!x||x==='.'||x==='..'||x==='.git')||name.startsWith('/')||name.includes('\\')
   ||parts.length>16||names.has(name)||names.size>=4096||offset+size>raw.length)throw Error('source_entry');
  names.add(name);total+=size;if(total>33554432)throw Error('source_size');
  const destination=path.join(root,...parts);fs.mkdirSync(path.dirname(destination),{recursive:true});
  fs.writeFileSync(destination,raw.subarray(offset,offset+size),{flag:'wx',mode:0o400});
  offset+=Math.ceil(size/512)*512;
 }
 if(names.size===0)throw Error('source_count');
 const source=path.join(root,PREFIX),testRoot=path.join(source,'tests');
 const files=[];
 function walk(folder){for(const item of fs.readdirSync(folder,{withFileTypes:true})){
  const full=path.join(folder,item.name);if(item.isDirectory())walk(full);
  else if(item.isFile()&&/\.test\.(?:js|cjs|mjs)$/.test(item.name))files.push(full);
 }}
 if(fs.existsSync(testRoot))walk(testRoot);files.sort();
 const result=files.length?spawnSync(process.execPath,['--test','--test-reporter=tap',...files],
  {cwd:source,env:{PATH:path.dirname(process.execPath)},encoding:'utf8',timeout:300000,maxBuffer:1048576})
  :{status:1,stdout:'',error:true};
 const count=name=>{const matches=[...(result.stdout||'').matchAll(new RegExp('^# '+name+' (\\d+)$','gm'))];
  return matches.length?Number(matches[matches.length-1][1]):-1;};
 const tests=count('tests'),failed=count('fail'),skipped=count('skipped'),cancelled=count('cancelled');
 const value={version:1,identity_digest:sha(identityRaw),recipe:'node-test-v1',tests_run:Math.max(0,tests),
  failures:Math.max(0,failed),errors:result.error||cancelled<0?1:cancelled,skipped:Math.max(0,skipped),
  passed:result.status===0&&!result.error&&tests>0&&failed===0&&skipped===0&&cancelled===0};
 const ordered=Object.fromEntries(Object.entries(value).sort(([a],[b])=>a.localeCompare(b)));
 fs.writeFileSync('validation.json',JSON.stringify(ordered));process.exitCode=value.passed?0:1;
}finally{fs.rmSync(root,{recursive:true,force:true});}
'''


def fail(code='repository_tests_unavailable'):raise DeliveryError(code) from None


def program(source_prefix='',recipe=RECIPE):
    if recipe not in RECIPES:fail('repository_test_recipe_unavailable')
    if source_prefix:
        from .repository_inputs import _permitted_name
        _permitted_name(source_prefix)
    if recipe=='node-test-v1':return _NODE_TEMPLATE.replace('path.join(root,PREFIX)','path.join(root,'+json.dumps(source_prefix)+')')
    template=_TEMPLATE if recipe==RECIPE else _PYTHON_TEMPLATE
    template=template.replace("prefix=PREFIX", "if 'sha256:'+hashlib.sha256((bundle/'source.tar').read_bytes()).hexdigest()!=identity['package_digest']:raise RuntimeError('source_identity')\nprefix="+repr(source_prefix))
    return template


def registered_test(store,task):
    store.verify_event_chain()
    with closing(store._connect()) as connection:
        rows=connection.execute("SELECT c.idempotency_key FROM commands c JOIN events e USING(event_id) "
            "WHERE e.owner_ref=? AND e.task_id=? AND e.kind='repository.test.create'",
            (task['owner_ref'],task['task_id'])).fetchall()
    if len(rows)>1:fail()
    return None if not rows else _confirmed(store,task['owner_ref'],rows[0][0],'repository.test.create')


class RepositoryTestPort:
    def __init__(self,store,sources,*,runtime_id,runtime_qualification_digest,source_prefix='',policy=None,recipe=RECIPE):
        if not isinstance(sources,WorkSourceArchive) or sources.store is not store:fail()
        reference(runtime_id)
        from .repo_manager import _sha
        _sha(runtime_qualification_digest)
        self.store,self.sources,self.archive=store,sources,sources.archive
        self.runtime_id,self.runtime_qualification=runtime_id,runtime_qualification_digest
        self.recipe=recipe
        self.program=program(source_prefix,recipe);self.template_digest=digest(self.program.encode())
        self.policy=policy;self.execution=None

    def attach_execution(self,execution):
        from .execution_composition import QualifiedExecution
        if (type(execution) is not QualifiedExecution or execution.store is not self.store
                or execution.source_archive is not self.sources):fail()
        self.execution=execution
        self._runtime()

    def _runtime(self):
        execution=self.execution
        if (execution is None or not execution._qualified or self.store.recovery_mode
                or execution.policy.registry_policy()['qualification_digest']!=self.runtime_qualification
                or not any(value['profile']=='code' and value['runtime_id']==self.runtime_id and value['available'] is True
                    for value in execution.capabilities())):fail('repository_test_runtime_unqualified')
        runtimes=[r for r in execution.policy.runtimes if r.runtime_ref==self.runtime_id]
        if len(runtimes)!=1 or runtimes[0].interpreter!=RECIPES[self.recipe] or runtimes[0].kind!='work-qemu-v1':fail()
        return execution

    def _current(self,context):
        if self.policy is None:fail('repository_test_policy_missing')
        self.policy.verify()
        with closing(self.store._connect()) as connection:
            if connection.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone():fail('repository_tests_pending')
            task=self.store._owned(connection,'tasks','task_id',context['owner_ref'],context['task_id'])
            project=self.store._project(connection,context['owner_ref'],context['project_id'])
            repository=self.store._owned(connection,'repositories','repo_id',context['owner_ref'],context['repo_id'])
            binding=connection.execute('SELECT * FROM task_repositories WHERE owner_ref=? AND task_id=? AND repo_id=?',
                (context['owner_ref'],context['task_id'],context['repo_id'])).fetchone()
            if (self.store.recovery_mode or task['revision']!=context['task_revision'] or task['project_id']!=context['project_id']
                    or task['mode']!=context['task_mode'] or task['mode'] not in {'develop','deliver','deploy'}
                    or task['state'] not in {'queued','running'} or task['control_pending'] or project['state']!='active'
                    or repository['status']!='enrolled' or repository['remote_identity']!=context['remote_identity']
                    or binding is None or binding['access']!='write' or binding['paths_json']!='[]'):fail('repository_test_scope_changed')
        return task,project

    def _repositories(self,context):
        with closing(self.store._connect()) as connection:
            return [dict(row) for row in connection.execute(
                'SELECT repo_id,access,paths_json FROM task_repositories WHERE owner_ref=? AND task_id=? ORDER BY repo_id',
                (context['owner_ref'],context['task_id']))]

    def request(self,context,view):
        execution=self._runtime();task,project=self._current(context)
        if type(view) is not CandidateView:fail()
        package=view.package()
        identity={'version':1,'context':context,'manifest_digest':view.manifest_digest,'scan_digest':view.scan_digest,
            'package_digest':digest(package),'repositories':self._repositories(context),
            'project_revision':task['project_revision'],
            'runtime_id':self.runtime_id,'runtime_qualification_digest':self.runtime_qualification,
            'template_digest':self.template_digest,'runner_ref':execution.settings.runner_ref,'recipe':self.recipe}
        identity_raw=canonical(identity).encode();key='repository-test:'+digest(identity_raw)[7:]
        spec={'profile':'code','runtime_id':self.runtime_id,'program':self.program,'output_names':['validation.json'],'input_refs':[]}
        def create(connection,timestamp):
            self._current(context);test,workspace=identifier('DL'),identifier('WS')
            connection.execute("INSERT INTO tasks(task_id,owner_ref,project_id,conversation_ref,source_event_ref,mode,requirement,"
                "project_revision,revision,phase,state,created_at,updated_at,task_scope,workspace_ref,work_json,input_revision,current_source_event_ref) "
                "VALUES (?,?,?,?,?,'work',?, ?,1,'execution','paused',?,?,'project',?,?,1,?)",
                (test,context['owner_ref'],project['project_id'],task['conversation_ref'],'RT-'+digest(identity_raw)[7:],
                 'Fixed internal repository tests',project['revision'],timestamp,timestamp,workspace,canonical(spec),'RT-'+digest(identity_raw)[7:]))
            return {'task_id':test,'project_id':project['project_id'],'workspace_ref':workspace,'identity':identity}
        created=self.store._mutation(context['owner_ref'],key,'repository.test.create',identity,create)
        if created.get('status')!='accepted':fail('repository_tests_pending')
        test=self.store.work_task(context['owner_ref'],created['task_id'])
        if (test['revision']!=1 or test['project_id']!=context['project_id'] or test['work_json']!=canonical(spec)
                or test['control_pending'] or self._repositories_for_test(test)):fail()
        if test['state']=='paused':
            # A deliberately paused/recovered original test is never resumed by
            # observing the software pipeline. Only its initial ready is valid.
            if _confirmed(self.store,context['owner_ref'],'repository-test-ready:'+test['task_id'],'repository.test.ready') is not None:fail()
            self._seal(test,spec,{'source.tar':package,'identity.json':identity_raw})
            def ready(connection,timestamp):
                self._current(context)
                current=self.store._work(connection,context['owner_ref'],test['task_id'],1)
                if current['state']!='paused' or current['control_pending']:fail()
                if _confirmed(self.store,context['owner_ref'],'source:'+test['task_id']+':1','work.source_archive') is None:fail()
                connection.execute("UPDATE tasks SET state='queued',updated_at=? WHERE task_id=?",(timestamp,test['task_id']))
                return {'task_id':test['task_id'],'identity_digest':digest(identity_raw)}
            queued=self.store._mutation(context['owner_ref'],'repository-test-ready:'+test['task_id'],
                'repository.test.ready',{'task_id':test['task_id'],'identity_digest':digest(identity_raw)},ready)
            if queued.get('status')!='accepted':fail('repository_tests_pending')
        result=_confirmed(self.store,context['owner_ref'],'repository-test-result:'+test['task_id'],'repository.test.result')
        if result is None:fail('repository_tests_pending')
        current=self.store.work_task(context['owner_ref'],test['task_id'])
        if result['result'].get('passed') is not True or current['state']!='completed':fail('repository_tests_failed')
        if (result['arguments']['identity']!=identity or registered_test(self.store,current)['result']['identity']!=identity):fail()
        from .repo_manager import _sha
        _sha(result['result']['tests_digest'])
        return result['result']['tests_digest']

    def _repositories_for_test(self,task):
        with closing(self.store._connect()) as connection:
            return connection.execute('SELECT 1 FROM task_repositories WHERE owner_ref=? AND task_id=? LIMIT 1',
                (task['owner_ref'],task['task_id'])).fetchone() is not None

    def _seal(self,task,spec,inputs):
        document={k:task[k] for k in ('owner_ref','task_id','workspace_ref','revision','input_revision',
            'current_source_event_ref','project_id','requirement')}
        document.update(version=1,spec=spec,inputs={k:digest(v) for k,v in sorted(inputs.items())})
        files={'program':spec['program'].encode(),'task.json':canonical(document).encode(),
            **{'inputs/'+k:v for k,v in inputs.items()}}
        root=self.store.path.parent/'repository-test-sources'/task['task_id'];_private(root)
        for name,raw in sorted(files.items()):_put(root/name,raw)
        expected=[{'name':k,'size_bytes':len(v),'sha256':digest(v)} for k,v in sorted(files.items())]
        source_digest=digest(canonical(expected).encode());source_ref='SRC-'+source_digest[7:]
        prior=_confirmed(self.store,task['owner_ref'],'source:'+task['task_id']+':1','work.source_archive')
        if prior is not None:
            if (prior['arguments']['source_digest']!=source_digest
                    or prior['arguments']['spec_digest']!=digest(task['work_json'].encode())
                    or prior['result']['source_ref']!=source_ref):fail()
            return
        receipt=self.archive.save(root,scope={'owner_ref':task['owner_ref'],'task_ref':task['task_id'],
            'revision':1,'checkpoint_ref':source_ref},expected_files=expected)
        arguments={'task_id':task['task_id'],'expected_revision':1,'spec_digest':digest(task['work_json'].encode()),
            'source_digest':source_digest,'receipt':receipt.document()}
        def record(connection,timestamp):
            current=self.store._work(connection,task['owner_ref'],task['task_id'],1)
            if current['state']!='paused' or current['work_json']!=canonical(spec):fail()
            return {'task_id':task['task_id'],'revision':1,'source_ref':source_ref,'source_archive':receipt.document()}
        value=self.store._mutation(task['owner_ref'],'source:'+task['task_id']+':1','work.source_archive',arguments,record)
        if value.get('status')!='accepted':fail('repository_tests_pending')

    def __call__(self,task,evidence):
        original=registered_test(self.store,task)
        if original is None:fail()
        execution=self._runtime();identity=original['result']['identity'];context=identity['context']
        spec={'profile':'code','runtime_id':self.runtime_id,'program':self.program,'output_names':['validation.json'],'input_refs':[]}
        if (original['arguments']!=identity or original['result']['task_id']!=task['task_id']
                or original['result']['workspace_ref']!=task['workspace_ref']
                or original['result']['project_id']!=task['project_id']
                or identity['template_digest']!=self.template_digest or identity['runtime_id']!=self.runtime_id
                or identity['runtime_qualification_digest']!=self.runtime_qualification
                or identity['recipe']!=self.recipe or identity['repositories']!=self._repositories(context)
                or identity['runner_ref']!=execution.settings.runner_ref or task['assigned_runner_ref']!=identity['runner_ref']
                or task['revision']!=1 or task['control_pending'] or task['state']!='running'
                or task['phase']!='checkpoint_archive' or task['project_id']!=context['project_id']
                or task['work_json']!=canonical(spec) or self._repositories_for_test(task)):fail()
        software,_=self._current(context)
        if software['project_revision']!=identity['project_revision']:fail()
        receipt=evidence['execution']
        # Cross-check the original executor journal, not just task JSON.
        execution.adapter.checkpoint(task,receipt)
        archived=_confirmed(self.store,task['owner_ref'],'archive:'+task['lease_ref'],'work.checkpoint')
        source=_confirmed(self.store,task['owner_ref'],'source:'+task['task_id']+':1','work.source_archive')
        ready=_confirmed(self.store,task['owner_ref'],'repository-test-ready:'+task['task_id'],'repository.test.ready')
        if (archived is None or source is None or ready is None
                or archived['arguments']['evidence']!=evidence
                or archived['arguments']['task_id']!=task['task_id']
                or archived['arguments']['lease_ref']!=task['lease_ref']
                or archived['arguments']['lease_epoch']!=task['lease_epoch']
                or ready['arguments']!={'task_id':task['task_id'],'identity_digest':digest(canonical(identity).encode())}
                or ready['result']!={'task_id':task['task_id'],'identity_digest':digest(canonical(identity).encode())}
                or receipt['workspace_ref']!=task['workspace_ref'] or receipt['input_revision']!=1
                or receipt['assignment_epoch']!=task['lease_epoch'] or receipt['checkpoint_confirmed'] is not True):fail()
        spec=json.loads(task['work_json']);inputs=self.sources.stopped_inputs(task,spec)
        if (inputs is None or set(inputs)!={'identity.json','source.tar'}
                or inputs['identity.json']!=canonical(identity).encode()
                or digest(inputs['source.tar'])!=identity['package_digest']):fail()
        with tempfile.TemporaryDirectory(prefix='.test-readback-',dir=self.store.path.parent) as temporary:
            destination=Path(temporary)/'checkpoint'
            self.archive.restore(destination,scope={'owner_ref':task['owner_ref'],'task_ref':task['task_id'],
                'revision':1,'checkpoint_ref':receipt['checkpoint_ref']},receipt=ArchiveReceipt.from_document(evidence['checkpoint_archive']))
            report_raw=_read(destination/'validation.json',65536)
            from .object_archive import _pairs
            report=json.loads(report_raw,object_pairs_hook=_pairs)
        expected={item['name']:item for item in receipt['files']}
        if (set(expected)!={'validation.json'} or digest(report_raw)!=expected['validation.json']['sha256']
                or len(report_raw)!=expected['validation.json']['size_bytes']
                or canonical(report).encode()!=report_raw
                or set(report)!={'version','identity_digest','recipe','tests_run','failures','errors','skipped','passed'}
                or type(report['version']) is not int or report['version']!=1 or report['recipe']!=self.recipe
                or report['identity_digest']!=digest(inputs['identity.json'])
                or any(type(report[k]) is not int or report[k]<0 for k in ('tests_run','failures','errors','skipped'))
                or type(report['passed']) is not bool):fail()
        passed=(receipt['evidence']=='exited' and receipt['stop_reason']=='finished' and type(receipt['exit_code']) is int
            and receipt['exit_code']==0 and report['passed'] and report['tests_run']>0
            and (self.recipe!=RECIPE or report['tests_run']==5)
            and report['failures']==report['errors']==report['skipped']==0)
        tests_digest=digest(canonical({'identity':identity,'report':report,'checkpoint':evidence}).encode())
        arguments={'task_id':task['task_id'],'identity':identity,'report':report,'checkpoint':evidence}
        def finish(connection,timestamp):
            self._current(context);current=self.store._work(connection,task['owner_ref'],task['task_id'],1)
            if (current['state']!='running' or current['lease_ref']!=task['lease_ref'] or current['lease_epoch']!=task['lease_epoch']
                    or current['control_pending'] or current['runtime_result_json']!=canonical(evidence)):fail()
            outcome={**evidence,'delivery':'internal_archive','validation':'passed' if passed else 'failed',
                'exit_code':receipt['exit_code'],'tests_digest':tests_digest}
            connection.execute('UPDATE tasks SET state=?,runtime_result_json=?,updated_at=? WHERE task_id=?',
                ('completed' if passed else 'failed',canonical(outcome),timestamp,task['task_id']))
            return {'task_id':task['task_id'],'passed':bool(passed),'tests_digest':tests_digest}
        result=self.store._mutation(task['owner_ref'],'repository-test-result:'+task['task_id'],
            'repository.test.result',arguments,finish)
        return result.get('status')=='accepted'
