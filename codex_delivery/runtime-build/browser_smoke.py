"""Fixed offline Chromium namespace-sandbox smoke, no model or network data."""
import ctypes,json,os,pathlib,resource,subprocess
home=pathlib.Path('/tmp/chromium-home');home.mkdir(mode=0o700);os.chown(home,65532,65532)
def demote():
 os.setgroups([]);os.setgid(65532);os.setuid(65532)
 assert ctypes.CDLL(None).prctl(38,1,0,0,0)==0
 resource.setrlimit(resource.RLIMIT_CORE,(0,0));resource.setrlimit(resource.RLIMIT_NOFILE,(256,256))
 resource.setrlimit(resource.RLIMIT_NPROC,(128,128));resource.setrlimit(resource.RLIMIT_FSIZE,(16*1024**2,16*1024**2))
command=['/usr/lib/chromium/chromium','--headless','--disable-gpu','--disable-background-networking',
 '--disable-component-update','--disable-sync','--no-first-run','--no-default-browser-check',
 '--user-data-dir='+str(home),'--dump-dom','data:text/html,<title>Offline sandbox smoke</title><body>forty-two</body>']
try:
 result=subprocess.run(command,env={'PATH':'/usr/bin:/bin','HOME':str(home),'TMPDIR':'/tmp'},
  stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,preexec_fn=demote,timeout=50)
 print('BROWSER_SMOKE_RESULT='+json.dumps({'return_code':result.returncode,'stdout':result.stdout[-16000:].decode(errors='replace'),
  'stderr':result.stderr[-16000:].decode(errors='replace'),'network_interfaces':[p.name for p in pathlib.Path('/sys/class/net').iterdir()]}),flush=True)
except Exception as error:print('BROWSER_SMOKE_ERROR='+type(error).__name__,flush=True)
