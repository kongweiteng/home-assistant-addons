"""Role-separated read projections of canonical tasks and trusted observations.

There is no build, deployment, credential discovery or policy selected by model
arguments here. The boot layer must supply independent target/source observers;
absence means unavailable. A thread that exceeds its deadline retains its slot
until the trusted Port actually returns, so repeated reads cannot pile up work.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
import hmac
import json
import re
import threading
import time

from .store import DeliveryError, SHANGHAI, canonical, reference

PREFIX = '/internal/v2/release-authority/'
SCOPE_FIELDS = frozenset({'owner_ref', 'task_ref', 'task_revision', 'project_ref', 'state', 'mode', 'control_pending'})
RELEASE_FIELDS = frozenset({'schema_version', 'release_ref', 'version', 'source_digest',
    'artifact_identity', 'permissions_digest', 'qualification_digest'})
TARGET_FIELDS = frozenset({'target_ref', 'instance_ref', 'slug', 'project_ref', 'initial_release_ref',
    'baseline_etag', 'config_digest', 'synthetic_data_digest', 'manager_principal_ref',
    'backup_principal_ref', 'backup_store_ref', 'backup_store_domain', 'canary_no_business_data'})
MANAGER_FIELDS = TARGET_FIELDS | {'qualification_expires_at'}
BACKUP_FIELDS = frozenset({'target_ref', 'instance_ref', 'slug', 'backup_store_ref', 'backup_store_domain',
    'key_ref', 'baseline_etag', 'config_digest', 'synthetic_data_digest', 'metadata_digest',
    'auxiliary_digests', 'canary_no_business_data', 'supervisor_mount_count',
    'supervisor_registry_count', 'qualification_expires_at'})
REF = r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}'
SHA = re.compile(r'sha256:[a-f0-9]{64}\Z')


class AuthorityError(DeliveryError):
    def __init__(self, code='release_authority_unavailable', status=503):
        super().__init__(code)
        self.status = status


def _fail(code='release_authority_unavailable', status=503):
    raise AuthorityError(code, status) from None


def _closed(value, fields):
    if type(value) is not dict or set(value) != fields:
        _fail()
    raw = canonical(value).encode()
    if len(raw) > 32768:
        _fail()
    # Return owned primitive data, never a mutable observer dictionary.
    return json.loads(raw)


@dataclass(frozen=True, repr=False)
class AuthorityPrincipal:
    role: str
    token: str = field(repr=False)
    owner_refs: frozenset[str]
    project_refs: frozenset[str]
    target_refs: frozenset[str]
    release_refs: frozenset[str]

    def __post_init__(self):
        if (self.role not in {'broker', 'manager', 'backup'} or type(self.token) is not str
                or not 32 <= len(self.token) <= 4096
                or any(not 33 <= ord(char) <= 126 for char in self.token)):
            _fail('release_authority_registration_invalid')
        for refs in (self.owner_refs, self.project_refs, self.target_refs, self.release_refs):
            if type(refs) is not frozenset or not 1 <= len(refs) <= 128:
                _fail('release_authority_registration_invalid')
            for value in refs:
                reference(value)


class CanonicalReleaseAuthority:
    def __init__(self, store, *, principals, target_reader=None, release_reader=None,
                 source_qualifier=None, reserved_secrets=(), timeout_seconds=5,
                 monotonic=time.monotonic):
        if (type(timeout_seconds) not in {float, int} or not .05 <= timeout_seconds <= 5
                or not isinstance(principals, (tuple, list)) or not 1 <= len(principals) <= 3
                or any(type(p) is not AuthorityPrincipal for p in principals)
                or len({p.role for p in principals}) != len(principals)):
            _fail('release_authority_registration_invalid')
        tokens = []
        for principal in principals:
            if any(hmac.compare_digest(principal.token.encode(), token.encode())
                   for token in (*tokens, *reserved_secrets)):
                _fail('release_authority_registration_invalid')
            tokens.append(principal.token)
        self.store, self.principals = store, tuple(principals)
        self.target_reader, self.release_reader, self.source_qualifier = target_reader, release_reader, source_qualifier
        self.monotonic, self.timeout_seconds = monotonic, timeout_seconds
        self._slots = threading.BoundedSemaphore(2)

    def authenticate(self, headers):
        if len(headers) != 1 or type(headers[0]) is not str:
            _fail('release_authority_not_authorized', 401)
        matches = [p for p in self.principals if hmac.compare_digest(
            headers[0].encode(), ('Bearer '+p.token).encode())]
        if len(matches) != 1:
            _fail('release_authority_not_authorized', 401)
        return matches[0]

    def _scope(self, principal, task_ref):
        if self.store.recovery_mode:
            _fail()
        with closing(self.store._connect()) as connection:
            connection.execute('BEGIN')
            row = connection.execute('SELECT owner_ref,task_id,revision,project_id,state,mode,control_pending '
                'FROM tasks WHERE task_id=?', (task_ref,)).fetchone()
            if (row is None or row['owner_ref'] not in principal.owner_refs
                    or row['project_id'] not in principal.project_refs):
                _fail('release_authority_not_found', 404)
            guard = connection.execute('SELECT recovery_required FROM runtime_guard WHERE singleton=1').fetchone()
            pending = connection.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone()
            project = connection.execute('SELECT owner_ref,state FROM projects WHERE project_id=?',
                                         (row['project_id'],)).fetchone()
            if (guard is None or guard[0] or pending or project is None
                    or project['owner_ref'] != row['owner_ref'] or project['state'] != 'active'):
                _fail()
            result = dict(owner_ref=row['owner_ref'], task_ref=row['task_id'], task_revision=row['revision'],
                project_ref=row['project_id'], state=row['state'], mode=row['mode'], control_pending=row['control_pending'])
            connection.execute('COMMIT')
        return _closed(result, SCOPE_FIELDS)

    def _release(self, principal, release_ref, deadline):
        if release_ref not in principal.release_refs:
            _fail('release_authority_not_found', 404)
        if self.release_reader is None:
            _fail()
        result = _closed(self.release_reader(release_ref, deadline=deadline), RELEASE_FIELDS)
        self._validate_release(result, release_ref)
        return result

    @staticmethod
    def _validate_release(result, release_ref):
        if (type(result['schema_version']) is not int or result['schema_version'] != 2
                or result['release_ref'] != release_ref or type(result['version']) is not str
                or re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,63}', result['version']) is None):
            _fail()
        for key in ('source_digest', 'permissions_digest', 'qualification_digest'):
            if type(result[key]) is not str or SHA.fullmatch(result[key]) is None:
                _fail()
        identity = result['artifact_identity']
        if type(identity) is not dict:
            _fail()
        expected = {'kind', 'digest'} if identity.get('kind') == 'docker_image_id' else {'kind', 'repository', 'digest'}
        if (set(identity) != expected or identity.get('kind') not in {'docker_image_id', 'oci_repo_digest'}
                or type(identity.get('digest')) is not str or SHA.fullmatch(identity['digest']) is None):
            _fail()
        if identity['kind'] == 'oci_repo_digest' and (type(identity['repository']) is not str
                or re.fullmatch(r'[a-z0-9][a-z0-9.:-]*(?:/[a-z0-9][a-z0-9._-]*)+', identity['repository']) is None):
            _fail()

    def _target(self, principal, target_ref, role, deadline):
        if role != principal.role:
            _fail('release_authority_role_denied', 403)
        if target_ref not in principal.target_refs:
            _fail('release_authority_not_found', 404)
        if self.target_reader is None:
            _fail()
        fields = {'broker': TARGET_FIELDS, 'manager': MANAGER_FIELDS, 'backup': BACKUP_FIELDS}[role]
        result = _closed(self.target_reader(target_ref, role, deadline=deadline), fields)
        if (result['target_ref'] != target_ref or result['canary_no_business_data'] is not True
                or type(result['slug']) is not str or re.fullmatch(r'[a-z0-9][a-z0-9_]{0,63}', result['slug']) is None):
            _fail()
        for key in ('target_ref', 'instance_ref', 'backup_store_ref', 'backup_store_domain'):
            reference(result[key])
        for key in ('baseline_etag', 'config_digest', 'synthetic_data_digest'):
            if type(result[key]) is not str or SHA.fullmatch(result[key]) is None:
                _fail()
        if role != 'backup' and result['project_ref'] not in principal.project_refs:
            _fail('release_authority_not_found', 404)
        if role != 'broker':
            try:
                expiry = datetime.fromisoformat(result['qualification_expires_at'])
                if expiry.utcoffset() != datetime.now(SHANGHAI).utcoffset() or expiry <= datetime.now(SHANGHAI):
                    _fail()
            except Exception:
                _fail()
        if role == 'backup':
            if (any(type(result[key]) is not int or result[key] < 0
                    for key in ('supervisor_mount_count', 'supervisor_registry_count'))
                    or type(result['auxiliary_digests']) is not dict
                    or set(result['auxiliary_digests']) - {'image.tar', 'apparmor.txt'}):
                _fail()
            for value in (result['metadata_digest'], *result['auxiliary_digests'].values()):
                if type(value) is not str or SHA.fullmatch(value) is None:
                    _fail()
            reference(result['key_ref'])
        return result

    def dispatch(self, method, path, headers, payload=None, *, deadline):
        principal = self.authenticate(headers)
        limit = min(deadline, self.monotonic()+self.timeout_seconds)
        if self.monotonic() >= limit:
            _fail()
        # Parse routes and roles before occupying a trusted observation slot.
        if method == 'GET' and payload is None:
            scope = re.fullmatch(re.escape(PREFIX)+r'scopes/('+REF+r')', path)
            target = re.fullmatch(re.escape(PREFIX)+r'targets/('+REF+r')/(broker|manager|backup)', path)
            release = re.fullmatch(re.escape(PREFIX)+r'releases/('+REF+r')', path)
            if scope:
                action = lambda: self._scope(principal, scope[1])
            elif target:
                action = lambda: self._target(principal, target[1], target[2], limit)
            elif release:
                action = lambda: self._release(principal, release[1], limit)
            else:
                _fail('release_authority_not_found', 404)
        elif method == 'POST' and path == PREFIX+'source-qualification':
            if (type(payload) is not dict or set(payload) != {'version', 'target_ref', 'release_ref'}
                    or type(payload['version']) is not int or payload['version'] != 2):
                _fail('release_authority_request_invalid', 400)
            if principal.role != 'manager':
                _fail('release_authority_role_denied', 403)
            target_ref, release_ref = payload['target_ref'], payload['release_ref']
            if any(type(value) is not str or re.fullmatch(REF, value) is None for value in (target_ref, release_ref)):
                _fail('release_authority_request_invalid', 400)
            if target_ref not in principal.target_refs or release_ref not in principal.release_refs:
                _fail('release_authority_not_found', 404)
            if self.source_qualifier is None:
                _fail()
            def action():
                actual = _closed(self.source_qualifier(target_ref, release_ref, deadline=limit), RELEASE_FIELDS)
                self._validate_release(actual, release_ref)
                registered = self._release(principal, release_ref, limit)
                if actual != registered:
                    _fail()
                return actual
        else:
            _fail('release_authority_not_found', 404)
        if not self._slots.acquire(blocking=False):
            _fail('release_authority_busy')
        completed, outcome = threading.Event(), []
        def observe():
            try:
                outcome.append((True, action()))
            except AuthorityError as error:
                outcome.append((False, error))
            except Exception:
                outcome.append((False, AuthorityError()))
            finally:
                self._slots.release()
                completed.set()
        try:
            threading.Thread(target=observe, name='release-authority-read', daemon=True).start()
        except Exception:
            self._slots.release()
            _fail()
        if not completed.wait(max(0, limit-self.monotonic())) or self.monotonic() >= limit:
            _fail()
        success, result = outcome[0]
        if not success:
            raise result from None
        return result
