"""Fixed ordinary thread/address/affinity qualification; no task or credentials."""
import hashlib,json,os,pathlib,subprocess
root=pathlib.Path('/work/root');root.mkdir(mode=0o700)
(root/'runtime').mkdir();(root/'tmp').mkdir()
for name in ('runtime/readonly.txt','input.img','output.img','export.bin'):
 p=root/name;p.write_text('resource-qualification-only');p.chmod(0o444 if name in ('runtime/readonly.txt','input.img') else 0o600)
for path in (root/'runtime',root/'tmp',root):path.chmod(0o555)
result=subprocess.run(['/isolate',str(root),'--self-test',str(os.getpid()),str(256*1024**2),'32','2'],
 stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={'LANG':'C'},timeout=10,text=True)
print(json.dumps({'helper_sha256':hashlib.sha256(pathlib.Path('/isolate').read_bytes()).hexdigest(),
 'return_code':result.returncode,'stdout':result.stdout[-8000:],'stderr':result.stderr[-4000:]}),flush=True)
assert result.returncode==0
report=json.loads(result.stdout)
assert report['qualified'] and report['resource_profile']=='native-resource-v1'
assert report['thread_limit']==32 and 1<=report['created_threads']<32
assert report['address_bytes']==256*1024**2 and 1<=report['logical_cpus']<=2
assert report['expected_parent_bound'] is True
wrong=subprocess.run(['/isolate','/does-not-exist','--exec-resource','4096','1',str(128*1024**2),'4','1',
 str(os.getpid()+1000000),'/runtime/not-executed'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
 stderr=subprocess.PIPE,env={'LANG':'C'},timeout=5,text=True)
assert wrong.returncode==125 and wrong.stderr=='QEMU_ISOLATION_ERROR=parent_identity:0\n'
print('NATIVE_RESOURCE_QUALIFICATION_PASS',flush=True)
