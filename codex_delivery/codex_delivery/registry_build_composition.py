"""Fixed image-owned Registry build boot candidate; no listener or execution.

Loading validates root-provenance data and a complete offline base. A created
port set may attach once to the original RepositoryManager/Store/Archive after
privilege drop. Missing and failed attachment cannot turn into a new attempt.
"""
from dataclasses import asdict, dataclass, field
import base64
import hmac
import ipaddress
import os
from pathlib import Path
import stat
import threading
import time
from urllib.parse import urlsplit

from .bootstrap import STATE_ROOT
from .durability import digest
from .oci_builder import BuildRecipe, FixedOCIBuilder, MAX_ARCHIVE, _base, _json
from .object_archive import ObjectArchive
from .registry_build import BuildEvidenceProjection, RegistryBuildBridge, hashed
from .registry_build_client import BuildIssuerClient
from .registry_build_routes import BuildEvidenceRoutes
from .repo_manager import RepoManager
from .store import DeliveryError, reference
from ha_artifact_registry.ha_artifact_registry.http import PublicationClient, RegistryObserver
from ha_artifact_registry.ha_artifact_registry.service import Binding, REF, private

REGISTRATION_PATH = Path('/etc/codex-delivery/registry-build/registration.json')
POLICY_PATH = Path('/opt/codex-delivery/registry-build-policy.json')
BASE_PATH = Path('/opt/codex-delivery/registry-build/base.oci.tar')
LIMIT = 32768
TOKENS = frozenset({'issuer_token', 'writer_token', 'reader_token', 'evidence_token'})


def fail():
    raise DeliveryError('registry_build_registration_invalid') from None


def _closed(value, fields):
    if type(value) is not dict or set(value) != fields: fail()
    return value


def _read_root(path, *, private_leaf=False, optional=False, limit=LIMIT):
    """Pin every root-owned ancestor and file; never follow a path alias."""
    parent = descriptor = None
    try:
        if not path.is_absolute() or any(part in {'.', '..'} for part in path.parts): fail()
        parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:-1]:
            info = os.fstat(parent)
            if info.st_uid != 0 or info.st_mode & 0o022: fail()
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent); parent = child
        info = os.fstat(parent)
        if (info.st_uid != 0 or info.st_mode & 0o022
                or private_leaf and stat.S_IMODE(info.st_mode) != 0o700): fail()
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
                or before.st_mode & 0o022 or private_leaf and stat.S_IMODE(before.st_mode) != 0o600
                or not 1 <= before.st_size <= limit): fail()
        raw = bytearray()
        while len(raw) <= before.st_size:
            block = os.read(descriptor, min(65536, before.st_size + 1 - len(raw)))
            if not block: break
            raw.extend(block)
        after = os.fstat(descriptor)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if len(raw) != before.st_size or identity(before) != identity(after): fail()
        return bytes(raw)
    except FileNotFoundError:
        if optional: return None
        fail()
    except Exception: fail()
    finally:
        for opened in (descriptor, parent):
            if opened is not None: os.close(opened)


def _origin(value):
    try:
        parsed = urlsplit(value); address = ipaddress.ip_address(parsed.hostname)
        if (type(value) is not str or parsed.scheme != 'http' or address.version != 4
                or not (address.is_private or address.is_loopback) or address.is_unspecified or address.is_multicast
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                or not parsed.port or value != 'http://' + str(address) + ':' + str(parsed.port)): fail()
        return value
    except Exception: fail()


def _policy(raw):
    if type(raw) is not bytes or not 1 <= len(raw) <= LIMIT: fail()
    policy = _closed(_json(raw), {'version', 'service_uid', 'owner_ref', 'project_ref', 'repo_policies',
        'broker_origin', 'registry_origin', 'delegation_ref', 'public_key', 'recipe', 'binding', 'forbidden_token_sha256'})
    if (type(policy['version']) is not int or policy['version'] != 1
            or type(policy['service_uid']) is not int or not 1 <= policy['service_uid'] <= 2**31-1): fail()
    for name in ('owner_ref', 'project_ref'): reference(policy[name])
    if type(policy['delegation_ref']) is not str or not REF.fullmatch(policy['delegation_ref']): fail()
    _origin(policy['broker_origin']); _origin(policy['registry_origin'])
    if policy['broker_origin'] == policy['registry_origin']: fail()
    recipe = BuildRecipe(**_closed(policy['recipe'], set(BuildRecipe.__dataclass_fields__)))
    binding = Binding(**_closed(policy['binding'], set(Binding.__dataclass_fields__)))
    if (recipe.profile != 'ha-canary-python-v1' or binding.target_ref != recipe.target_ref
            or binding.architecture != recipe.architecture or binding.recipe_digest != hashed(asdict(recipe))
            or binding.permissions_digest != recipe.permissions_digest
            or binding.qualification_digest != recipe.qualification_digest): fail()
    repos = policy['repo_policies']
    if type(repos) is not dict or not 1 <= len(repos) <= 32 or recipe.repo_ref not in repos: fail()
    from .repo_manager import _sha
    for repo, identity in repos.items(): reference(repo); _sha(identity)
    fingerprints = policy['forbidden_token_sha256']
    if type(fingerprints) is not list or len(fingerprints) > 128 or len(set(fingerprints)) != len(fingerprints): fail()
    for value in fingerprints: _sha(value)
    public_key = base64.b64decode(policy['public_key'], validate=True)
    if len(public_key) != 32 or base64.b64encode(public_key).decode() != policy['public_key']: fail()
    return policy, recipe, binding, public_key


@dataclass(frozen=True, repr=False)
class RegistryBuildBootPlan:
    recipe: BuildRecipe
    binding: Binding
    owner_ref: str
    project_ref: str
    repo_policies: tuple
    service_uid: int
    broker_origin: str
    registry_origin: str
    delegation_ref: str
    public_key: bytes = field(repr=False)
    _base: bytes = field(repr=False)
    _tokens: tuple = field(repr=False)

    @classmethod
    def load(cls, *, forbidden_credentials=()):
        try:
            # An image without this optional registration remains metadata-only
            # even on development hosts whose /etc is a platform symlink. An
            # existing registration still requires every no-follow/root check.
            try:
                REGISTRATION_PATH.lstat()
            except FileNotFoundError:
                return None
            raw = _read_root(REGISTRATION_PATH, private_leaf=True, optional=True)
            if raw is None: return None
            registration = _json(raw)
            if (registration == {'version': 1, 'enabled': False} and type(registration['version']) is int
                    and registration['enabled'] is False): return None
            _closed(registration, {'version', 'enabled', 'policy_sha256', *TOKENS})
            if registration['enabled'] is not True: fail()
            return cls.parse(raw, _read_root(POLICY_PATH), _read_root(BASE_PATH, limit=MAX_ARCHIVE),
                             forbidden_credentials=forbidden_credentials)
        except Exception: fail()

    @classmethod
    def parse(cls, raw, policy_raw, base, *, forbidden_credentials=()):
        """Data-only validation for trusted boot/tests; does not prove file ownership."""
        try:
            if type(raw) is not bytes or not 1 <= len(raw) <= LIMIT: fail()
            registration = _closed(_json(raw), {'version', 'enabled', 'policy_sha256', *TOKENS})
            policy, recipe, binding, public_key = _policy(policy_raw)
            if (type(registration['version']) is not int or registration['version'] != 1
                    or registration['enabled'] is not True or registration['policy_sha256'] != digest(policy_raw)
                    or type(base) is not bytes or not 1024 <= len(base) <= MAX_ARCHIVE): fail()
            if not isinstance(forbidden_credentials, (tuple, list)) or any(type(v) not in {str, bytes} for v in forbidden_credentials): fail()
            used = [v.encode() if type(v) is str else v for v in forbidden_credentials]
            tokens = []
            for name in sorted(TOKENS):
                value = registration[name]
                if type(value) is not str or not 32 <= len(value) <= 256 or any(not 33 <= ord(c) <= 126 for c in value): fail()
                encoded = value.encode('ascii')
                if any(hmac.compare_digest(encoded, other) for other in used) or digest(encoded) in policy['forbidden_token_sha256']: fail()
                used.append(encoded); tokens.append((name, value))
            _base(base, recipe, time.monotonic() + 30)
            return cls(recipe, binding, policy['owner_ref'], policy['project_ref'], tuple(sorted(policy['repo_policies'].items())),
                policy['service_uid'], policy['broker_origin'], policy['registry_origin'], policy['delegation_ref'], public_key, base, tuple(tokens))
        except Exception: fail()

    def create(self):
        return RegistryBuildPorts(self)


class _ScopedProjection(BuildEvidenceProjection):
    def __init__(self, manager, archive, builder, root, plan):
        super().__init__(manager, archive, builder, root)
        self._scope = plan

    def _request(self, request_digest, deadline):
        request = super()._request(request_digest, deadline)
        plan = self._scope
        if (request['owner_ref'] != plan.owner_ref or request['project_ref'] != plan.project_ref
                or request['target_ref'] != plan.binding.target_ref
                or {item['repo_id']: item['policy_digest'] for item in request['candidates']} != dict(plan.repo_policies)): fail()
        return request


class RegistryBuildPorts:
    """Lazy fixed Ports to break the factory/manager dependency without retries."""
    def __init__(self, plan):
        if type(plan) is not RegistryBuildBootPlan: fail()
        self._plan = plan
        self._lock = threading.Lock()
        self._attempted = False
        self._bridge = self._routes = None

    def attach(self, manager, archive, state_root):
        with self._lock:
            if self._attempted: fail()
            self._attempted = True
            try:
                root = Path(state_root); plan = self._plan
                if (type(manager) is not RepoManager or type(archive) is not ObjectArchive
                        or root != STATE_ROOT or manager.store.path.parent != root
                        or archive.replica is not manager.store.replica
                        or archive.local_domain != manager.store.local_domain
                        or archive.replica_domain != manager.store.replica_domain
                        or os.getuid() != os.geteuid() or os.geteuid() != plan.service_uid): fail()
                private(root)
                for path in (root/'oci-builds', root/'registry-builds'):
                    if path.exists() or path.is_symlink(): private(path)
                tokens = dict(plan._tokens)
                builder = FixedOCIBuilder(root/'oci-builds', plan.recipe, plan._base, git=manager.git)
                projection = _ScopedProjection(manager, archive, builder, root/'registry-builds', plan)
                issuer = BuildIssuerClient(plan.broker_origin, tokens['issuer_token'], timeout=5)
                writer = PublicationClient(plan.registry_origin, tokens['writer_token'], timeout=120)
                reader = RegistryObserver(plan.registry_origin, tokens['reader_token'], public_key=plan.public_key,
                                          binding=plan.binding, grants=(), timeout=120)
                bridge = RegistryBuildBridge(projection, delegation_ref=plan.delegation_ref,
                                              issuer=issuer, publisher=writer, observer=reader)
                routes = BuildEvidenceRoutes(projection, token=tokens['evidence_token'], owner_refs=frozenset({plan.owner_ref}))
                self._bridge, self._routes = bridge, routes
            except Exception: fail()
        return self

    def execute(self, request, files, *, deadline):
        if self._bridge is None: fail()
        return self._bridge.execute(request, files, deadline=deadline)

    def observe(self, request, *, deadline):
        if self._bridge is None: fail()
        return self._bridge.observe(request, deadline=deadline)

    @property
    def evidence_routes(self):
        if self._routes is None: fail()
        return self._routes
