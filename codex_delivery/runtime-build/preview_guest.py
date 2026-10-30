"""Fixed root preview supervisor. App/Chromium/driver have separate guest UIDs."""
import base64,ctypes,hashlib,io,json,os,pathlib,resource,selectors,signal,socket,stat,struct,subprocess,time
import importlib.util
# -I intentionally omits the script directory. Load only the pinned, root-owned
# checkpoint implementation; never search a task-controlled working directory.
_checkpoint_spec=importlib.util.spec_from_file_location('work_guest','/opt/poc/work_guest.py')
checkpoint=importlib.util.module_from_spec(_checkpoint_spec)
_checkpoint_spec.loader.exec_module(checkpoint)

MAGIC=b'CWP1\n';MAX_FRAME=6*1024**2;MAX_SPOOL=96*1024**2
UIDS=(65530,65531,65532)
DRIVER_FAILURES={'driver','driver_cdp_deadline','driver_reload_deadline','driver_capture_deadline','driver_cdp_failed','driver_capture_failed','driver_event','driver_request','driver_sequence','driver_init','driver_load_pending','driver_pixel_limit','driver_render_pending','driver_render_deadline','driver_render_changed'}
LIMITS={'workspace_bytes':1024**2,'max_files':128,'tmp_bytes':256*1024**2}

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
def decode(raw):
 def pairs(items):
  result={}
  for key,value in items:
   if key in result:raise RuntimeError('duplicate')
   result[key]=value
  return result
 value=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
 if not isinstance(value,dict):raise RuntimeError('json')
 return value
def config():
 raw=pathlib.Path('/bundle/request.json').read_bytes()
 if len(raw)>8192:raise RuntimeError('config')
 value=decode(raw)
 if (set(value)!={'version','nonce','scope_digest','ttl_seconds','interpreter'} or type(value['version']) is not int or value['version']!=1
  or not isinstance(value['nonce'],str) or len(value['nonce'])!=64 or any(c not in '0123456789abcdef' for c in value['nonce'])
  or not isinstance(value['scope_digest'],str) or len(value['scope_digest'])!=71 or not value['scope_digest'].startswith('sha256:')
  or type(value['ttl_seconds']) is not int or not 1<=value['ttl_seconds']<=1800 or value['interpreter'] not in {'python3','node'}):raise RuntimeError('config')
 return value
def port_path():
 for item in pathlib.Path('/sys/class/virtio-ports').iterdir():
  if (item/'name').read_text().strip()=='codex-preview':
   path=pathlib.Path('/dev')/item.name;info=path.stat()
   if not stat.S_ISCHR(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)&0o077:raise RuntimeError('port')
   return path
 raise RuntimeError('port')
def demote(uid,read_fd=None,write_fd=None):
 if read_fd is not None:
  os.dup2(read_fd,3);os.dup2(write_fd,4)
  for fd in (read_fd,write_fd):
   if fd not in (3,4):os.close(fd)
 os.setgroups([]);os.setgid(uid);os.setuid(uid)
 if ctypes.CDLL(None).prctl(38,1,0,0,0):raise RuntimeError('identity')
 for kind,amount in ((resource.RLIMIT_CORE,0),(resource.RLIMIT_NOFILE,256),(resource.RLIMIT_NPROC,128),
  (resource.RLIMIT_FSIZE,1024**2 if uid==65532 else 64*1024**2),(resource.RLIMIT_CPU,3602),
  (resource.RLIMIT_AS,16*1024**4)):
  resource.setrlimit(kind,(amount,amount))
def private_home(name,uid):
 path=pathlib.Path('/tmp')/name;path.mkdir(mode=0o700);os.chown(path,uid,uid);return str(path)
def all_pids():
 values=[]
 for item in pathlib.Path('/proc').iterdir():
  if not item.name.isdigit():continue
  try:
   status=dict(line.split(':',1) for line in (item/'status').read_text().splitlines() if ':' in line)
   if int(status['Uid'].split()[0]) in UIDS and status['State'].strip()[0] not in 'ZX':values.append(int(item.name))
  except (FileNotFoundError,ProcessLookupError):pass
 return values
def stop_all(processes):
 for _ in range(100):
  pids=all_pids()
  if not pids:break
  for pid in pids:
   try:os.kill(pid,signal.SIGKILL)
   except ProcessLookupError:pass
  time.sleep(.01)
 else:raise RuntimeError('payload_alive')
 for process in processes:process.wait(timeout=1)
def isolation():
 if sorted(p.name for p in pathlib.Path('/sys/class/net').iterdir())!=['lo']:raise RuntimeError('network')
 renderers=[]
 for item in pathlib.Path('/proc').iterdir():
  if not item.name.isdigit():continue
  try:
   status=dict(line.split(':',1) for line in (item/'status').read_text().splitlines() if ':' in line)
   if int(status['Uid'].split()[0])!=65531 or b'--type=renderer' not in (item/'cmdline').read_bytes():continue
   renderers.append(status.get('Seccomp','').strip()=='2' and len(status.get('NSpid','').split())>=2)
  except (FileNotFoundError,ProcessLookupError):pass
 if not renderers or not all(renderers):raise RuntimeError('chrome_sandbox')
 return {'interfaces':['lo'],'app_uid':65532,'chrome_uid':65531,'driver_uid':65530,'renderer_seccomp':True,'renderer_pid_namespace':True}
def archive():
 tree=checkpoint.entries(LIMITS);maximum=1024**2+(128*4+16)*2048+32768
 buffer=io.BytesIO();sink=checkpoint.Sink(maximum,buffer);checkpoint.archive(tree,sink)
 if checkpoint.entries(LIMITS)!=tree:raise RuntimeError('checkpoint_changed')
 raw=buffer.getvalue()
 return {'archive_b64':base64.b64encode(raw).decode(),'archive_sha256':'sha256:'+hashlib.sha256(raw).hexdigest(),'archive_bytes':len(raw)}
def main():
 value=config();processes=[];started=time.monotonic();last=0;pending=None;pending_refresh=False;render_pending=False;ready=False;reason='failed';stop_id=0;stage='app'
 os.chown('/workspace',65532,65532);os.chmod('/workspace',0o700)
 # Validate the restored tree before exposing it to task code.
 tree=checkpoint.entries(LIMITS)
 for directory in tree['directories']:os.chown('/workspace/'+directory,65532,65532);os.chmod('/workspace/'+directory,0o700)
 for item in tree['files']:os.chown('/workspace/'+item['name'],65532,65532);os.chmod('/workspace/'+item['name'],0o600)
 with os.fdopen(os.open(port_path(),os.O_RDWR|os.O_NOFOLLOW),'r+b',buffering=0) as port:
  total=0
  def send(message):
   nonlocal total
   message={**message,'nonce':value['nonce'],'scope_digest':value['scope_digest']};raw=canonical(message)
   if len(raw)>MAX_FRAME:raise RuntimeError('frame')
   data=struct.pack('!I',len(raw))+raw;total+=len(data)
   if total>MAX_SPOOL:raise RuntimeError('spool')
   view=memoryview(data)
   while view:
    written=port.write(view)
    if not written:raise RuntimeError('port')
    view=view[written:]
  port.write(MAGIC)
  try:
   binary='/usr/bin/python3' if value['interpreter']=='python3' else '/usr/bin/node'
   app=subprocess.Popen([binary,*(['-I','-B'] if value['interpreter']=='python3' else []),'/bundle/program'],cwd='/workspace',
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,start_new_session=True,
    env={'PATH':'/usr/bin:/bin','HOME':'/workspace','TMPDIR':'/tmp','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'},preexec_fn=lambda:demote(65532))
   processes.append(app)
   while True:
    if app.poll() is not None or time.monotonic()-started>min(20,value['ttl_seconds']):raise RuntimeError('app_not_ready')
    try:
     with socket.create_connection(('127.0.0.1',8080),timeout=.1):break
    except OSError:time.sleep(.05)
   stage='chrome';to_r,to_w=os.pipe();from_r,from_w=os.pipe()
   chrome_home=private_home('chromium',65531);driver_home=private_home('driver',65530)
   try:
    chrome=subprocess.Popen(['/usr/lib/chromium/chromium','--headless','--disable-gpu','--remote-debugging-pipe','--no-startup-window','--window-size=1024,768','--force-device-scale-factor=1',
     '--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run','--no-default-browser-check',
     '--disable-renderer-backgrounding','--disable-background-timer-throttling','--disable-backgrounding-occluded-windows','--user-data-dir='+chrome_home],
     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
     env={'PATH':'/usr/bin:/bin','HOME':chrome_home,'TMPDIR':'/tmp','DBUS_SESSION_BUS_ADDRESS':'unix:path=/tmp/no-dbus'},
     pass_fds=tuple(set((to_r,from_w,3,4))),preexec_fn=lambda:demote(65531,to_r,from_w))
    processes.append(chrome)
    stage='driver';driver=subprocess.Popen(['/usr/bin/node','/opt/poc/preview_driver.cjs'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
     env={'PATH':'/usr/bin:/bin','HOME':driver_home,'TMPDIR':'/tmp'},pass_fds=tuple(set((from_r,to_w,3,4))),preexec_fn=lambda:demote(65530,from_r,to_w))
    processes.append(driver)
   finally:
    for fd in (to_r,to_w,from_r,from_w):os.close(fd)
   driver.stdin.write(b'{"kind":"init"}\n');driver.stdin.flush()
   buffers={port.fileno():bytearray(),driver.stdout.fileno():bytearray()};stderr_size=0;done=False
   for fd in (*buffers,chrome.stderr.fileno()):os.set_blocking(fd,False)
   with selectors.DefaultSelector() as selector:
    for fd in (*buffers,chrome.stderr.fileno()):selector.register(fd,selectors.EVENT_READ)
    while not done:
     if time.monotonic()-started>=value['ttl_seconds']:reason='expired';break
     if not ready and time.monotonic()-started>70:raise RuntimeError('chrome_not_ready')
     if app.poll() is not None or chrome.poll() is not None:raise RuntimeError('child_exit')
     for key,_ in selector.select(.05):
      data=os.read(key.fd,65536)
      if key.fd==chrome.stderr.fileno():
       if not data:selector.unregister(key.fd)
       stderr_size+=len(data)
       if stderr_size>65536:raise RuntimeError('stderr')
       continue
      if not data:raise RuntimeError('pipe')
      buffer=buffers[key.fd];buffer.extend(data)
      if len(buffer)>MAX_FRAME+4:raise RuntimeError('frame')
      if key.fd==port.fileno():
       while len(buffer)>=4:
        size=struct.unpack('!I',buffer[:4])[0]
        if not 1<=size<=8192:raise RuntimeError('request')
        if len(buffer)<size+4:break
        request=decode(bytes(buffer[4:size+4]));del buffer[:size+4]
        kind=request.get('kind');expected={'kind','id','nonce','event'} if kind=='action' else {'kind','id','nonce'}
        if (set(request)!=expected or request['nonce']!=value['nonce'] or type(request['id']) is not int
          or request['id']!=last+1 or pending is not None or not ready or kind not in {'frame','action','stop'}
          or kind=='action' and render_pending):raise RuntimeError('request')
        if kind=='stop':reason='stopped';stop_id=request['id'];done=True;break
        if request['id']>4096:raise RuntimeError('request_limit')
        last=request['id'];pending=last
        pending_refresh=kind=='action' and isinstance(request.get('event'),dict) and request['event'].get('kind')=='refresh'
        command={k:v for k,v in request.items() if k!='nonce'}
        driver.stdin.write(canonical(command)+b'\n');driver.stdin.flush()
      else:
       while b'\n' in buffer:
        raw,_,rest=buffer.partition(b'\n');buffer[:]=rest;message=decode(raw)
        if message=={'kind':'ready','version':1} and not ready:
         stage='isolation';observed=isolation();ready=True;stage='commands';send({'kind':'ready','isolation':observed})
        elif (ready and pending_refresh and set(message)=={'kind','id','state'}
          and message['kind']=='action_ack' and type(message['id']) is int and message['id']==pending
          and message['state']=='render_pending'):
         send(message);pending=None;pending_refresh=False;render_pending=True
        elif (ready and not pending_refresh and set(message)=={'kind','id','mime','width','height','image_b64'}
          and message['kind']=='frame' and type(message['id']) is int and message['id']==pending):
         send(message);pending=None;render_pending=False
        else:
         if set(message)=={'kind','code'} and message['kind']=='error' and message['code'] in DRIVER_FAILURES:stage=message['code']
         raise RuntimeError('driver')
  except Exception as error:
   reason='failed'
   if isinstance(error,RuntimeError) and str(error)=='chrome_not_ready':stage='driver_boot_timeout'
   elif isinstance(error,RuntimeError) and str(error)=='request':stage='commands_protocol'
  finally:
   stop_all(processes)
   receipt={'archive_b64':None,'archive_sha256':None,'archive_bytes':None}
   if reason=='stopped':
    try:receipt=archive()
    except Exception:pass
   send({'kind':'stopped','id':stop_id,'reason':reason,'failure_stage':stage if reason=='failed' else None,**receipt})
   for process in processes:
    for stream in (process.stdin,process.stdout,process.stderr):
     if stream is not None:stream.close()

if __name__=='__main__':
 try:main()
 except Exception:pass
