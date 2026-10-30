"""Offline browser VM with bounded exact-grant fetch IPC; no task inputs."""
from __future__ import annotations

import base64
from dataclasses import replace
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import struct
import subprocess
import threading
import time
from typing import Callable

from .errors import WorkRuntimeError
from .browser_broker_process import BrokerProcess
from .public_browser import (BrowserResult, PublicBrowserGrant, PublicFetchBroker,
    MAX_FRAME, MAX_RESOURCE, MAX_TOTAL, validate_browser_result)
from .qemu_work import (QemuHostPolicy, QemuWorkBackend, _assets, _canonical, _copy_file,
    _host_boundary)
from .work_execution import MIB, SHANGHAI, _private_dir, _write_new

MAGIC=b'CWB1\n'
SPOOL_LIMIT=16*MIB
LIFECYCLE_SECONDS=120
CLEANUP_RESERVE_SECONDS=6


def _fail(code: str, *, uncertain=False):
    return WorkRuntimeError(code,'Public browser VM policy rejected the operation.',uncertain=uncertain)


def _decode(raw: bytes) -> dict:
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: raise ValueError('duplicate')
            result[key]=value
        return result
    try:
        value=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
        if not isinstance(value,dict): raise ValueError('object')
        return value
    except (ValueError,TypeError,UnicodeError):
        raise _fail('browser_vm_protocol_invalid') from None


class BrowserFrames:
    """Bounded streaming decoder; data does not imply authorization."""
    def __init__(self):
        self.buffer=bytearray();self.total=0;self.started=False
    def feed(self,data: bytes) -> list[dict]:
        self.total+=len(data)
        if self.total>SPOOL_LIMIT: raise _fail('browser_vm_spool_limit')
        self.buffer.extend(data)
        if not self.started:
            if len(self.buffer)<len(MAGIC): return []
            if self.buffer[:len(MAGIC)]!=MAGIC: raise _fail('browser_vm_protocol_invalid')
            del self.buffer[:len(MAGIC)];self.started=True
        result=[]
        while len(self.buffer)>=4:
            size=struct.unpack('!I',self.buffer[:4])[0]
            if not 1<=size<=MAX_FRAME: raise _fail('browser_vm_frame_limit')
            if len(self.buffer)<size+4: break
            raw=bytes(self.buffer[4:size+4]);del self.buffer[:size+4]
            result.append(_decode(raw))
        if len(self.buffer)>MAX_FRAME+4: raise _fail('browser_vm_frame_limit')
        return result
    def finish(self):
        if not self.started or self.buffer: raise _fail('browser_vm_protocol_incomplete')


def validate_fetch(message: dict, expected_id: int) -> tuple[str,str]:
    if (set(message)!={'kind','id','url','method'} or message['kind']!='fetch'
        or type(message['id']) is not int or message['id']!=expected_id or not 1<=expected_id<=64
        or not isinstance(message['url'],str) or not 1<=len(message['url'])<=4096
        or not isinstance(message['method'],str) or message['method'] not in {'GET','HEAD'}):
        raise _fail('browser_vm_fetch_invalid')
    return message['url'],message['method']


class QemuPublicBrowser(QemuWorkBackend):
    """Single fixed browser runtime. Production browse always uses a real broker."""
    def __init__(self, assets: str|Path, manifest_digest: str, state_root: str|Path, *,
            policy: QemuHostPolicy=QemuHostPolicy(max_workspace_bytes=MIB,
                max_host_memory_bytes=4096*MIB,max_host_pids=64)):
        policy.validate();self.policy=policy
        self.asset=_assets(Path(assets),manifest_digest,expected_kind='browser-qemu-v1')
        if self.asset.manifest['resource_profile']!=policy.resource_profile:
            raise _fail('work_qemu_resource_profile_mismatch')
        self.root=Path(state_root).absolute();_private_dir(self.root)
        if self.root.resolve()!=self.root: raise _fail('browser_vm_state_invalid')
        self._qualified=set();self._lock=threading.Lock();self._frozen=False;self._revoked={};self._uncertain=None
        self.reserve_bytes=sum(item['size_bytes'] for item in self.asset.manifest['files'])+96*MIB

    def browse(self,grant: PublicBrowserGrant,url: str,*,screenshot: bool=True,
            cancelled: Callable[[],bool]|None=None) -> BrowserResult:
        cpus=self.policy.native_cpu_count if self.policy.resource_profile=='native-resource-v1' else 2
        broker=BrokerProcess(grant,cpu_count=cpus,cleanup_seconds=self.policy.cleanup_seconds)
        return self._run(grant,url,screenshot,broker,cancelled=cancelled)

    @staticmethod
    def _write_response(process, message: dict, deadline: float, guard=None):
        raw=_canonical(message)
        if len(raw)>MAX_FRAME: raise _fail('browser_vm_frame_limit')
        view=memoryview(struct.pack('!I',len(raw))+raw)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin,selectors.EVENT_WRITE)
            while view:
                if guard is not None:guard()
                remaining=deadline-time.monotonic()
                if remaining<=0: raise _fail('browser_timeout')
                if not selector.select(min(.1,remaining)):
                    if process.poll() is not None: raise _fail('browser_vm_worker_failed')
                    continue
                try:count=os.write(process.stdin.fileno(),view)
                except BlockingIOError:continue
                except OSError:raise _fail('browser_vm_worker_failed') from None
                if not count:raise _fail('browser_vm_worker_failed')
                view=view[count:]

    def _browser_command(self,jail: Path) -> list[str]:
        manifest=self.asset.manifest
        cpu_seconds=240 if self.policy.resource_profile=='native-resource-v1' else 120
        return [*self._launcher_command(self.asset,jail,SPOOL_LIMIT,cpu_seconds,2048*MIB),
            '/runtime/'+manifest['loader'],'--inhibit-cache','--library-path','/runtime/lib',
            '/runtime/'+manifest['qemu'],'-L','/runtime/share/qemu','-accel','tcg,tb-size=64',
            '-machine','pc','-smp','2','-m','2048','-nodefaults','-no-user-config',
            '-display','none','-serial','none','-monitor','none','-no-reboot','-nic','none',
            '-kernel','/runtime/'+manifest['guest_kernel'],'-initrd','/runtime/'+manifest['guest_initramfs'],
            '-append','rdinit=/init panic=-1 quiet',
            '-drive','file=/input.img,format=raw,if=virtio,readonly=on',
            '-device','virtio-serial-pci','-chardev','stdio,id=browser,signal=off',
            '-device','virtserialport,chardev=browser,name=codex-browser']

    def _run(self,grant: PublicBrowserGrant,url: str,screenshot: bool,broker,
            *,cancelled: Callable[[],bool]|None=None) -> BrowserResult:
        if getattr(self,'_frozen',False):raise _fail('browser_vm_frozen',uncertain=True)
        if grant.grant_ref in getattr(self,'_revoked',{}):raise _fail('browser_cancelled')
        grant.require(url)
        if type(screenshot) is not bool: raise _fail('browser_action_invalid')
        if cancelled is not None and not callable(cancelled):raise _fail('browser_action_invalid')
        if not self._lock.acquire(blocking=False): raise _fail('browser_vm_busy')
        process=None;pidfd=None;run=None;stopped=True;complete=False;revoked=False
        original_expiry=grant.monotonic_deadline
        try:
            budget=min(LIFECYCLE_SECONDS,grant.remaining())-CLEANUP_RESERVE_SECONDS
            if budget<=0:raise _fail('browser_grant_expired')
            deadline=time.monotonic()+budget
            grant=replace(grant,monotonic_deadline=min(grant.monotonic_deadline,deadline),
                expires_at=min(grant.expires_at,dt.datetime.now(SHANGHAI)+dt.timedelta(seconds=budget)))
            broker.grant=grant
            def active():
                nonlocal revoked
                if not revoked and cancelled is not None:
                    try:
                        state=cancelled()
                        revoked=type(state) is not bool or state
                    except Exception:revoked=True
                if revoked:
                    if not hasattr(self,'_revoked'):self._revoked={}
                    self._revoked={key:expiry for key,expiry in self._revoked.items() if expiry>time.monotonic()}
                    if len(self._revoked)>=256:self._frozen=True
                    self._revoked[grant.grant_ref]=original_expiry
                    raise _fail('browser_cancelled')
                grant.remaining()
                if time.monotonic()>=deadline:raise _fail('browser_timeout')
            if isinstance(broker,BrokerProcess):broker.guard=active
            active()
            boundary=_host_boundary(self.policy)
            if self.policy.resource_profile=='native-resource-v1':self._native_address(2048*MIB,boundary)
            asset=_assets(self.asset.root,self.asset.digest,expected_kind='browser-qemu-v1')
            self._qualify(asset)
            if os.statvfs(self.root).f_bavail*os.statvfs(self.root).f_frsize<self.reserve_bytes:
                raise _fail('browser_vm_storage_budget')
            run=self.root/('BVM-'+secrets.token_hex(20));_private_dir(run)
            jail=run/'jail';_private_dir(jail);_private_dir(jail/'runtime');_private_dir(jail/'tmp')
            for item in asset.manifest['files']:
                source=asset.root/item['name']
                _copy_file(source,jail/'runtime'/item['name'],item['sha256'],mode=0o555 if source.stat().st_mode&0o111 else 0o444)
            _copy_file(asset.root/'manifest.json',jail/'runtime/manifest.json',asset.digest,mode=0o444)
            config=run/'config';_private_dir(config)
            _write_new(config/'config.json',_canonical({'version':1,'url':url,'screenshot':screenshot}))
            (config/'config.json').chmod(0o444);config.chmod(0o555)
            self._disk(asset,jail/'input.img',config,33*MIB)
            _write_new(jail/'output.img',b'');_write_new(jail/'export.bin',b'');self._seal_jail(jail)
            if os.statvfs(jail/'runtime').f_flag&os.ST_NOEXEC:raise _fail('browser_vm_runtime_noexec')
            grant.require(url)
            active()
            if time.monotonic()>=deadline:raise _fail('browser_timeout')
            fd=os.open(jail/'export.bin',os.O_WRONLY|os.O_NOFOLLOW)
            try:
                process=subprocess.Popen(self._browser_command(jail),stdin=subprocess.PIPE,stdout=fd,stderr=subprocess.PIPE,
                    env={'LANG':'C'},close_fds=True,start_new_session=True)
            finally:os.close(fd)
            stopped=False
            try:pidfd=os.pidfd_open(process.pid)
            except OSError:
                if process.poll() is None:process.kill()
                process.wait(timeout=self.policy.cleanup_seconds);stopped=True
                raise _fail('browser_vm_pidfd_unavailable') from None
            os.set_blocking(process.stdin.fileno(),False);os.set_blocking(process.stderr.fileno(),False)
            reader=os.open(jail/'export.bin',os.O_RDONLY|os.O_NOFOLLOW)
            decoder=BrowserFrames();offset=0;last_id=0;rejected=0;result=None;stderr_size=0;total_body=0
            try:
                while True:
                    active()
                    try:chunk=os.read(process.stderr.fileno(),4096)
                    except BlockingIOError:chunk=b''
                    stderr_size+=len(chunk)
                    if stderr_size>self.policy.max_stderr_bytes:raise _fail('browser_vm_output_limit')
                    size=os.fstat(reader).st_size
                    if size>SPOOL_LIMIT:raise _fail('browser_vm_spool_limit')
                    data=os.pread(reader,min(65536,max(0,size-offset)),offset);offset+=len(data)
                    for message in decoder.feed(data):
                        if result is not None:raise _fail('browser_vm_protocol_invalid')
                        if message.get('kind')=='fetch':
                            request_url,method=validate_fetch(message,last_id+1);last_id+=1
                            try:
                                active()
                                grant.require(request_url)
                                response=broker.fetch(request_url,method)
                                active()
                                grant.require(request_url)
                                if (response.url!=request_url or type(response.status) is not int or not 100<=response.status<=599
                                    or not isinstance(response.content_type,str) or len(response.content_type)>128
                                    or type(response.body) is not bytes or len(response.body)>MAX_RESOURCE
                                    or response.location is not None and not isinstance(response.location,str)):
                                    raise _fail('browser_vm_response_invalid')
                                if response.location is not None:grant.require(response.location)
                                total_body+=len(response.body)
                                if total_body>MAX_TOTAL:raise _fail('browser_total_limit')
                                reply={'id':last_id,'status':response.status,'content_type':response.content_type,
                                    'location':response.location,'body':base64.b64encode(response.body).decode()}
                            except WorkRuntimeError as error:
                                if revoked or error.uncertain:raise
                                rejected+=1
                                code=error.code if isinstance(error.code,str) and len(error.code)<=80 else 'browser_fetch_failed'
                                reply={'id':last_id,'error':code}
                            self._write_response(process,reply,deadline,active)
                        elif message.get('kind')=='result':
                            result=validate_browser_result(message,grant,screenshot=screenshot,rejected_requests=rejected)
                            self._write_response(process,{'kind':'result_ack'},deadline,active)
                        elif message.get('kind')=='error' and set(message)=={'kind','code'} and message['code']=='browser_guest_failed':
                            raise _fail('browser_vm_worker_failed')
                        else:raise _fail('browser_vm_protocol_invalid')
                    if process.poll() is not None and offset==os.fstat(reader).st_size:
                        process.wait(timeout=self.policy.cleanup_seconds);stopped=True;decoder.finish()
                        if process.returncode!=0 or result is None:raise _fail('browser_vm_worker_failed')
                        grant.require(result.url)
                        active();complete=True
                        return result
                    if not data:time.sleep(.02)
            finally:os.close(reader)
        finally:
            try:
                cleanup_error=None
                try:
                    if isinstance(broker,BrokerProcess):broker.close()
                except Exception as error:cleanup_error=error
                if process is not None and not stopped:
                    try:
                        if pidfd is None:raise _fail('browser_vm_cleanup_uncertain',uncertain=True)
                        if process.poll() is None:
                            try:signal.pidfd_send_signal(pidfd,signal.SIGKILL)
                            except ProcessLookupError:pass
                            process.wait(timeout=self.policy.cleanup_seconds)
                        if process.poll() is None:raise _fail('browser_vm_cleanup_uncertain',uncertain=True)
                        stopped=True
                    except Exception as error:cleanup_error=error
                if cleanup_error is not None:
                    self._frozen=True
                    self._uncertain=(process,pidfd,broker,run)
                    raise _fail('browser_vm_cleanup_uncertain',uncertain=True) from None
                if process is not None:
                    if process.stdin is not None:process.stdin.close()
                    if process.stderr is not None:process.stderr.close()
                if pidfd is not None:os.close(pidfd)
                if run is not None and stopped:
                    try:self._remove_run(run)
                    except Exception:
                        self._frozen=True
                        self._uncertain=(None,None,None,run)
                        raise _fail('browser_vm_cleanup_uncertain',uncertain=True) from None
                if complete:active()
            finally:self._lock.release()
