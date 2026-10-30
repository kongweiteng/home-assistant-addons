"""Explicit, durable Runner presence; unknown writes never become new heartbeats."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time

from .store import DeliveryError

REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
MAX_RECEIPT = 60 * 1024
MAX_JOURNAL = 128 * 1024


def fail(code='work_presence_frozen'):
    raise DeliveryError(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def reference(value):
    if not isinstance(value, str) or not REF.fullmatch(value):
        fail('work_presence_input_invalid')
    return value


class PresenceMonitor:
    def __init__(self, client, journal_path, *, runner_ref, boot_ref, policy_revision,
                 policy_digest, qualification_digest, receipt_gate,
                 refresh_seconds=.5, io_timeout_seconds=.75):
        reference(runner_ref); reference(boot_ref)
        if (client.runner_ref != runner_ref or not callable(receipt_gate)
                or type(policy_revision) is not int or policy_revision < 1
                or not isinstance(policy_digest, str) or not DIGEST.fullmatch(policy_digest)
                or not isinstance(qualification_digest, str) or not DIGEST.fullmatch(qualification_digest)
                or type(refresh_seconds) not in {int, float} or not math.isfinite(refresh_seconds)
                or not 0 < refresh_seconds <= .5
                or type(io_timeout_seconds) not in {int, float} or not math.isfinite(io_timeout_seconds)
                or not 0 < client.timeout_seconds <= io_timeout_seconds <= .75):
            fail('work_presence_config_invalid')
        self.client, self.path, self.runner_ref, self.boot_ref = client, Path(journal_path), runner_ref, boot_ref
        self.policy_revision, self.policy_digest = policy_revision, policy_digest
        self.qualification_digest, self.receipt_gate = qualification_digest, receipt_gate
        self.refresh_seconds = refresh_seconds
        self._lock = threading.RLock(); self._view_lock = threading.Lock()
        self._stop = threading.Event(); self._thread = None; self._started = False; self._closed = False
        self._frozen = False; self._active = None; self._snapshot = None; self._snapshot_expires = 0
        self._fd = None
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or not self.path.parent.is_dir() or self.path.parent.stat().st_mode & 0o077:
            fail('work_presence_storage_invalid')
        lock = self.path.with_name(self.path.name + '.lock')
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            self._private(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            fail('work_presence_writer_busy')
        self._fd = fd
        self._state = {'version': 1, 'runner_ref': runner_ref, 'policy_digest': policy_digest,
            'boot_ref': boot_ref, 'epoch': 0, 'sequence': 0, 'phase': 'fresh',
            'active_task_ref': None, 'pending': None, 'receipt': None, 'receipt_ref': None,
            'stop_evidence': None}
        try:
            if self.path.exists() or self.path.is_symlink():
                old = self._read(self.path)
                if (not isinstance(old, dict) or set(old) != set(self._state)
                        or old['version'] != 1 or old['runner_ref'] != runner_ref or old['policy_digest'] != policy_digest
                        or old['phase'] not in {'session_pending', 'idle', 'busy', 'stopped', 'closed', 'frozen'}
                        or type(old['epoch']) is not int or old['epoch'] < 0
                        or type(old['sequence']) is not int or old['sequence'] < 0
                        or not isinstance(old['boot_ref'], str) or not REF.fullmatch(old['boot_ref'])
                        or (old['phase'] == 'closed' and (old['epoch'] < 1 or old['sequence'] < 1))):
                    fail('work_presence_journal_invalid')
                if old['phase'] == 'closed' and old['active_task_ref'] is None and old['pending'] is None:
                    self._archive('history', canonical(old))
                else:
                    self._state = old
                    self._active = old['active_task_ref']; self._frozen = True
        except BaseException:
            self._release()
            raise

    @staticmethod
    def _private(fd):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
            fail('work_presence_storage_invalid')

    def _read(self, path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            self._private(source.fileno())
            content = source.read(MAX_JOURNAL + 1)
        if len(content) > MAX_JOURNAL:
            fail('work_presence_journal_invalid')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    fail('work_presence_journal_invalid')
                result[key] = value
            return result
        try:
            return json.loads(content, object_pairs_hook=unique, parse_constant=lambda _: fail('work_presence_journal_invalid'))
        except (ValueError, UnicodeDecodeError):
            fail('work_presence_journal_invalid')

    def _directory_sync(self, folder):
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _persist(self):
        if self._fd is None:
            fail("work_presence_writer_closed")
        content = canonical(self._state)
        if len(content) > MAX_JOURNAL:
            fail('work_presence_journal_invalid')
        temporary = self.path.with_name(self.path.name + '.tmp-' + os.urandom(8).hex())
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as target:
                target.write(content); target.flush(); os.fsync(target.fileno())
            if self.path.is_symlink():
                fail('work_presence_storage_invalid')
            os.replace(temporary, self.path)
            self._directory_sync(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _archive(self, kind, content):
        folder = self.path.with_name(self.path.name + '.' + kind)
        if folder.is_symlink():
            fail('work_presence_storage_invalid')
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.stat().st_mode & 0o077:
            fail('work_presence_storage_invalid')
        digest = hashlib.sha256(content).hexdigest(); target = folder / (digest + '.json')
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as source:
                self._private(source.fileno())
                if source.read(MAX_JOURNAL + 1) != content:
                    fail('work_presence_archive_conflict')
        else:
            with os.fdopen(fd, 'wb') as source:
                source.write(content); source.flush(); os.fsync(source.fileno())
        self._directory_sync(folder); self._directory_sync(folder.parent)
        return 'sha256:' + digest

    def _freeze(self):
        self._frozen = True; self._state['phase'] = 'frozen'
        with self._view_lock:
            self._snapshot = None; self._snapshot_expires = 0
        try:
            self._persist()
        except Exception:
            # The previous write-ahead state is already unclean. Never send.
            pass

    @property
    def frozen(self):
        return self._frozen

    @property
    def qualified(self):
        if self._frozen or self._closed or not self._started or self._active is not None:
            return False
        try:
            view = self.identity()
            return view.get('enabled') is True and view.get('accept_new') is True and view.get('state') == 'idle'
        except DeliveryError:
            return False

    def identity(self):
        if self._frozen or self._closed:
            fail()
        with self._view_lock:
            remaining = self._snapshot_expires - time.monotonic()
            if self._snapshot is None or remaining <= 0:
                fail('work_runner_identity_unavailable')
            result = copy.deepcopy(self._snapshot)
            result['valid_for_ms'] = max(0, min(result['valid_for_ms'], int(remaining * 1000)))
        return result

    def _refresh_identity(self):
        began = time.monotonic()
        try:
            value = self.client.identity()
            if (value.get('runner_ref') != self.runner_ref or value.get('session_epoch') != self._state['epoch']
                    or value.get('policy_digest') != self.policy_digest or value.get('runner_kind') != 'owner_work_v1'
                    or type(value.get('valid_for_ms')) is not int or not 0 <= value['valid_for_ms'] <= 3000):
                fail()
            expires = began + value['valid_for_ms'] / 1000
        except Exception:
            value = None; expires = 0
        with self._view_lock:
            self._snapshot, self._snapshot_expires = copy.deepcopy(value), expires

    def _heartbeat(self, state, task=None, evidence=None):
        if self._frozen:
            fail()
        value = {'session_epoch': self._state['epoch'], 'sequence': self._state['sequence'] + 1,
            'policy_revision': self.policy_revision, 'policy_digest': self.policy_digest,
            'qualification_digest': self.qualification_digest, 'state': state,
            'active_task_ref': task, 'self_check_ok': True, 'stop_evidence': evidence}
        self._state['pending'] = {'kind': 'heartbeat', 'payload': value}
        self._persist()
        result = self.client.heartbeat(value)
        presence = result.get('presence') if isinstance(result, dict) else None
        if (result.get('runner_id') != self.runner_ref or result.get('runner_kind') != 'owner_work_v1'
                or result.get('session_epoch') != self._state['epoch'] or not isinstance(presence, dict)
                or any(presence.get(key) != item for key, item in value.items())
                or result.get('recovery_required') is not False):
            fail()
        self._state['sequence'] = value['sequence']; self._state['pending'] = None
        self._persist()
        self._refresh_identity()
        return result

    def start(self, recovery_mode=False):
        with self._lock:
            if self._closed or type(recovery_mode) is not bool:
                fail()
            if self._frozen:
                pending = self._state.get('pending') or {}
                if recovery_mode and self._state['epoch'] > 0 and pending.get('kind') != 'session':
                    self._started = True
                    return self._state['epoch']  # Known local epoch, never execution permission.
                fail()
            if self._started:
                if recovery_mode:
                    self._freeze()
                return self._state['epoch']
            payload = {'boot_ref': self.boot_ref, 'policy_revision': self.policy_revision,
                       'policy_digest': self.policy_digest, 'recovery_mode': recovery_mode}
            try:
                self._state['phase'] = 'session_pending'; self._state['pending'] = {'kind': 'session', 'payload': payload}
                self._persist()
                response = self.client.session(payload)
                epoch = response.get('session_epoch')
                if response.get('runner_id') != self.runner_ref or type(epoch) is not int or epoch < 1:
                    fail()
                self._state.update(epoch=epoch, pending=None, phase='idle')
                self._persist(); self._started = True
                if recovery_mode:
                    self._freeze()
                    return epoch
                self._heartbeat('idle')
            except Exception:
                self._freeze(); fail()
            def run():
                while not self._stop.wait(self.refresh_seconds):
                    with self._lock:
                        if self._frozen or self._closed:
                            return
                        try:
                            state = 'busy' if self._active is not None else 'stopped' if self._state['phase'] == 'stopped' else 'idle'
                            self._heartbeat(state, self._active, self._state['stop_evidence'] if state == 'stopped' else None)
                        except Exception:
                            self._freeze(); return
            self._thread = threading.Thread(target=run, daemon=True, name='work-runner-presence')
            try:
                self._thread.start()
            except Exception:
                self._freeze(); fail()
            return epoch

    def begin(self, task_ref):
        reference(task_ref)
        with self._lock:
            if not self.qualified or self._active is not None:
                fail()
            try:
                self._active = task_ref
                self._state.update(active_task_ref=task_ref, phase='busy', receipt=None, receipt_ref=None, stop_evidence=None)
                self._heartbeat('busy', task_ref)
                view = self.identity()
                if view.get('enabled') is not True or view.get('state') != 'busy' or view.get('active_task_ref') != task_ref:
                    fail()
                return {'session_epoch': self._state['epoch'], 'sequence': self._state['sequence'], 'task_ref': task_ref}
            except Exception:
                self._freeze(); fail()

    def settled(self, task_ref, receipt):
        reference(task_ref)
        with self._lock:
            try:
                if not isinstance(receipt, dict):
                    fail('work_presence_receipt_invalid')
                raw = canonical(receipt)
                if len(raw) > MAX_RECEIPT:
                    fail('work_presence_receipt_invalid')
                retained = json.loads(raw)
                receipt_ref = self._archive('receipts', canonical({'task_ref': task_ref, 'receipt': retained}))
                self._state.update(receipt=retained, receipt_ref=receipt_ref)
                self._persist()
                if not self._started or self._active != task_ref:
                    fail()
                result = self.receipt_gate(task_ref, copy.deepcopy(retained))
                if not isinstance(result, dict) or set(result) != {'stop_ref', 'checkpoint_ref'}:
                    fail('work_presence_receipt_invalid')
                reference(result['stop_ref']); reference(result['checkpoint_ref'])
                evidence = {'session_epoch': self._state['epoch'], 'task_ref': task_ref, **result}
                self._state['stop_evidence'] = evidence; self._persist()
                if self._frozen:
                    fail()
                self._heartbeat('stopped', evidence=evidence)
                self._active = None
                self._state.update(active_task_ref=None, phase='stopped')
                self._persist()
                return {'receipt_ref': receipt_ref, **result}
            except Exception:
                self._freeze(); fail()

    def uncertain(self, task_ref):
        """Latch an unknown runtime outcome locally; never issue a remote write."""
        reference(task_ref)
        with self._lock:
            if self._active is None:
                self._active = task_ref
                self._state['active_task_ref'] = task_ref
            self._freeze()

    def _release(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
        with self._lock:
            if self._closed:
                return
            try:
                if self._started and not self._frozen and self._active is None and self._state['pending'] is None:
                    self._heartbeat('stopped')
                    self._state['phase'] = 'closed'; self._persist()
                elif self._started or self._frozen:
                    self._freeze()
            except Exception:
                self._freeze()
            finally:
                self._closed = True
                with self._view_lock:
                    self._snapshot = None; self._snapshot_expires = 0
                self._release()
