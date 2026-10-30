"""Private no-vNIC pixel preview. No service wiring or canonical task authority."""
from __future__ import annotations
import base64
from collections import deque
from contextlib import contextmanager
from datetime import datetime,timedelta
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import secrets
import selectors
import shutil
import struct
import subprocess
import threading
import time

from .errors import WorkRuntimeError
from .preview_protocol import (PreviewLease,PreviewFrames,canonical,event,png,render_ack,fail,
    MAX_FRAME,MAX_SPOOL,MAX_TOTAL)
from .qemu_work import QemuWorkBackend,QemuHostPolicy,_assets,_host_boundary,_copy_file,_copy_checkpoint,_archive_limit
from .work_execution import (MIB,SHANGHAI,WorkLimits,_private_dir,_write_new,_sync_dir,
    _checkpoint_tree,_checkpoint_ref,_name,extract_checkpoint)

LIMITS=WorkLimits(timeout_seconds=1800,cpu_millis=2000,memory_bytes=2048*MIB,
    workspace_bytes=MIB,pids=256,max_files=128,tmp_bytes=64*MIB)
STOP_RESERVE_SECONDS=10
FAILURE_STAGES=frozenset({'app','chrome','driver','driver_cdp_deadline','driver_reload_deadline','driver_capture_deadline','driver_cdp_failed','driver_capture_failed','driver_event','driver_request','driver_sequence','driver_init','driver_load_pending','driver_pixel_limit','driver_render_pending','driver_render_deadline','driver_render_changed','driver_boot_timeout','isolation','commands','commands_protocol'})

def _atomic(path,value):
    temporary=path.with_name('.pending-'+secrets.token_hex(12))
    _write_new(temporary,canonical(value));os.replace(temporary,path);_sync_dir(path.parent)


class _WorkerLifetime:
    """Nonserializable service capability for a persistent creating worker."""
    def __init__(self,issuer):
        self.issuer=issuer;self.owner=threading.current_thread();self.pid=os.getpid()
        self.active=True;self.session=None
    def alive(self):return self.active and self.pid==os.getpid() and self.owner.is_alive()
    def creates_here(self):return self.alive() and self.owner is threading.current_thread()


class QemuPreviewBackend(QemuWorkBackend):
    """Standalone candidate; start requires a persistent creating-worker lifetime.

    Linux parent-death fencing follows the creating thread. The trusted Work
    worker must keep worker_lifetime() open until a confirmed session stop.
    Per-request HTTP threads may call frame/action, but must not create QEMU.
    """
    def __init__(self,assets,manifest_digest,state_root,*,policy=QemuHostPolicy(
            max_workspace_bytes=MIB,max_host_memory_bytes=4096*MIB,max_host_pids=256)):
        policy.validate()
        self.policy=policy;self.asset=_assets(Path(assets),manifest_digest,expected_kind='preview-qemu-v1')
        if self.asset.manifest['resource_profile']!=policy.resource_profile:raise fail('preview_resource_profile_invalid')
        self.root=Path(state_root).absolute();_private_dir(self.root)
        if self.root.resolve()!=self.root:raise fail('preview_state_invalid')
        self._qualified=set();self._lock=threading.Lock()
        self._process_lock=None
        self._lifetime_issuer=object()
        self.reserve_bytes=sum(item['size_bytes'] for item in self.asset.manifest['files'])+192*MIB

    @contextmanager
    def worker_lifetime(self):
        lifetime=_WorkerLifetime(self._lifetime_issuer)
        try:yield lifetime
        finally:
            lifetime.active=False
            if lifetime.session is not None and not lifetime.session._done.is_set():
                lifetime.session._terminate('worker_lifetime_ended',uncertain=True)

    def _pending(self):
        for path in self.root.glob('PVM-*/journal.json'):
            try:
                if path.is_symlink() or path.stat().st_size>16384:raise ValueError()
                value=json.loads(path.read_bytes())
                receipt=value.get('receipt',{})
                if (value.get('stage')!='stopped' or not isinstance(receipt,dict)
                        or receipt.get('stopped') is not True or receipt.get('uncertain') is not False):return True
            except (OSError,ValueError,TypeError):return True
        return False

    def _acquire_process_lock(self):
        fd=os.open(self.root/'preview.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            os.close(fd);raise fail('preview_busy') from None
        self._process_lock=fd

    def _release(self):
        try:
            if self._process_lock is not None:
                fcntl.flock(self._process_lock,fcntl.LOCK_UN);os.close(self._process_lock);self._process_lock=None
        finally:self._lock.release()

    def start(self,lease:PreviewLease,program:str,*,gate,stop_gate=None,worker_lifetime=None,inputs=None,restore=None,interpreter='python3',cancelled=None,now=None):
        if (not isinstance(worker_lifetime,_WorkerLifetime) or worker_lifetime.issuer is not self._lifetime_issuer
                or not worker_lifetime.creates_here()):raise fail('preview_worker_lifetime_required')
        now=now or (lambda:datetime.now(SHANGHAI))
        if not isinstance(lease,PreviewLease):raise fail('preview_scope_invalid')
        lease.validate(now())
        if (lease.expires_at-now()).total_seconds()<=STOP_RESERVE_SECONDS:raise fail('preview_lease_too_short')
        if stop_gate is not None and not callable(stop_gate):raise fail('preview_stop_gate_invalid')
        if (not callable(gate) or cancelled is not None and not callable(cancelled)
                or interpreter not in {'python3','node'} or not isinstance(program,str)
                or not 1<=len(program.encode())<=65536):raise fail('preview_configuration_invalid')
        content=dict(inputs or {})
        if len(content)>128 or sum(len(v) for v in content.values() if isinstance(v,bytes))>MIB:raise fail('preview_input_limit')
        for name,data in content.items():
            _name(name)
            if type(data) is not bytes:raise fail('preview_input_invalid')
            if any(name.startswith(other+'/') for other in content if other!=name):raise fail('preview_input_invalid')
        if not self._lock.acquire(blocking=False):raise fail('preview_busy')
        session=None;attempt=None
        try:
            self._acquire_process_lock()
            if self._pending():raise fail('preview_recovery_required',uncertain=True)
            boundary=_host_boundary(self.policy)
            if self.policy.resource_profile=='native-resource-v1':self._native_address(LIMITS.memory_bytes,boundary)
            asset=_assets(self.asset.root,self.asset.digest,expected_kind='preview-qemu-v1');self._qualify(asset)
            if shutil.disk_usage(self.root).free<self.reserve_bytes:raise fail('preview_storage_budget')
            session=PreviewSession(self,lease,gate,cancelled or (lambda:False),now,worker_lifetime,stop_gate)
            worker_lifetime.session=session
            session._start(program,content,restore,interpreter)
            return session
        except BaseException:
            if session is not None:
                session._terminate('start_failed',uncertain=session.process is not None)
            else:self._release()
            raise


class PreviewSession:
    def __init__(self,backend,lease,gate,cancelled,now,worker_lifetime,stop_gate=None):
        self.backend=backend;self.lease=lease;self.gate=gate;self.cancelled=cancelled;self.now=now
        self.worker_lifetime=worker_lifetime
        self.stop_gate=stop_gate;self._stop_gate_unknown=False
        self.reference='PVM-'+secrets.token_hex(20);self.nonce=secrets.token_hex(32)
        self.attempt=backend.root/self.reference;_private_dir(self.attempt)
        self.journal=self.attempt/'journal.json';self.jail=self.attempt/'jail'
        self.deadline=time.monotonic()+(lease.expires_at-now()).total_seconds()
        self.admission_deadline=self.deadline-STOP_RESERVE_SECONDS
        self.admission_expires_at=lease.expires_at-timedelta(seconds=STOP_RESERVE_SECONDS)
        self.process=None;self.pidfd=None;self.reader=None;self.offset=0;self.stderr_size=0
        self.decoder=PreviewFrames();self.messages=deque();self.sequence=0;self.used=0;self.tokens=4.;self.updated=time.monotonic()
        self._io=threading.Lock();self._state=threading.Lock();self._done=threading.Event()
        self.closing=False;self.ready=False;self.receipt=None;self.observed_isolation=None;self.pending=False;self.render_pending=False
        self._persist('claimed')

    def _persist(self,stage,**values):
        _atomic(self.journal,{'version':1,'stage':stage,'execution_ref':self.reference,
            'scope':self.lease.document(),'nonce':self.nonce,'sequence':self.sequence,
            'recorded_at':self.now().astimezone(SHANGHAI).isoformat(),**values})

    def _absolute_live(self):
        try:
            if not self.worker_lifetime.alive():return False
            self.lease.validate(self.now())
            return time.monotonic()<self.deadline
        except Exception:return False

    def _scope_live(self):
        try:return self._absolute_live() and not self.cancelled() and self.gate(self.lease) is True
        except Exception:return False

    def _live(self):return self._scope_live() and time.monotonic()<self.admission_deadline

    def _export_authorized(self):
        if not self._absolute_live() or self.pending:return False
        if self.stop_gate is None:return self._scope_live()
        try:
            allowed=self.stop_gate(self.lease)
            if type(allowed) is not bool:self._stop_gate_unknown=True;return False
            return allowed
        except Exception:self._stop_gate_unknown=True;return False

    def _start(self,program,inputs,restore,interpreter):
        asset=self.backend.asset
        if not self._live():raise fail('preview_lease_expired')
        _private_dir(self.jail);_private_dir(self.jail/'runtime');_private_dir(self.jail/'tmp')
        for item in asset.manifest['files']:
            source=asset.root/item['name']
            _copy_file(source,self.jail/'runtime'/item['name'],item['sha256'],mode=0o555 if source.stat().st_mode&0o111 else 0o444)
        _copy_file(asset.root/'manifest.json',self.jail/'runtime/manifest.json',asset.digest,mode=0o444)
        bundle=self.attempt/'bundle';_private_dir(bundle);_private_dir(bundle/'files')
        for name,data in inputs.items():
            _write_new(bundle/'files'/name,data);(bundle/'files'/name).chmod(0o444)
        for directory in sorted((p for p in (bundle/'files').rglob('*') if p.is_dir()),reverse=True):directory.chmod(0o555)
        (bundle/'files').chmod(0o555)
        _write_new(bundle/'program',program.encode())
        (bundle/'program').chmod(0o444)
        ttl=max(1,math.floor(min(1800,self.deadline-time.monotonic())))
        _write_new(bundle/'request.json',canonical({'version':1,'nonce':self.nonce,'scope_digest':self.lease.digest,'ttl_seconds':ttl,'interpreter':interpreter}))
        bundle.chmod(0o555)
        try:self.backend._disk(asset,self.jail/'input.img',bundle,33*MIB)
        finally:
            bundle.chmod(0o700)
            for directory in (p for p in bundle.rglob('*') if p.is_dir()):directory.chmod(0o700)
        restored=self.attempt/'restore';_private_dir(restored)
        if restore is not None:_copy_checkpoint(Path(restore),restored,LIMITS)
        self.backend._disk(asset,self.jail/'output.img',restored,33*MIB)
        _write_new(self.jail/'export.bin',b'');self.backend._seal_jail(self.jail)
        if os.statvfs(self.jail/'runtime').f_flag&os.ST_NOEXEC:raise fail('preview_runtime_noexec')
        if not self._live():raise fail('preview_lease_expired')
        m=asset.manifest
        command=[*self.backend._launcher_command(asset,self.jail,MAX_SPOOL,ttl*2+30,LIMITS.memory_bytes),
            '/runtime/'+m['loader'],'--inhibit-cache','--library-path','/runtime/lib','/runtime/'+m['qemu'],
            '-L','/runtime/share/qemu','-accel','tcg,tb-size=128','-machine','pc','-smp','2','-m','2048',
            '-nodefaults','-no-user-config','-display','none','-serial','none','-monitor','none','-no-reboot','-nic','none',
            '-kernel','/runtime/'+m['guest_kernel'],'-initrd','/runtime/'+m['guest_initramfs'],'-append','rdinit=/init panic=-1 quiet',
            '-drive','file=/input.img,format=raw,if=virtio,readonly=on','-drive','file=/output.img,format=raw,if=virtio',
            '-device','virtio-serial-pci','-chardev','stdio,id=preview,signal=off','-device','virtserialport,chardev=preview,name=codex-preview']
        fd=os.open(self.jail/'export.bin',os.O_WRONLY|os.O_NOFOLLOW)
        try:self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=fd,stderr=subprocess.PIPE,
            env={'LANG':'C'},close_fds=True,start_new_session=True)
        finally:os.close(fd)
        try:self.pidfd=os.pidfd_open(self.process.pid)
        except OSError:
            if self.process.poll() is None:self.process.kill()
            self.process.wait(timeout=self.backend.policy.cleanup_seconds)
            raise fail('preview_pidfd_unavailable') from None
        os.set_blocking(self.process.stdin.fileno(),False);os.set_blocking(self.process.stderr.fileno(),False)
        self.reader=os.open(self.jail/'export.bin',os.O_RDONLY|os.O_NOFOLLOW)
        self._persist('starting')
        threading.Thread(target=self._watch,daemon=True,name='preview-lease').start()
        message=self._read(min(time.monotonic()+90,self.admission_deadline))
        if message.get('kind')=='stopped' and message.get('failure_stage') in FAILURE_STAGES:
            raise fail('preview_guest_failed_'+message['failure_stage'])
        expected={'interfaces':['lo'],'app_uid':65532,'chrome_uid':65531,'driver_uid':65530,'renderer_seccomp':True,'renderer_pid_namespace':True}
        if set(message)!={'kind','nonce','scope_digest','isolation'} or message['kind']!='ready' or message['isolation']!=expected:raise fail('preview_guest_not_qualified')
        if not self._live() or self.closing:raise fail('preview_lease_expired')
        self.observed_isolation=message['isolation'];self.ready=True;self._persist('ready')

    def _watch(self):
        while not self._done.wait(.05):
            if self.closing:continue
            if not self.worker_lifetime.alive():self._terminate('worker_lifetime_lost',uncertain=True);return
            if not self._live():
                reason='lease_expired' if not self._absolute_live() else 'drain' if time.monotonic()>=self.admission_deadline else 'admission_closed'
                self._terminate(reason,uncertain=False,graceful=True);return
            if self.process is not None and self.process.poll() is not None:self._terminate('unexpected_exit',uncertain=True);return

    def _read(self,deadline,*,allow_closing=False):
        while True:
            if self.closing and not allow_closing:raise fail('preview_closed')
            if allow_closing and not self._export_authorized():raise fail('preview_stop_authority_lost',uncertain=True)
            if time.monotonic()>=deadline:raise fail('preview_deadline',uncertain=True)
            try:data=os.read(self.process.stderr.fileno(),4096)
            except BlockingIOError:data=b''
            self.stderr_size+=len(data)
            if self.stderr_size>65536:raise fail('preview_stderr_limit')
            size=os.fstat(self.reader).st_size
            if size>MAX_SPOOL:raise fail('preview_spool_limit')
            data=os.pread(self.reader,min(65536,max(0,size-self.offset)),self.offset);self.offset+=len(data)
            self.messages.extend(self.decoder.feed(data))
            if self.messages:
                message=self.messages.popleft()
                if message.get('nonce')!=self.nonce or message.get('scope_digest')!=self.lease.digest:raise fail('preview_scope_mismatch')
                return message
            if self.process.poll() is not None:raise fail('preview_vm_exited',uncertain=True)
            if not data:time.sleep(.01)

    def _write(self,message,deadline):
        raw=canonical({**message,'nonce':self.nonce})
        if len(raw)>8192:raise fail('preview_event_invalid')
        data=memoryview(struct.pack('!I',len(raw))+raw)
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdin,selectors.EVENT_WRITE)
            while data:
                if time.monotonic()>=deadline:raise fail('preview_deadline',uncertain=True)
                if not selector.select(min(.05,max(0,deadline-time.monotonic()))):continue
                try:count=os.write(self.process.stdin.fileno(),data)
                except BlockingIOError:continue
                if not count:raise fail('preview_pipe_failed',uncertain=True)
                data=data[count:]

    def _finish_protocol(self):
        # Only after pidfd-confirmed exit can the terminal spool be complete.
        size=os.fstat(self.reader).st_size
        if size>MAX_SPOOL:raise fail('preview_spool_limit')
        while self.offset<size:
            data=os.pread(self.reader,min(65536,size-self.offset),self.offset)
            if not data:raise fail('preview_protocol_incomplete')
            self.offset+=len(data);self.messages.extend(self.decoder.feed(data))
        self.decoder.finish()
        if self.messages:raise fail('preview_response_invalid')

    def frame(self,*,deadline=None):return self._request(None,deadline)
    def action(self,value,*,deadline=None):
        checked,_=event(value);return self._request(checked,deadline)
    def _request(self,value,deadline):
        if not self._io.acquire(blocking=False):raise fail('preview_busy')
        dispatched=False
        try:
            if not self.ready or self.closing or not self._live():raise fail('preview_not_ready')
            if value is not None and self.render_pending:raise fail('preview_render_pending')
            instant=time.monotonic();self.tokens=min(4,self.tokens+(instant-self.updated)*2);self.updated=instant
            if self.tokens<1:raise fail('preview_rate_limited')
            self.tokens-=1
            if self.sequence>=4096 or self.used+4*MIB>MAX_TOTAL:raise fail('preview_budget_exhausted')
            deadline=min(deadline if deadline is not None else instant+5,instant+5,self.admission_deadline)
            if deadline<=instant:raise fail('preview_deadline')
            self.sequence+=1
            request={'kind':'frame' if value is None else 'action','id':self.sequence}
            if value is not None:request['event']=value
            self._persist('pending',request_digest='sha256:'+hashlib.sha256(canonical(request)).hexdigest())
            self.pending=True;dispatched=True
            if self.closing or not self._live() or time.monotonic()>=deadline:raise fail('preview_lease_expired',uncertain=True)
            self._write(request,deadline);message=self._read(deadline)
            if message.get('kind')=='stopped' and message.get('failure_stage') in FAILURE_STAGES:
                raise fail('preview_guest_failed_'+message['failure_stage'],uncertain=True)
            if value is not None and value['kind']=='refresh':
                if (set(message)!={'kind','id','nonce','scope_digest','state'} or message['kind']!='action_ack'
                        or type(message['id']) is not int or message['id']!=self.sequence):raise fail('preview_response_invalid')
                result=render_ack({'state':message['state']})
                if self.closing or not self._live() or time.monotonic()>=deadline:raise fail('preview_lease_expired')
                self.pending=False;self.render_pending=True;self._persist('render_pending')
                if self.closing or not self._live() or time.monotonic()>=deadline:raise fail('preview_deadline',uncertain=True)
                return result
            if (set(message)!={'kind','id','nonce','scope_digest','mime','width','height','image_b64'}
                    or message['kind']!='frame' or type(message['id']) is not int or message['id']!=self.sequence):raise fail('preview_response_invalid')
            result,size=png({k:message[k] for k in ('mime','width','height','image_b64')})
            if self.closing or not self._live() or time.monotonic()>=deadline:raise fail('preview_lease_expired')
            self.used+=size+(len(value.get('text','').encode()) if value is not None else 0)
            if self.used>MAX_TOTAL:raise fail('preview_budget_exhausted')
            self.pending=False;self.render_pending=False;self._persist('ready')
            if self.closing or not self._live() or time.monotonic()>=deadline:raise fail('preview_deadline',uncertain=True)
            return result
        except BaseException:
            if dispatched:self._terminate('unknown_action',uncertain=True)
            raise
        finally:self._io.release()

    def checkpoint_directory(self):
        receipt=self.receipt
        if (not self._done.is_set() or not isinstance(receipt,dict) or receipt.get('stopped') is not True
                or receipt.get('uncertain') is not False or receipt.get('checkpoint_confirmed') is not True):return None
        if self.process is None or self.process.poll() is None:raise fail('preview_checkpoint_changed',uncertain=True)
        destination=self.attempt/'checkpoint'
        try:reference=_checkpoint_ref(_checkpoint_tree(destination,LIMITS))
        except Exception:raise fail('preview_checkpoint_changed',uncertain=True) from None
        if reference!=receipt['checkpoint_ref']:raise fail('preview_checkpoint_changed',uncertain=True)
        return destination

    def stop(self,*,reason='stopped'):
        if reason not in {'stopped','pause','cancel','revise','drain'}:raise fail('preview_stop_reason_invalid')
        return self._terminate(reason,uncertain=False,graceful=True)
    def _terminate(self,reason,*,uncertain,graceful=False):
        with self._state:
            if self.closing:
                first=False
            else:
                self.closing=True;self.ready=False;first=True
                uncertain=uncertain or self.pending or self._io.locked()
                lifetime=getattr(self,'worker_lifetime',None)
                if lifetime is not None and not lifetime.alive():uncertain=True
        if not first:
            if not self._done.wait(self.backend.policy.cleanup_seconds*2+3) or self.receipt is None:
                raise fail('preview_cleanup_uncertain',uncertain=True)
            return self.receipt
        stopped=False;checkpoint_ref=None;files=[];grace=None
        try:
            try:self._persist('stopping',reason=reason,uncertain=uncertain)
            except Exception:uncertain=True
            if self.process is not None:
                can_export=(graceful and not uncertain and self.observed_isolation is not None and self._export_authorized())
                uncertain=uncertain or self._stop_gate_unknown
                if can_export and self.pidfd is not None and self.process.poll() is None and self._io.acquire(blocking=False):
                    try:
                        deadline=min(time.monotonic()+2,self.deadline)
                        self._write({'kind':'stop','id':self.sequence+1},deadline)
                        grace=self._read(deadline,allow_closing=True)
                        self.process.wait(timeout=max(.1,deadline-time.monotonic()))
                    except Exception:grace=None
                    finally:self._io.release()
                if self.process.poll() is None:
                    if self.pidfd is None:raise fail('preview_cleanup_uncertain',uncertain=True)
                    self.backend._stop(self.process,self.pidfd)
                self.process.wait(timeout=self.backend.policy.cleanup_seconds);stopped=True
            else:stopped=True
            if (grace is not None and not uncertain and self._export_authorized() and self.process.returncode==0
                    and set(grace)=={'kind','id','nonce','scope_digest','reason','failure_stage','archive_b64','archive_sha256','archive_bytes'}
                    and grace['kind']=='stopped' and grace['reason']=='stopped' and grace['failure_stage'] is None and type(grace['id']) is int and grace['id']==self.sequence+1
                    and isinstance(grace['archive_b64'],str) and type(grace['archive_bytes']) is int
                    and 0<grace['archive_bytes']<=_archive_limit(LIMITS) and len(grace['archive_b64'])<=4*((_archive_limit(LIMITS)+2)//3)):
                self._finish_protocol()
                raw=base64.b64decode(grace['archive_b64'],validate=True)
                if (len(raw)!=grace['archive_bytes'] or base64.b64encode(raw).decode()!=grace['archive_b64']
                        or 'sha256:'+hashlib.sha256(raw).hexdigest()!=grace['archive_sha256']):raise fail('preview_checkpoint_invalid')
                destination=self.attempt/'checkpoint';_private_dir(destination)
                files=extract_checkpoint(io.BytesIO(raw),destination,LIMITS,require_manifest=True)
                verified_ref=_checkpoint_ref(_checkpoint_tree(destination,LIMITS))
                if self._export_authorized():checkpoint_ref=verified_ref
                else:files=[]
            uncertain=uncertain or self._stop_gate_unknown
            if uncertain:checkpoint_ref=None;files=[]
            self.receipt={'version':1,'execution_ref':self.reference,'scope':self.lease.document(),
                'stopped':stopped,'stop_reason':reason,'uncertain':uncertain,'checkpoint_ref':checkpoint_ref,
                'checkpoint_confirmed':checkpoint_ref is not None,'checkpoint_authority':('stop_gate' if self.stop_gate is not None else 'admission_gate') if checkpoint_ref is not None else None,
                'admission_expires_at':self.admission_expires_at.astimezone(SHANGHAI).isoformat(),
                'files':files,'recorded_at':self.now().astimezone(SHANGHAI).isoformat()}
            self._persist('stopped_unknown' if uncertain else 'stopped',receipt=self.receipt)
            return self.receipt
        except BaseException:
            try:self._persist('recovery_required',stop_confirmed=stopped)
            except Exception:pass
            raise
        finally:
            try:
                if stopped:
                    for stream in (() if self.process is None else (self.process.stdin,self.process.stderr)):
                        if stream is not None:stream.close()
                    if self.pidfd is not None:os.close(self.pidfd);self.pidfd=None
                    if self.reader is not None:os.close(self.reader);self.reader=None
                    if self.jail.exists():self.backend._remove_run(self.jail)
            except BaseException:
                try:self._persist('recovery_required',stop_confirmed=stopped)
                except Exception:pass
                raise
            finally:self._done.set();self.backend._release()
