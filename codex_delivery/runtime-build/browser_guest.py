"""Fixed root bridge: CDP/stdio pipes only; it never executes page/model code."""
import ctypes,json,os,pathlib,resource,selectors,signal,stat,struct,subprocess,time

MAGIC=b'CWB1\n';MAX_FRAME=8*1024**2;MAX_SPOOL=16*1024**2

def canonical(value):return json.dumps(value,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
def load_config():
 raw=pathlib.Path('/bundle/config.json').read_bytes()
 if len(raw)>8192:raise RuntimeError('config')
 value=json.loads(raw)
 if set(value)!={'version','url','screenshot'} or type(value['version']) is not int or value['version']!=1 or not isinstance(value['url'],str) or len(value['url'])>4096 or type(value['screenshot']) is not bool:raise RuntimeError('config')
 return value

def port_path():
 for item in pathlib.Path('/sys/class/virtio-ports').iterdir():
  if (item/'name').read_text().strip()=='codex-browser':
   path=pathlib.Path('/dev')/item.name;info=path.stat()
   if not stat.S_ISCHR(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)&0o077:raise RuntimeError('port')
   return path
 raise RuntimeError('port')

def demote(uid,read_fd,write_fd):
 os.dup2(read_fd,3);os.dup2(write_fd,4)
 for fd in (read_fd,write_fd):
  if fd not in (3,4):os.close(fd)
 os.setgroups([]);os.setgid(uid);os.setuid(uid)
 if ctypes.CDLL(None).prctl(38,1,0,0,0):raise RuntimeError('identity')
 for kind,amount in ((resource.RLIMIT_CORE,0),(resource.RLIMIT_NOFILE,256),(resource.RLIMIT_NPROC,128),
   (resource.RLIMIT_FSIZE,16*1024**2),(resource.RLIMIT_CPU,90)):
  resource.setrlimit(kind,(amount,amount))
 # Chromium/V8 reserves large sparse virtual cages. Guest physical RAM, host
 # QEMU budget and total wall time remain separate hard boundaries.
 address=1024*1024**2 if uid==65531 else 16*1024**4
 resource.setrlimit(resource.RLIMIT_AS,(address,address))

def stop_children(processes):
 for _ in range(100):
  values=[]
  for entry in pathlib.Path('/proc').iterdir():
   if not entry.name.isdigit():continue
   try:
    data=dict(line.split(':',1) for line in (entry/'status').read_text().splitlines() if ':' in line)
    if int(data['Uid'].split()[0]) in (65531,65532) and data['State'].strip()[0] not in 'ZX':values.append(int(entry.name))
   except (FileNotFoundError,ProcessLookupError):pass
  if not values:break
  for pid in values:
   try:os.kill(pid,signal.SIGKILL)
   except ProcessLookupError:pass
  time.sleep(.01)
 else:raise RuntimeError('payload_alive')
 for process in processes:
  try:process.wait(timeout=1)
  except subprocess.TimeoutExpired:raise RuntimeError('payload_alive')

def main():
 config=load_config();processes=[]
 with os.fdopen(os.open(port_path(),os.O_RDWR|os.O_NOFOLLOW),'r+b',buffering=0) as port:
  total=0
  def send(value):
   nonlocal total
   raw=canonical(value)
   if len(raw)>MAX_FRAME:raise RuntimeError('frame')
   framed=struct.pack('!I',len(raw))+raw;total+=len(framed)
   if total>MAX_SPOOL:raise RuntimeError('spool')
   view=memoryview(framed);deadline=time.monotonic()+5
   while view:
    if time.monotonic()>=deadline:raise RuntimeError('port_write_timeout')
    try:count=port.write(view)
    except BlockingIOError:count=None
    if count is None:
     with selectors.DefaultSelector() as writable:
      writable.register(port,selectors.EVENT_WRITE);writable.select(.05)
     continue
    if count<=0:raise RuntimeError('port_write')
    view=view[count:]
  port.write(MAGIC)
  to_r,to_w=os.pipe();from_r,from_w=os.pipe()
  home=pathlib.Path('/tmp/chromium');home.mkdir(mode=0o700);os.chown(home,65532,65532)
  driver_home=pathlib.Path('/tmp/driver');driver_home.mkdir(mode=0o700);os.chown(driver_home,65531,65531)
  try:
   chrome=subprocess.Popen(['/usr/lib/chromium/chromium','--headless','--disable-gpu','--remote-debugging-pipe',
    '--no-startup-window','--disable-background-networking','--disable-component-update','--disable-sync',
    '--no-first-run','--no-default-browser-check','--user-data-dir='+str(home)],
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
    env={'PATH':'/usr/bin:/bin','HOME':str(home),'TMPDIR':'/tmp','DBUS_SESSION_BUS_ADDRESS':'unix:path=/tmp/no-dbus'},
    pass_fds=tuple(set((to_r,from_w,3,4))),preexec_fn=lambda:demote(65532,to_r,from_w))
   processes.append(chrome)
   driver=subprocess.Popen(['/usr/bin/node','/opt/poc/browser_driver.cjs'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
    env={'PATH':'/usr/bin:/bin','HOME':str(driver_home),'TMPDIR':'/tmp'},
    pass_fds=tuple(set((from_r,to_w,3,4))),preexec_fn=lambda:demote(65531,from_r,to_w))
   processes.append(driver)
  finally:
   for fd in (to_r,to_w,from_r,from_w):os.close(fd)
  driver.stdin.write(canonical(config)+b'\n');driver.stdin.flush()
  buffers={port.fileno():bytearray(),driver.stdout.fileno():bytearray()};stderr_size=0;started=time.monotonic();done=False;awaiting_ack=False
  for fd in (*buffers,chrome.stderr.fileno()):os.set_blocking(fd,False)
  with selectors.DefaultSelector() as selector:
   for fd in (*buffers,chrome.stderr.fileno()):selector.register(fd,selectors.EVENT_READ)
   try:
    while not done:
     if time.monotonic()-started>95:raise RuntimeError('timeout')
     # Drain the driver's last frame before treating pipe EOF as failure.
     # A successful driver can exit while its final PNG is still buffered.
     for key,_ in selector.select(.05):
      data=os.read(key.fd,65536)
      if key.fd==chrome.stderr.fileno():
       if not data:selector.unregister(key.fd)
       stderr_size+=len(data)
       if stderr_size>65536:raise RuntimeError('stderr')
       continue
      if not data:raise RuntimeError('pipe_eof')
      buffer=buffers[key.fd];buffer.extend(data)
      if len(buffer)>MAX_FRAME+4:raise RuntimeError('frame')
      if key.fd==port.fileno():
       while len(buffer)>=4:
        size=struct.unpack('!I',buffer[:4])[0]
        if not 1<=size<=MAX_FRAME:raise RuntimeError('frame')
        if len(buffer)<size+4:break
        raw=bytes(buffer[4:size+4]);del buffer[:size+4]
        value=json.loads(raw)
        if awaiting_ack:
         if value!={'kind':'result_ack'}:raise RuntimeError('result_ack')
         done=True;break
        driver.stdin.write(canonical(value)+b'\n');driver.stdin.flush()
      else:
       while b'\n' in buffer:
        raw,_,rest=buffer.partition(b'\n');buffer[:]=rest
        value=json.loads(raw);send(value)
        if value.get('kind')=='result':
         awaiting_ack=True;selector.unregister(driver.stdout.fileno());break
        if value.get('kind')=='error':done=True;break
   finally:
    stop_children(processes)
    for process in processes:
     for stream in (process.stdin,process.stdout,process.stderr):
      if stream is not None:stream.close()

if __name__=='__main__':
 try:main()
 except Exception:pass
