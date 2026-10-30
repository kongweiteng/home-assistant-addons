"""AES-256-GCM immutable replicas; no enrollment, key persistence or network setup.

The failure-domain name is an administrator assertion. A verified network mount
and successful fsync do not prove physical independence or NAS power-loss
durability. Those require separate deployment/recovery acceptance. Unit mountinfo
fixtures are not evidence of an off-device replica.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
from typing import Any, Iterator, Protocol

from .durability import ReplicaReceipt, Snapshot, digest

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # Metadata-only installations must remain importable.
    AESGCM = None


MAGIC = b'CDXREPL1\x00'
MAX_HEADER = 4096
OVERHEAD = len(MAGIC)+4+MAX_HEADER+12+16
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
SHA = re.compile(r'sha256:[a-f0-9]{64}\Z')
OBJECT = re.compile(r'ER-[a-f0-9]{64}-[a-f0-9]{64}\Z')
HEADER_FIELDS = {'version','algorithm','event_id','plain_sha256','plain_size','key_ref','failure_domain'}


class ReplicaError(RuntimeError):
    """Stable public code only; never retain keys, mount sources or raw errors."""
    def __init__(self,code: str):
        super().__init__(code)
        self.code=code


class ObjectStoreUnsupported(ReplicaError):
    pass


class ObjectStore(Protocol):
    """Trusted immutable CAS store, with stable writes and read-after-write.

    put_if_absent never replaces an object. True means created, False means an
    object already exists. An exception can mean unknown, requiring exact GET.
    Missing and unreadable/corrupt must never be conflated. GET must return only
    complete bounded objects, and must fail if stable-write semantics are absent.
    """
    def get(self,ref: str) -> bytes | None: ...
    def put_if_absent(self,ref: str,content: bytes) -> bool: ...


def _reference(value: Any) -> str:
    if not isinstance(value,str) or REF.fullmatch(value) is None:
        raise ReplicaError('replica_reference_invalid')
    return value


def _json(value: Any) -> bytes:
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()


def _pairs(items):
    result={}
    for key,value in items:
        if key in result: raise ValueError('duplicate')
        result[key]=value
    return result


def _constant(_):
    raise ValueError('nonfinite')


class EncryptedReplica:
    def __init__(self,store: ObjectStore,encryption_key: bytes,*,key_ref: str,
                 failure_domain: str,maxbytes: int = 128*1024*1024):
        if AESGCM is None:
            raise ReplicaError('replica_crypto_unavailable')
        if (not isinstance(encryption_key,bytes) or len(encryption_key)!=32
                or type(maxbytes) is not int or not 1<=maxbytes<=512*1024*1024
                or not callable(getattr(store,'get',None))
                or not callable(getattr(store,'put_if_absent',None))):
            raise ReplicaError('replica_config_invalid')
        self.store=store
        self.key_ref=_reference(key_ref)
        self.failure_domain=_reference(failure_domain)
        self.maxbytes=maxbytes
        # The key is never serialized, written, logged, or included in repr.
        self._cipher=AESGCM(encryption_key)

    @staticmethod
    def object_ref(event_id: str,sha256: str) -> str:
        _reference(event_id)
        if not isinstance(sha256,str) or SHA.fullmatch(sha256) is None:
            raise ReplicaError('replica_snapshot_invalid')
        return 'ER-'+hashlib.sha256(event_id.encode()).hexdigest()+'-'+sha256[7:]

    def _receipt(self,snapshot: Snapshot) -> ReplicaReceipt:
        if (not isinstance(snapshot,Snapshot) or not isinstance(snapshot.content,bytes)
                or not 1<=len(snapshot.content)<=self.maxbytes
                or digest(snapshot.content)!=snapshot.sha256):
            raise ReplicaError('replica_snapshot_invalid')
        return ReplicaReceipt(snapshot.event_id,snapshot.sha256,self.failure_domain,
                              self.object_ref(snapshot.event_id,snapshot.sha256))

    def _get(self,ref: str) -> bytes | None:
        try:
            value=self.store.get(ref)
        except ObjectStoreUnsupported:
            raise
        except Exception:
            raise ReplicaError('replica_store_unavailable') from None
        if value is not None and (not isinstance(value,bytes) or len(value)>self.maxbytes+OVERHEAD):
            raise ReplicaError('replica_envelope_invalid')
        return value

    def save(self,snapshot: Snapshot) -> ReplicaReceipt:
        receipt=self._receipt(snapshot)
        existing=self._get(receipt.object_ref)
        if existing is not None:
            if self.decrypt(receipt,existing)!=snapshot:
                raise ReplicaError('replica_snapshot_conflict')
            return receipt
        _,envelope=self.seal(snapshot)
        unknown=False
        try:
            created=self.store.put_if_absent(receipt.object_ref,envelope)
            if type(created) is not bool:
                raise ObjectStoreUnsupported('replica_store_semantics_unsupported')
        except ObjectStoreUnsupported:
            raise
        except Exception:
            unknown=True
        # Both a CAS loser and an unknown write reconcile the same immutable
        # object. Never retry PUT, invent another object, or compare random
        # ciphertexts: concurrent successful encryptions have different nonces.
        existing=self._get(receipt.object_ref)
        if existing is None:
            raise ReplicaError('replica_write_unknown' if unknown else 'replica_read_after_write_failed')
        if self.decrypt(receipt,existing)!=snapshot:
            raise ReplicaError('replica_snapshot_conflict')
        return receipt

    def seal(self,snapshot: Snapshot) -> tuple[ReplicaReceipt,bytes]:
        """Pure envelope codec: no storage access or durability assertion.

        Callers using a different storage failure model must establish their
        own write-once journal, full readback and qualification boundary.
        """
        receipt=self._receipt(snapshot)
        header={'version':1,'algorithm':'AES-256-GCM','event_id':snapshot.event_id,
                'plain_sha256':snapshot.sha256,'plain_size':len(snapshot.content),
                'key_ref':self.key_ref,'failure_domain':self.failure_domain}
        aad=_json(header)
        nonce=os.urandom(12)
        envelope=MAGIC+struct.pack('>I',len(aad))+aad+nonce+self._cipher.encrypt(nonce,snapshot.content,aad)
        return receipt,envelope

    def get(self,receipt: ReplicaReceipt) -> Snapshot:
        self._validate_receipt(receipt)
        envelope=self._get(receipt.object_ref)
        if envelope is None: raise ReplicaError('replica_object_missing')
        return self.decrypt(receipt,envelope)

    def _validate_receipt(self,receipt: ReplicaReceipt) -> None:
        if (not isinstance(receipt,ReplicaReceipt) or receipt.failure_domain!=self.failure_domain
                or receipt.object_ref!=self.object_ref(receipt.event_id,receipt.sha256)):
            raise ReplicaError('replica_receipt_invalid')

    def decrypt(self,receipt: ReplicaReceipt,envelope: bytes) -> Snapshot:
        self._validate_receipt(receipt)
        try:
            if (not isinstance(envelope,bytes) or len(envelope)>self.maxbytes+OVERHEAD
                    or not envelope.startswith(MAGIC) or len(envelope)<len(MAGIC)+4+12+16):
                raise ValueError('format')
            size=struct.unpack('>I',envelope[len(MAGIC):len(MAGIC)+4])[0]
            if not 1<=size<=MAX_HEADER: raise ValueError('header')
            offset=len(MAGIC)+4
            aad=envelope[offset:offset+size]
            header=json.loads(aad,object_pairs_hook=_pairs,parse_constant=_constant)
            if (not isinstance(header,dict) or set(header)!=HEADER_FIELDS
                    or type(header['version']) is not int or header['version']!=1
                    or header['algorithm']!='AES-256-GCM'
                    or header['event_id']!=receipt.event_id or header['plain_sha256']!=receipt.sha256
                    or header['key_ref']!=self.key_ref or header['failure_domain']!=self.failure_domain
                    or type(header['plain_size']) is not int or not 1<=header['plain_size']<=self.maxbytes
                    or _json(header)!=aad):
                raise ValueError('binding')
            offset+=size
            nonce=envelope[offset:offset+12]; ciphertext=envelope[offset+12:]
            if len(ciphertext)!=header['plain_size']+16: raise ValueError('length')
            content=self._cipher.decrypt(nonce,ciphertext,aad)
            if digest(content)!=receipt.sha256: raise ValueError('digest')
            return Snapshot(receipt.event_id,content,receipt.sha256)
        except Exception:
            raise ReplicaError('replica_envelope_invalid') from None


def _unescape(value: str) -> str:
    if re.search(r'\\(?!040|011|012|134)',value):
        raise ReplicaError('replica_mountinfo_invalid')
    return re.sub(r'\\(040|011|012|134)',lambda m:chr(int(m.group(1),8)),value)


class NetworkMountObjectStore:
    """POSIX immutable CAS on an exact administrator-configured network mount.

    No mkdir, mount, remount, chmod of existing data, local fallback, shell, or
    credentials. The private object root must already exist. A server/filesystem
    lacking hard-link CAS, file fsync or directory fsync is rejected.
    """
    def __init__(self,root: str | Path,*,expected_fstype: str,expected_source: str,
                 expected_mountpoint: str | Path,maxbytes: int = 256*1024*1024):
        self.root=Path(root)
        self.mountpoint=Path(expected_mountpoint)
        if (not self.root.is_absolute() or not self.mountpoint.is_absolute()
                or '..' in self.root.parts or '..' in self.mountpoint.parts
                or expected_fstype not in {'nfs','nfs4','cifs'}
                or not isinstance(expected_source,str) or not 1<=len(expected_source)<=2048
                or any(ord(c)<32 for c in expected_source)
                or type(maxbytes) is not int or not 1<=maxbytes<=512*1024*1024+OVERHEAD
                or not hasattr(os,'O_NOFOLLOW') or not hasattr(os,'O_DIRECTORY')):
            raise ReplicaError('replica_mount_config_invalid')
        try: self.root.relative_to(self.mountpoint)
        except ValueError: raise ReplicaError('replica_mount_config_invalid') from None
        self.fstype,self.source,self.maxbytes=expected_fstype,expected_source,maxbytes
        self._pinned=None
        with self._directory(): pass

    @staticmethod
    def _mountinfo() -> str:
        try: return Path('/proc/self/mountinfo').read_text()
        except OSError: raise ReplicaError('replica_mount_unavailable') from None

    def _identity(self) -> tuple:
        covering=[]
        try:
            text=self._mountinfo()
            if len(text)>2*1024*1024: raise ValueError('limit')
            for line in text.splitlines():
                fields=line.split(); separator=fields.index('-')
                if separator<6 or len(fields)!=separator+4: raise ValueError('line')
                mount=Path(_unescape(fields[4]))
                if not mount.is_absolute(): raise ValueError('path')
                try: self.root.relative_to(mount)
                except ValueError: continue
                major,minor=(int(x) for x in fields[2].split(':'))
                item=(int(fields[0]),int(fields[1]),major,minor,_unescape(fields[3]),
                      str(mount),fields[separator+1],_unescape(fields[separator+2]),fields[5],fields[separator+3])
                covering.append((len(mount.parts),item))
            if not covering: raise ValueError('missing')
            depth=max(item[0] for item in covering)
            matches=[item for length,item in covering if length==depth]
            if len(matches)!=1: raise ValueError('overmount')
            identity=matches[0]
            if (identity[4]!='/' or identity[5]!=str(self.mountpoint) or identity[6]!=self.fstype or identity[7]!=self.source
                    or 'rw' not in identity[8].split(',') or 'rw' not in identity[9].split(',')):
                raise ValueError('identity')
            return identity
        except (ValueError,IndexError,TypeError):
            raise ReplicaError('replica_mount_identity_mismatch') from None

    def _open_root(self) -> int:
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
        fd=os.open('/',flags)
        try:
            for part in self.root.parts[1:]:
                child=os.open(part,flags,dir_fd=fd)
                os.close(fd); fd=child
            info=os.fstat(fd)
            if info.st_mode&0o077: raise ReplicaError('replica_storage_permissions_invalid')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _verify_root(self,fd: int,identity: tuple,inode: tuple) -> None:
        current=os.fstat(fd)
        if ((current.st_dev,current.st_ino)!=inode or os.major(current.st_dev)!=identity[2]
                or os.minor(current.st_dev)!=identity[3] or self._identity()!=identity):
            raise ReplicaError('replica_mount_identity_mismatch')
        other=self._open_root()
        try:
            info=os.fstat(other)
            if (info.st_dev,info.st_ino)!=inode:
                raise ReplicaError('replica_mount_identity_mismatch')
        finally: os.close(other)

    @contextmanager
    def _directory(self) -> Iterator[int]:
        fd=None
        try:
            identity=self._identity(); fd=self._open_root(); info=os.fstat(fd)
            inode=(info.st_dev,info.st_ino)
            if self._pinned is not None and self._pinned!=(identity,inode):
                raise ReplicaError('replica_mount_identity_mismatch')
            self._verify_root(fd,identity,inode)
            yield fd
            self._verify_root(fd,identity,inode)
            if self._pinned is None: self._pinned=(identity,inode)
        except OSError:
            raise ReplicaError('replica_mount_unavailable') from None
        finally:
            if fd is not None: os.close(fd)

    @staticmethod
    def _sync(fd: int) -> None:
        try: os.fsync(fd)
        except OSError as error:
            if error.errno in {errno.EINVAL,errno.ENOTSUP,errno.EOPNOTSUPP}:
                raise ObjectStoreUnsupported('replica_store_semantics_unsupported') from None
            raise

    def _read(self,fd: int,ref: str) -> bytes | None:
        try:
            info=os.stat(ref,dir_fd=fd,follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode): raise ReplicaError('replica_object_invalid')
            handle=os.open(ref,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        except FileNotFoundError: return None
        try:
            before=os.fstat(handle)
            if (not stat.S_ISREG(before.st_mode) or before.st_mode&0o077
                    or not 1<=before.st_size<=self.maxbytes):
                raise ReplicaError('replica_object_invalid')
            data=bytearray()
            while len(data)<=self.maxbytes:
                block=os.read(handle,min(65536,self.maxbytes+1-len(data)))
                if not block: break
                data.extend(block)
            after=os.fstat(handle)
            stable=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
            if len(data)!=before.st_size or stable(before)!=stable(after):
                raise ReplicaError('replica_object_changed')
            # Reconciliation of a lost write response still requires supported
            # server flushes; a merely cached read cannot create a receipt.
            self._sync(handle); self._sync(fd)
            return bytes(data)
        finally: os.close(handle)

    def get(self,ref: str) -> bytes | None:
        if not isinstance(ref,str) or OBJECT.fullmatch(ref) is None:
            raise ReplicaError('replica_object_reference_invalid')
        with self._directory() as fd: return self._read(fd,ref)

    def put_if_absent(self,ref: str,content: bytes) -> bool:
        if (not isinstance(ref,str) or OBJECT.fullmatch(ref) is None
                or not isinstance(content,bytes) or not 1<=len(content)<=self.maxbytes):
            raise ReplicaError('replica_object_invalid')
        with self._directory() as fd:
            temporary='.staging-'+os.urandom(16).hex()
            handle=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            try:
                with os.fdopen(handle,'wb') as output:
                    output.write(content); output.flush(); self._sync(output.fileno())
                try:
                    os.link(temporary,ref,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
                    created=True
                except FileExistsError:
                    created=False
                except OSError as error:
                    if error.errno in {errno.EPERM,errno.EXDEV,errno.ENOTSUP,errno.EOPNOTSUPP}:
                        raise ObjectStoreUnsupported('replica_store_semantics_unsupported') from None
                    raise
            finally:
                os.unlink(temporary,dir_fd=fd)
                self._sync(fd)
            if created:
                actual=self._read(fd,ref)
                if actual is None or len(actual)!=len(content) or digest(actual)!=digest(content):
                    raise ReplicaError('replica_read_after_write_failed')
            return created
