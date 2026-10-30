"""Fixed, default-disabled repository boot. JSON selects data, never code.

Only load() establishes root provenance. The resulting ports remain revocable:
policy bytes, fixed Git, canonical task scope and real read-only mounts are
checked again when used. Parsing fixtures alone is not runtime qualification.
"""
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
import hmac
import os
from pathlib import Path
import re
import stat
import threading

from .durability import digest
from .registry_build_composition import _read_root
from .repo_manager import AuthorizationReceipt, RepositoryPolicy, RepositoryWorkspace, _sha
from .repository_composition import RepositoryExecutionFactory, RepositoryTestBinding
from .repository_inputs import SourceExposureReceipt
from .repository_validation import SCANNER_DIGEST, SourceScanner
from .store import DeliveryError, SHANGHAI, canonical, reference
from .target_observation_client import ManagerTargetObservationClient, decode, origin_binding

REGISTRATION_PATH = Path('/etc/codex-delivery/repository/registration.json')
POLICY_PATH = Path('/opt/codex-delivery/repository-policy.json')
GIT_PATH = Path('/usr/bin/git')
WORKSPACE_ROOT = Path('/var/lib/codex-delivery/repository-workspaces')
LIMIT = 131072
READ_ACTIONS = frozenset({'sync', 'export', 'archive', 'export_execution_input'})
WRITE_ACTIONS = frozenset({'bind_code_task', 'assemble_candidate', 'stage', 'import', 'prepare_publish', 'publish', 'reconcile_publish', 'observe_publication'})
MODES = frozenset({'work', 'develop', 'deliver', 'deploy'})


def fail():
    raise DeliveryError('repository_registration_invalid') from None


def _closed(value, fields):
    if type(value) is not dict or set(value) != fields: fail()
    return value


def _integer(value, minimum=1):
    if type(value) is not int or not minimum <= value <= 2**63-1: fail()
    return value


def _expiry(value):
    if type(value) is not str: fail()
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != datetime.now(SHANGHAI).utcoffset() or parsed <= datetime.now(SHANGHAI): fail()
    return parsed


def _sequence(value, maximum=32):
    if type(value) is not list or not 1 <= len(value) <= maximum: fail()
    return value


def _workspace_path(value):
    if type(value) is not str: fail()
    path = Path(value)
    if (str(path) != value or '..' in path.parts or '.' in path.parts
            or not path.is_relative_to(WORKSPACE_ROOT) or path == WORKSPACE_ROOT): fail()
    return path


def _directory(path):
    """Open each ancestor without following aliases; caller owns the final fd."""
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor); fail()


def _mount_path(value):
    # mountinfo escapes only these four characters. Reject unexpected escapes.
    if re.search(r'\\(?!040|011|012|134)', value): fail()
    return re.sub(r'\\(040|011|012|134)', lambda match: chr(int(match[1], 8)), value)


def _readonly(path, identity, uid):
    """Linux kernel facts, including writable nested mounts; never chmod/flock."""
    descriptor = None
    try:
        descriptor = _directory(path); info = os.fstat(descriptor)
        if ((info.st_dev, info.st_ino) != identity or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) != 0o700
                or not os.fstatvfs(descriptor).f_flag & os.ST_RDONLY): fail()
        with open('/proc/self/mountinfo', 'rb') as stream:
            raw = stream.read(1024*1024+1)
        if not raw or len(raw) > 1024*1024: fail()
        mounts = []
        for line in raw.decode('utf-8').splitlines():
            left, right = line.split(' - ', 1); fields = left.split(); extra = right.split()
            if len(fields) < 6 or len(extra) < 3: fail()
            mount = Path(_mount_path(fields[4]))
            if not mount.is_absolute(): fail()
            mounts.append((mount, fields[5].split(','), extra[2].split(',')))
        covering = [item for item in mounts if path.is_relative_to(item[0])]
        if not covering: fail()
        deepest = max(len(item[0].parts) for item in covering)
        # Duplicate mountpoints/overmount ambiguity is deliberately unsupported.
        selected = [item for item in covering if len(item[0].parts) == deepest]
        if len(selected) != 1 or 'ro' not in selected[0][1] or 'ro' not in selected[0][2]: fail()
        for mount, options, super_options in mounts:
            if mount.is_relative_to(path) and ('ro' not in options or 'ro' not in super_options): fail()
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_uid, after.st_mode) != (info.st_dev, info.st_ino, info.st_uid, info.st_mode): fail()
        return True
    except Exception: fail()
    finally:
        if descriptor is not None: os.close(descriptor)


def _policy(raw):
    if type(raw) is not bytes or not 1 <= len(raw) <= LIMIT: fail()
    policy = decode(raw)
    if type(policy) is not dict or type(policy.get('version')) is not int or policy['version'] not in {1,2}:fail()
    managed=policy['version']==2
    _closed(policy, {'version', 'service_uid', 'expires_at', 'git_sha256', 'scanner_digest',
        'runtime_qualification_digest', 'repositories', 'grants', 'test_bindings', 'target', 'forbidden_token_sha256'}
        | ({'source_kind'} if managed else set()))
    if managed and policy['source_kind']!='managed-sealed-v2':fail()
    _integer(policy['service_uid']); _expiry(policy['expires_at'])
    for name in ('git_sha256', 'runtime_qualification_digest'): _sha(policy[name])
    if policy['scanner_digest'] != SCANNER_DIGEST: fail()
    repositories = []
    for item in _sequence(policy['repositories']):
        _closed(item, set(RepositoryPolicy.__dataclass_fields__))
        targets = tuple(tuple(pair) for pair in _sequence(item['publish_targets'], 16)
                        if type(pair) is list and len(pair) == 2)
        if len(targets) != len(item['publish_targets']): fail()
        repositories.append(RepositoryPolicy(**dict(item, publish_targets=targets)))
    if len({item.remote_identity for item in repositories}) != len(repositories): fail()
    by_digest = {item.sha256: item for item in repositories}
    scopes = set(); workspace_refs = set(); roots = set(); repo_owners = {}
    for grant in _sequence(policy['grants']):
        _closed(grant, {'grant_ref', 'owner_ref', 'project_ref', 'task_ref', 'task_revision', 'mode', 'repositories',
                       'code_task_ref', 'code_revision'})
        for key in ('grant_ref', 'owner_ref', 'project_ref', 'task_ref'): reference(grant[key])
        _integer(grant['task_revision'])
        if type(grant['mode']) is not str or grant['mode'] not in MODES: fail()
        if grant['code_task_ref'] is None:
            if grant['code_revision'] is not None: fail()
        else:
            reference(grant['code_task_ref']); _integer(grant['code_revision'])
            if grant['mode'] == 'work': fail()
        key = (grant['owner_ref'], grant['task_ref'])
        if key in scopes: fail()
        scopes.add(key); repos = set()
        for item in _sequence(grant['repositories']):
            _closed(item, {'repo_ref', 'policy_digest', 'access', 'workspace_ref'}
                    | (set() if managed else {'root','device','inode'}))
            for name in ('repo_ref', 'workspace_ref'): reference(item[name])
            if item['policy_digest'] not in by_digest or item['access'] not in {'read', 'write'}: fail()
            if item['repo_ref'] in repos:fail()
            if not managed:
                _integer(item['device'], 0); _integer(item['inode']); root = _workspace_path(item['root'])
                if item['workspace_ref'] in workspace_refs or root in roots: fail()
                workspace_refs.add(item['workspace_ref']); roots.add(root)
            repos.add(item['repo_ref'])
            owner_key = (grant['owner_ref'], item['repo_ref'])
            old = repo_owners.setdefault(owner_key, item['policy_digest'])
            if old != item['policy_digest']: fail()
    for grant in policy['grants']:
        if grant['code_task_ref'] is None: continue
        paired = [other for other in policy['grants'] if other['owner_ref'] == grant['owner_ref']
                  and other['task_ref'] == grant['code_task_ref'] and other['task_revision'] == grant['code_revision']]
        inventory = lambda value: {(item['repo_ref'], item['policy_digest'], item['access']) for item in value['repositories']}
        if (len(paired) != 1 or paired[0]['mode'] != 'work' or paired[0]['project_ref'] != grant['project_ref']
                or inventory(grant) != inventory(paired[0]) or any(item['access'] != 'write' for item in grant['repositories'])): fail()
    bindings = []
    for item in _sequence(policy['test_bindings']):
        _closed(item, set(RepositoryTestBinding.__dataclass_fields__))
        binding = RepositoryTestBinding(**item)
        if repo_owners.get((binding.owner_ref, binding.repo_ref)) != binding.repository_policy_digest: fail()
        bindings.append(binding)
    if len({(item.owner_ref, item.repo_ref) for item in bindings}) != len(bindings): fail()
    required = {(grant['owner_ref'], item['repo_ref']) for grant in policy['grants']
                for item in grant['repositories'] if item['access'] == 'write' and grant['mode'] != 'work'}
    if required != {(item.owner_ref, item.repo_ref) for item in bindings}: fail()
    target = _closed(policy['target'], {'origin', 'target_ref', 'project_ref', 'permissions_digest', 'observation'})
    origin_binding(target['origin'])
    for name in ('target_ref', 'project_ref'): reference(target[name])
    _sha(target['permissions_digest'])
    from .release_authority import TARGET_FIELDS
    observation = _closed(target['observation'], set(TARGET_FIELDS))
    if (observation['target_ref'] != target['target_ref'] or observation['project_ref'] != target['project_ref']
            or observation['canary_no_business_data'] is not True
            or any(grant['project_ref'] != target['project_ref'] for grant in policy['grants'])): fail()
    for name in ('instance_ref', 'initial_release_ref', 'manager_principal_ref', 'backup_principal_ref', 'backup_store_ref', 'backup_store_domain'):
        reference(observation[name])
    if type(observation['slug']) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9_]{0,63}', observation['slug']): fail()
    for name in ('baseline_etag', 'config_digest', 'synthetic_data_digest'): _sha(observation[name])
    fingerprints = policy['forbidden_token_sha256']
    if type(fingerprints) is not list or len(fingerprints) > 128 or len(set(fingerprints)) != len(fingerprints): fail()
    for value in fingerprints: _sha(value)
    SourceScanner()  # Fixed positive/negative self-checks; no source execution.
    return policy, tuple(repositories), tuple(bindings)


@dataclass(frozen=True, repr=False)
class RepositoryBootPlan:
    service_uid: int
    _raw: bytes = field(repr=False)
    _token: str = field(repr=False)
    _lock: object = field(default_factory=threading.Lock, repr=False)
    _attempted: bool = field(default=False, repr=False)
    _factory: object = field(default=None, repr=False)

    @classmethod
    def load(cls, *, forbidden_credentials=()):
        try:
            try: REGISTRATION_PATH.lstat()
            except FileNotFoundError: return None
            raw = _read_root(REGISTRATION_PATH, private_leaf=True, optional=True)
            if raw is None: return None
            registration = decode(raw)
            if (registration == {'version': 1, 'enabled': False}
                    and type(registration['version']) is int and registration['enabled'] is False): return None
            plan = cls.parse(raw, _read_root(POLICY_PATH, limit=LIMIT), forbidden_credentials=forbidden_credentials)
            plan.verify()
            return plan
        except Exception: fail()

    @classmethod
    def parse(cls, raw, policy_raw, *, forbidden_credentials=()):
        """Pure closed schema validation. create() re-establishes file provenance."""
        try:
            if type(raw) is not bytes or not 1 <= len(raw) <= LIMIT: fail()
            registration = _closed(decode(raw), {'version', 'enabled', 'policy_sha256', 'target_observation_token'})
            policy, _, _ = _policy(policy_raw)
            if (type(registration['version']) is not int or registration['version'] != 1
                    or registration['enabled'] is not True or registration['policy_sha256'] != digest(policy_raw)): fail()
            token = registration['target_observation_token']
            if (type(token) is not str or not 32 <= len(token) <= 256 or any(not 33 <= ord(c) <= 126 for c in token)
                    or type(forbidden_credentials) not in {tuple, list}
                    or any(type(value) not in {str, bytes} for value in forbidden_credentials)): fail()
            encoded = token.encode('ascii')
            if (digest(encoded) in policy['forbidden_token_sha256'] or any(hmac.compare_digest(encoded,
                    value.encode() if type(value) is str else value) for value in forbidden_credentials)): fail()
            return cls(policy['service_uid'], policy_raw, token)
        except Exception: fail()

    def verify(self):
        try:
            if _read_root(POLICY_PATH, limit=LIMIT) != self._raw: fail()
            policy, _, _ = _policy(self._raw)
            if digest(_read_root(GIT_PATH, limit=64*1024*1024)) != policy['git_sha256']: fail()
        except Exception: fail()

    def create(self, *, build_executor, build_observer,git_runtime=None):
        with self._lock:
            if self._attempted: fail()
            object.__setattr__(self, '_attempted', True)
            try:
                self.verify()
                if os.getuid() != self.service_uid or os.geteuid() != self.service_uid: fail()
                policy, repositories, _ = _policy(self._raw)
                gates = _RepositoryGates(self, policy, repositories)
                managed=policy['version']==2
                if managed:
                    from .sealed_repository_sources import qualify_sealing
                    qualify_sealing()
                else:
                    # v1 checks every fixed directory; v2 has no directory gate.
                    for grant in policy['grants']:
                        for item in grant['repositories']: gates.readonly(item)
                factory = _BootFactory(gates, policies=repositories, git_binary=GIT_PATH,
                    authorization_gate=gates.authorize, workspace_bindings=None if managed else gates.workspaces,
                    quiescence_gate=None if managed else gates.quiescent, input_gate=gates.inputs,
                    source_kind='managed-sealed-v2' if managed else 'directory-v1',git_runtime=git_runtime,
                    build_executor=build_executor, build_observer=build_observer, target_observer=gates.target)
                object.__setattr__(self, '_factory', factory)
                return factory
            except Exception: fail()

    def attach_validation(self, factory, execution, coordinator):
        if self._factory is None or factory is not self._factory: fail()
        _, _, bindings = _policy(self._raw)
        return factory.attach_validation(execution, coordinator, policy=self, bindings=bindings,
            scanner_digest=SCANNER_DIGEST, runtime_qualification_digest=decode(self._raw)['runtime_qualification_digest'])


class _BootFactory(RepositoryExecutionFactory):
    def __init__(self, gates, **kwargs):
        super().__init__(**kwargs); self._gates = gates

    def __call__(self, store, archive, state_root):
        if self._gates.store is not None: fail()
        self._gates.store = store
        self._gates.plan.verify()
        return super().__call__(store, archive, state_root)


class _RepositoryGates:
    def __init__(self, plan, policy, repositories):
        self.plan, self.policy, self.store = plan, policy, None
        self.repositories = {item.sha256: item for item in repositories}
        target = policy['target']; targets = frozenset({target['target_ref']})
        self.observer = ManagerTargetObservationClient(target['origin'], plan._token,
            target_allowlists={role: targets for role in ('broker', 'manager', 'backup')})

    def readonly(self, item):
        return _readonly(Path(item['root']), (item['device'], item['inode']), self.plan.service_uid)

    def current(self, request, *, sync=False):
        self.plan.verify()
        store = self.store
        if store is None or store.recovery_mode: fail()
        store.verify_event_chain()
        matches = []
        with closing(store._connect()) as connection:
            connection.execute('BEGIN')
            if connection.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone(): fail()
            for grant in self.policy['grants']:
                if grant['owner_ref'] != request.get('owner_ref'): continue
                if not sync and (grant['task_ref'] != request.get('task_id')
                        or grant['task_revision'] != request.get('task_revision')): continue
                task = store._owned(connection, 'tasks', 'task_id', grant['owner_ref'], grant['task_ref'])
                project = store._project(connection, grant['owner_ref'], grant['project_ref'])
                if (task['revision'] != grant['task_revision'] or task['project_id'] != grant['project_ref']
                        or task['mode'] != grant['mode'] or task['state'] not in {'queued', 'running', 'completed'}
                        or task['control_pending'] or project['state'] != 'active'): continue
                expected = sorted((item['repo_ref'], item['access'], '[]') for item in grant['repositories'])
                actual = [tuple(row) for row in connection.execute('SELECT repo_id,access,paths_json FROM task_repositories '
                    'WHERE owner_ref=? AND task_id=? ORDER BY repo_id', (grant['owner_ref'], grant['task_ref']))]
                if actual != expected: continue
                for item in grant['repositories']:
                    if item['repo_ref'] != request.get('repo_id'): continue
                    repo = store._owned(connection, 'repositories', 'repo_id', grant['owner_ref'], item['repo_ref'])
                    policy = self.repositories[item['policy_digest']]
                    if (repo['status'] != 'enrolled' or repo['remote_identity'] != policy.remote_identity
                            or request.get('remote_identity') != policy.remote_identity
                            or request.get('policy_digest') != policy.sha256): continue
                    if (self.policy['version']==2 and grant['mode']=='work'
                            and task['workspace_ref']!=item['workspace_ref']):continue
                    if sync:
                        if request.get('repository_revision') != repo['revision']: continue
                    elif (request.get('project_id') != grant['project_ref'] or request.get('task_mode') != grant['mode']): continue
                    matches.append((grant, item))
        if not matches: fail()
        # A repo sync may serve several grants; all grant the same read action.
        return matches[0]

    def authorize(self, request):
        action = request.get('action')
        if action not in READ_ACTIONS | WRITE_ACTIONS: fail()
        grant, item = self.current(request, sync=action == 'sync')
        if action in WRITE_ACTIONS and (item['access'] != 'write' or grant['mode'] == 'work'): fail()
        if action == 'bind_code_task':
            target = self.policy['target']
            if (grant['code_task_ref'] is None or request.get('code_task_ref') != grant['code_task_ref']
                    or request.get('code_revision') != grant['code_revision']
                    or request.get('target_ref') != target['target_ref']
                    or request.get('permissions_digest') != target['permissions_digest']): fail()
            self.current(dict(request, task_id=grant['code_task_ref'], task_revision=grant['code_revision'],task_mode='work'))
        if action in {'prepare_publish', 'publish', 'reconcile_publish', 'observe_publication'}:
            if grant['mode'] not in {'deliver', 'deploy'}: fail()
            policy = self.repositories[item['policy_digest']]
            if request.get('destination_ref') not in dict(policy.publish_targets).values(): fail()
        return AuthorizationReceipt(digest(canonical(request).encode()), grant['grant_ref'])

    def workspaces(self, task):
        if self.policy['version']!=1:fail()
        matching = [grant for grant in self.policy['grants'] if grant['owner_ref'] == task.get('owner_ref')
                    and grant['task_ref'] == task.get('task_id') and grant['task_revision'] == task.get('revision')]
        if len(matching) != 1: fail()
        grant = matching[0]; result = []
        for item in grant['repositories']:
            policy = self.repositories[item['policy_digest']]
            request = dict(owner_ref=grant['owner_ref'], task_id=grant['task_ref'], task_revision=grant['task_revision'],
                project_id=grant['project_ref'], task_mode=grant['mode'], repo_id=item['repo_ref'],
                remote_identity=policy.remote_identity, policy_digest=policy.sha256)
            self.current(request); self.readonly(item)
            result.append(RepositoryWorkspace(grant['owner_ref'], grant['task_ref'], grant['task_revision'],
                item['repo_ref'], policy.remote_identity, item['workspace_ref'], Path(item['root'])))
        return tuple(result)

    def quiescent(self, context):
        if self.policy['version']!=1:fail()
        _, item = self.current(context)
        if context.get('workspace_ref') != item['workspace_ref']: fail()
        return self.readonly(item)

    def inputs(self, context, files):
        self.current(context)
        scan = SourceScanner().scan(files)
        self.current(context)
        return SourceExposureReceipt(digest(canonical(context).encode()), context['manifest_digest'], scan, True)

    def target(self, target_ref, request, *, deadline):
        self.plan.verify(); target = self.policy['target']
        if (target_ref != target['target_ref'] or request.get('target_ref') != target_ref
                or request.get('project_ref') != target['project_ref']
                or request.get('permissions_digest') != target['permissions_digest']): fail()
        grants = [grant for grant in self.policy['grants'] if grant['owner_ref'] == request.get('owner_ref')
                  and grant['task_ref'] == request.get('task_ref') and grant['task_revision'] == request.get('revision')]
        if len(grants) != 1: fail()
        grant = grants[0]; candidates = request.get('candidates')
        if type(candidates) is not list or not 1 <= len(candidates) <= 32: fail()
        expected = {(item['repo_ref'], item['policy_digest']) for item in grant['repositories']}
        if (any(type(item) is not dict for item in candidates) or len(candidates) != len(expected)
                or {(item.get('repo_id'), item.get('policy_digest')) for item in candidates} != expected): fail()
        # Recheck the entire canonical scope on both sides of the network read.
        def current():
            for candidate in candidates:
                self.current(dict(owner_ref=request['owner_ref'], task_id=request['task_ref'],
                    task_revision=request['revision'], project_id=request['project_ref'], task_mode=grant['mode'],
                    repo_id=candidate['repo_id'], policy_digest=candidate['policy_digest'],
                    remote_identity=self.repositories[candidate['policy_digest']].remote_identity))
        current()
        observed = self.observer(target_ref, 'manager', deadline=deadline)
        if type(observed) is not dict: fail()
        actual = dict(observed); expiry = actual.pop('qualification_expires_at', None); _expiry(expiry)
        if actual != target['observation']: fail()
        current()
        return dict(target_ref=target_ref, project_ref=target['project_ref'], permissions_digest=target['permissions_digest'],
                    evidence_digest=digest(canonical(observed).encode()))
