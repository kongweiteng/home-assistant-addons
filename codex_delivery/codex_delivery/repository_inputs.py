"""Read a reviewed source view from one authenticated sealed repository backup.

The full recovery tree stays in private trusted temporary storage. Git config,
raw .git objects, ignored/untracked bytes and credential-shaped paths never enter
the guest view. A qualified independent content gate is mandatory; filename
checks alone are not a credential scan or authorization to expose source bytes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import io
from pathlib import Path
import stat
import tarfile
import tempfile
from types import MappingProxyType

from .durability import digest
from .object_archive import MAX_BYTES, _name
from .repo_manager import RepositoryArchive, _private, _read, _sha
from .store import DeliveryError, canonical


@dataclass(frozen=True)
class SourceExposureReceipt:
    context_digest: str
    manifest_digest: str
    scan_digest: str
    passed: bool


@dataclass(frozen=True, repr=False)
class RepositoryInputView:
    metadata: dict
    files: object

    def package(self):
        """One bounded USTAR input; no raw Git/config tree or host extraction."""
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for item in self.metadata['manifest']:
                name = _permitted_name(item['name'])
                content = self.files[name]
                if len(content) != item['size_bytes'] or digest(content) != item['sha256']:
                    raise DeliveryError('repository_input_file_invalid')
                if output.tell()+len(content)+1024 > MAX_BYTES:
                    raise DeliveryError('repository_input_budget_exceeded')
                member = tarfile.TarInfo(name)
                member.mode = 0o500 if item['executable'] else 0o400
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        value = output.getvalue()
        if len(value) > MAX_BYTES: raise DeliveryError('repository_input_budget_exceeded')
        return value


_SENSITIVE_NAMES = frozenset({'.env', '.netrc', '.npmrc', '.pypirc', '.git-credentials',
    '.secrets', '.ssh', '.aws', 'id_rsa', 'id_ed25519', 'secrets.yaml', 'credentials.json',
    'auth.json'})


def _permitted_name(name):
    parts = _name(name).casefold().split('/')
    if ('.git' in parts or any(part in _SENSITIVE_NAMES or part.startswith('.env.') for part in parts)
            or parts[-1].endswith(('.key', '.p12', '.pfx'))):
        raise DeliveryError('repository_input_sensitive_path')
    return name


def sealed_repository_inputs(archives: RepositoryArchive, owner, key, *, exposure_gate):
    if not callable(exposure_gate):
        raise DeliveryError('repository_input_gate_unavailable')
    result, context, policy, binding = archives._record(owner, key)
    if archives.store.recovery_mode:
        raise DeliveryError('recovery_read_only')
    current, _ = archives.manager._context(owner, context['task_id'], context['task_revision'], binding.repo_ref)
    if any(current[field] != context[field] for field in current):
        raise DeliveryError('repository_input_scope_invalid')
    from contextlib import closing
    with closing(archives.store._connect()) as connection:
        confirmed = connection.execute('SELECT d.state FROM commands c JOIN durability d USING(event_id) '
            'WHERE c.owner_ref=? AND c.idempotency_key=?', (owner, key)).fetchone()
    if confirmed is None or confirmed['state'] != 'confirmed':
        raise DeliveryError('repository_input_source_pending')
    with closing(archives.store._connect()) as connection:
        source_artifact,source_metadata=archives.manager._source(connection,current)
    staging = archives.manager.root / 'input-views'
    _private(staging)
    try:
        with tempfile.TemporaryDirectory(prefix='.sealed-source-', dir=staging) as temporary:
            destination = Path(temporary) / 'verified'
            archives._restore_verified_tree(owner, key, destination=destination, action='export_execution_input')
            workspace = destination / 'workspace'
            lock = Path(temporary) / 'input.lock'
            import os
            fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                budget = archives.manager.git.budget()
                arguments = ['-c', 'core.excludesFile=/dev/null', '--no-optional-locks',
                             '--work-tree=' + str(workspace)]
                _,head_raw=archives.manager._run(workspace/'.git',['rev-parse','--verify','HEAD'],budget,fd)
                from .repo_manager import _oid
                head_oid=_oid(head_raw.decode('ascii').strip())
                _, raw = archives.manager._run(workspace / '.git',
                    [*arguments, 'ls-files', '--cached', '--stage', '-z'], budget, fd)
                _, ignored_raw = archives.manager._run(workspace / '.git',
                    [*arguments, 'ls-files', '--cached', '--ignored', '--exclude-standard', '-z'], budget, fd)
                ignored = {_name(item.decode('utf-8')) for item in ignored_raw.split(b'\x00') if item}
                files, manifest, excluded, names, total = {}, [], [], set(), 0
                for entry in raw.split(b'\x00'):
                    if not entry: continue
                    header, encoded = entry.split(b'\t', 1)
                    mode, oid, stage = header.decode('ascii').split(' ')
                    name = _name(encoded.decode('utf-8'))
                    if (mode not in {'100644', '100755'} or stage != '0'
                            or name.casefold() in names or len(names) >= RepositoryArchive.MAX_ENTRIES):
                        raise DeliveryError('repository_input_index_invalid')
                    names.add(name.casefold())
                    if name in ignored:
                        excluded.append(name)
                        continue
                    _permitted_name(name)
                    path = workspace.joinpath(*name.split('/'))
                    if not path.exists():
                        # A sealed working-tree deletion is a real source fact,
                        # never replaced with the index or a live checkout read.
                        excluded.append(name)
                        continue
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise DeliveryError('repository_input_file_invalid')
                    content = _read(path, MAX_BYTES-total)
                    total += len(content)
                    if total > MAX_BYTES: raise DeliveryError('repository_input_budget_exceeded')
                    files[name] = content
                    manifest.append(dict(name=name, size_bytes=len(content), sha256=digest(content),
                                         executable=bool(info.st_mode & 0o100)))
            finally:
                os.close(fd)
            manifest.sort(key=lambda item: item['name'])
            source_context = dict(context, backup_ref=result['backup_ref'], state_sha256=result['state_sha256'],
                baseline_oid=source_metadata['baseline_oid'],head_oid=head_oid,
                source_bundle_ref=source_artifact['artifact_ref'],source_bundle_digest=source_artifact['sha256'],
                archive_digest=digest(canonical(result['archive']).encode()),
                manifest_digest=digest(canonical(manifest).encode()), excluded_paths=sorted(excluded))
            try:
                receipt = exposure_gate(dict(source_context), MappingProxyType(dict(files)))
                if (type(receipt) is not SourceExposureReceipt or receipt.passed is not True
                        or receipt.context_digest != digest(canonical(source_context).encode())
                        or receipt.manifest_digest != source_context['manifest_digest']):
                    raise ValueError()
                _sha(receipt.scan_digest)
            except Exception:
                raise DeliveryError('repository_input_exposure_denied') from None
            after, _ = archives.manager._context(owner, context['task_id'], context['task_revision'], binding.repo_ref)
            if after != current:
                raise DeliveryError('repository_input_scope_invalid')
            archives.manager._authorize('export_execution_input', context)
            metadata = dict(version=1, repo_ref=binding.repo_ref, workspace_ref=binding.workspace_ref,
                backup_ref=result['backup_ref'], state_sha256=result['state_sha256'],
                baseline_oid=source_metadata['baseline_oid'],head_oid=head_oid,
                source_bundle_ref=source_artifact['artifact_ref'],source_bundle_digest=source_artifact['sha256'],
                source_context_digest=digest(canonical(source_context).encode()),
                manifest=manifest, excluded_paths=sorted(excluded), exposure=asdict(receipt))
            return RepositoryInputView(metadata, MappingProxyType(files))
    except DeliveryError:
        raise
    except Exception:
        raise DeliveryError('repository_input_unavailable') from None
