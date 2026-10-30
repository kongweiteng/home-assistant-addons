"""Fixed, offline OCI assembly; no Engine/import/Release authority.

All Git is read as objects. Application code and base layers are never executed
or extracted. Observation reconstructs the artifact from retained source bytes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from io import BytesIO
import gzip
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile
import time

from .durability import digest
from .repo_manager import GitRuntime, _oid, _private, _read, _sha
from .store import DeliveryError, canonical

MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
CONFIG = 'application/vnd.oci.image.config.v1+json'
LAYER = 'application/vnd.oci.image.layer.v1.tar'
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_SOURCE = 32 * 1024 * 1024
REPO = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')


def _fail():
    raise DeliveryError('oci_build_evidence_invalid')


def _deadline(value):
    if type(value) not in {int, float} or not math.isfinite(value) or time.monotonic() >= value:
        raise DeliveryError('oci_build_deadline')


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: _fail()
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: _fail())
    except (ValueError, UnicodeError, TypeError, RecursionError):
        _fail()


def _encoded(value):
    return canonical(value).encode()


def _path(name):
    if (not isinstance(name, str) or not name or len(name.encode()) > 240 or '\\' in name
            or any(ord(char) < 32 for char in name) or name.startswith('/')
            or str(PurePosixPath(name)) != name
            or any(part in {'..', '.', ''} or part.lower() in {'.git', '.gitmodules'}
                   or part.endswith((' ', '.')) or ':' in part for part in name.split('/'))):
        _fail()
    return name


def _tar(files):
    stream = BytesIO()
    try:
        with tarfile.open(fileobj=stream, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for name, (data, mode) in sorted(files.items()):
                item = tarfile.TarInfo(name)
                item.size, item.mode, item.mtime, item.uid, item.gid = len(data), mode, 0, 0, 0
                archive.addfile(item, BytesIO(data))
    except (ValueError, UnicodeError, tarfile.TarError): _fail()
    return stream.getvalue()


def _members(raw, deadline):
    if not isinstance(raw, bytes) or not 1024 <= len(raw) <= MAX_ARCHIVE: _fail()
    result = {}; total = 0
    try:
        with tarfile.open(fileobj=BytesIO(raw), mode='r:') as archive:
            for item in archive:
                _deadline(deadline)
                name = item.name.rstrip('/') if item.isdir() else item.name
                _path(name)
                if (name in result or len(result) >= 128 or item.size < 0
                        or item.size > MAX_ARCHIVE or item.pax_headers or item.issparse()
                        or not (item.isfile() or item.isdir())): _fail()
                total += item.size
                if total > MAX_ARCHIVE: _fail()
                result[name] = None if item.isdir() else archive.extractfile(item).read(item.size + 1)
                if result[name] is not None and len(result[name]) != item.size: _fail()
    except (tarfile.TarError, OSError, ValueError, EOFError):
        _fail()
    return result


def _descriptor(media, raw):
    return dict(mediaType=media, digest=digest(raw), size=len(raw))


def _blob_name(hashed):
    return 'blobs/sha256/' + _sha(hashed)[7:]


def _base(raw, recipe, deadline):
    if digest(raw) != recipe.base_archive_digest: _fail()
    files = _members(raw, deadline)
    if _json(files.get('oci-layout')) != {'imageLayoutVersion': '1.0.0'}: _fail()
    index = _json(files.get('index.json'))
    if (not isinstance(index, dict) or index.get('schemaVersion') != 2
            or not isinstance(index.get('manifests'), list) or len(index['manifests']) != 1): _fail()
    used = {'oci-layout', 'index.json'}

    def blob(desc, kinds):
        if (not isinstance(desc, dict) or set(desc) - {'mediaType', 'digest', 'size', 'annotations', 'platform'}
                or desc.get('mediaType') not in kinds or type(desc.get('size')) is not int): _fail()
        name = _blob_name(desc.get('digest')); data = files.get(name)
        if not isinstance(data, bytes) or len(data) != desc['size'] or digest(data) != desc['digest']: _fail()
        used.add(name)
        return data

    manifest_desc = index['manifests'][0]
    manifest_raw = blob(manifest_desc, {MANIFEST})
    if digest(manifest_raw) != recipe.base_manifest_digest: _fail()
    platform = manifest_desc.get('platform')
    if platform is not None and platform != {'os': 'linux', 'architecture': recipe.architecture}: _fail()
    manifest = _json(manifest_raw)
    if (not isinstance(manifest, dict) or set(manifest) - {'schemaVersion', 'mediaType', 'config', 'layers', 'annotations'}
            or manifest.get('schemaVersion') != 2 or manifest.get('mediaType', MANIFEST) != MANIFEST
            or not isinstance(manifest.get('layers'), list) or not 1 <= len(manifest['layers']) <= 60): _fail()
    config_raw = blob(manifest.get('config'), {CONFIG})
    config = _json(config_raw)
    if (not isinstance(config, dict) or config.get('architecture') != recipe.architecture
            or config.get('os') != 'linux'): _fail()
    layers = []; diffs = []; expanded = 0
    for desc in manifest['layers']:
        data = blob(desc, {LAYER, LAYER + '+gzip'})
        compressed = desc['mediaType'].endswith('+gzip')
        if compressed != data.startswith(b'\x1f\x8b'): _fail()
        try:
            source = gzip.GzipFile(fileobj=BytesIO(data)) if compressed else BytesIO(data)
            import hashlib
            hashed = hashlib.sha256()
            with source:
                while True:
                    _deadline(deadline)
                    part = source.read(min(65536, MAX_EXPANDED - expanded + 1))
                    if not part: break
                    expanded += len(part)
                    if expanded > MAX_EXPANDED: _fail()
                    hashed.update(part)
        except (OSError, ValueError, EOFError): _fail()
        diffs.append('sha256:' + hashed.hexdigest()); layers.append((desc, data))
    if config.get('rootfs') != {'type': 'layers', 'diff_ids': diffs}: _fail()
    if 'manifest.json' in files:
        compat = _json(files['manifest.json'])
        if (not isinstance(compat, list) or len(compat) != 1 or not isinstance(compat[0], dict)
                or set(compat[0]) != {'Config', 'RepoTags', 'Layers'}
                or compat[0]['Config'] != _blob_name(manifest['config']['digest'])
                or compat[0]['Layers'] != [_blob_name(item['digest']) for item, _ in layers]
                or not isinstance(compat[0]['RepoTags'], list)
                or any(not isinstance(tag, str) for tag in compat[0]['RepoTags'])): _fail()
        used.add('manifest.json')
    if {name for name, data in files.items() if data is not None} != used: _fail()
    return layers, diffs


@dataclass(frozen=True)
class BuildRecipe:
    profile: str
    repo_ref: str
    source_prefix: str
    architecture: str
    base_archive_digest: str
    base_manifest_digest: str
    permissions_digest: str
    qualification_digest: str
    target_ref: str = ''

    def __post_init__(self):
        if (not isinstance(self.profile, str) or self.profile not in {'python-service-v1', 'node-service-v1', 'ha-canary-python-v1'}
                or not isinstance(self.repo_ref, str) or not REPO.fullmatch(self.repo_ref)
                or not isinstance(self.architecture, str) or self.architecture not in {'amd64', 'arm64'}
                or not isinstance(self.source_prefix, str)): _fail()
        if not isinstance(self.target_ref,str) or (self.target_ref and not REPO.fullmatch(self.target_ref)): _fail()
        if self.profile=='ha-canary-python-v1' and not self.target_ref: _fail()
        if self.source_prefix: _path(self.source_prefix)
        for value in (self.base_archive_digest, self.base_manifest_digest,
                      self.permissions_digest, self.qualification_digest): _sha(value)


@dataclass(frozen=True)
class ArtifactBuildEvidence:
    request_digest: str
    recipe_digest: str
    artifact_blob_digest: str
    image_config_digest: str
    image_manifest_digest: str
    context_digest: str
    evidence_digest: str


def _inputs(request, files, recipe):
    if (not isinstance(request, dict) or not isinstance(files, dict)
            or not 3 <= len(files) <= 65
            or any(not isinstance(data, bytes) for data in files.values())
            or sum(map(len, files.values())) > 128 * 1024 * 1024): _fail()
    raw = files.get('software.json'); manifest = _json(raw)
    if (not isinstance(manifest, dict) or set(manifest) != {'binding_ref', 'binding', 'candidates'}
            or raw != _encoded(manifest) or digest(raw) != request.get('source_digest')
            or manifest['binding_ref'] != request.get('binding_ref')
            or manifest['candidates'] != request.get('candidates')
            or not isinstance(manifest['binding'], dict)): _fail()
    binding = manifest['binding']
    if not isinstance(request.get('version'),str) or not re.fullmatch(r'[0-9][A-Za-z0-9_.-]{0,63}',request['version']): _fail()
    for key in ('owner_ref', 'task_ref', 'revision', 'project_ref', 'target_ref', 'release_ref', 'version', 'permissions_digest'):
        if key not in binding or binding[key] != request.get(key): _fail()
    if request['permissions_digest'] != recipe.permissions_digest: _fail()
    if recipe.target_ref and request.get('target_ref')!=recipe.target_ref: _fail()
    candidates = manifest['candidates']
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 32: _fail()
    expected = {'software.json'}; selected = None; repos = set()
    sources = binding.get('repositories')
    if not isinstance(sources, list) or len(sources) != len(candidates): _fail()
    for candidate in candidates:
        if not isinstance(candidate, dict): _fail()
        repo = candidate.get('repo_id')
        if not isinstance(repo, str) or not REPO.fullmatch(repo) or repo in repos: _fail()
        repos.add(repo)
        source_matches = [item for item in sources if isinstance(item, dict) and item.get('repo_id') == repo]
        if len(source_matches) != 1: _fail()
        source = source_matches[0]
        if candidate.get('baseline_oid') != source.get('baseline_oid'): _fail()
        _oid(candidate.get('candidate_oid')); _oid(source.get('baseline_oid'))
        for suffix, expected_digest in (('source', source.get('source_bundle_digest')),
                                        ('candidate', candidate.get('bundle_sha256'))):
            name = repo + '-' + suffix + '.bundle'; data = files.get(name)
            _sha(expected_digest)
            if not isinstance(data, bytes) or not 1 <= len(data) <= 64 * 1024 * 1024 or digest(data) != expected_digest: _fail()
            expected.add(name)
        if repo == recipe.repo_ref: selected = candidate
    if set(files) != expected or selected is None: _fail()
    return selected


def _source(request, files, recipe, git, scratch, deadline):
    candidate = _inputs(request, files, recipe)
    budget = git.budget(); budget.deadline = min(budget.deadline, deadline)
    with tempfile.TemporaryDirectory(prefix='.oci-source-', dir=scratch) as temporary:
        root = Path(temporary); home = root / 'home'; home.mkdir(mode=0o700)
        (home / 'no-hooks').mkdir(mode=0o700)
        lock = os.open(root / 'lock', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            repository = root / 'source.git'
            def run(args):
                return git.run(home, repository, args, budget, lockfd=lock)[1]
            git.run(home, None, ['init', '--bare', '--template=', str(repository)], budget, lockfd=lock)
            for kind in ('source', 'candidate'):
                path = root / (kind + '.bundle')
                path.write_bytes(files[recipe.repo_ref + '-' + kind + '.bundle']); path.chmod(0o600)
                run(['bundle', 'unbundle', str(path)])
            baseline = candidate['baseline_oid']; oid = candidate['candidate_oid']
            run(['cat-file', '-e', oid + '^{commit}'])
            run(['merge-base', '--is-ancestor', baseline, oid])
            run(['fsck', '--strict', '--no-reflogs'])
            tree = run(['ls-tree', '-r', '-z', '--full-tree', oid]); context = {}; folded = set(); total = 0
            prefix = recipe.source_prefix + '/' if recipe.source_prefix else ''
            for entry in tree.split(b'\0'):
                if not entry: continue
                try:
                    header, raw_name = entry.split(b'\t', 1)
                    mode, kind, hashed = header.decode('ascii').split(' '); name = raw_name.decode('utf-8')
                except (ValueError, UnicodeError): _fail()
                _path(name); _oid(hashed)
                if mode not in {'100644', '100755'} or kind != 'blob': _fail()
                if not name.startswith(prefix): continue
                relative = _path(name[len(prefix):])
                if relative.casefold() in folded or len(context) >= 4096: _fail()
                folded.add(relative.casefold()); data = run(['cat-file', 'blob', hashed]); total += len(data)
                if total > MAX_SOURCE or data.startswith(b'version https://git-lfs.github.com/spec/v1\n'): _fail()
                if PurePosixPath(name).name == '.gitattributes' and re.search(rb'(?i)filter', data): _fail()
                context['app/' + relative] = (data, 0o755 if mode == '100755' else 0o644)
            entry = 'main.mjs' if recipe.profile == 'node-service-v1' else 'main.py'
            if 'app/' + entry not in context: _fail()
            return context
        finally:
            os.close(lock)


def _assemble(request, files, recipe, base, git, scratch, deadline):
    _deadline(deadline)
    layers, diffs = _base(base, recipe, deadline)
    context = _source(request, files, recipe, git, scratch, deadline)
    layer = _tar(context); desc = _descriptor(LAYER, layer)
    context_digest = digest(_encoded({name: dict(sha256=digest(data), mode=mode)
                                     for name, (data, mode) in sorted(context.items())}))
    command = (['/usr/bin/python3', '-I', '-c',
                'import runpy,sys;sys.path.insert(0,"/app");runpy.run_path("/app/main.py",run_name="__main__")']
               if recipe.profile != 'node-service-v1' else ['/usr/bin/node', '/app/main.mjs'])
    labels={'org.codex.build.request': digest(_encoded(request)),
            'org.codex.build.recipe': digest(_encoded(asdict(recipe))),
            'org.codex.build.context': context_digest}
    environment=['PATH=/usr/bin:/bin', 'TZ=Asia/Shanghai']
    user='65532:65532'
    if recipe.profile=='ha-canary-python-v1':
        user='0:0'
        labels.update({'io.hass.type':'addon','io.hass.version':request['version'],
                       'io.hass.arch':'aarch64' if recipe.architecture=='arm64' else 'amd64'})
        environment.extend(['CODEX_RELEASE_SOURCE_DIGEST='+request['source_digest'],
                            'CODEX_RELEASE_VERSION='+request['version']])
    config = _encoded(dict(architecture=recipe.architecture, os='linux',
        config=dict(User=user, WorkingDir='/app', Entrypoint=command, Cmd=[],Env=environment,Labels=labels),
        rootfs=dict(type='layers', diff_ids=diffs + [digest(layer)])))
    manifest = _encoded(dict(schemaVersion=2, mediaType=MANIFEST, config=_descriptor(CONFIG, config),
                             layers=[item for item, _ in layers] + [desc]))
    output = {_blob_name(item['digest']): (data, 0o644) for item, data in layers}
    output.update({_blob_name(digest(layer)): (layer, 0o644), _blob_name(digest(config)): (config, 0o644),
        _blob_name(digest(manifest)): (manifest, 0o644),
        'oci-layout': (_encoded({'imageLayoutVersion': '1.0.0'}), 0o644),
        'index.json': (_encoded(dict(schemaVersion=2, manifests=[_descriptor(MANIFEST, manifest)])), 0o644),
        'manifest.json': (_encoded([dict(Config=_blob_name(digest(config)), RepoTags=[],
            Layers=[_blob_name(item['digest']) for item, _ in layers] + [_blob_name(digest(layer))])]), 0o644)})
    artifact = _tar(output)
    if len(artifact) > MAX_ARCHIVE: _fail()
    _deadline(deadline)
    facts = dict(request_digest=digest(_encoded(request)), recipe_digest=digest(_encoded(asdict(recipe))),
                 artifact_blob_digest=digest(artifact), image_config_digest=digest(config),
                 image_manifest_digest=digest(manifest), context_digest=context_digest)
    return artifact, ArtifactBuildEvidence(**facts, evidence_digest=digest(_encoded(facts)))


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(data); stream.flush(); os.fsync(fd)
    finally: os.close(fd)


def _sync(path):
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


class FixedOCIBuilder:
    """Administrator-owned offline Port; intentionally not BuildObservation."""
    def __init__(self, root, recipe, base_archive, *, git=None):
        self.root = Path(root)
        if (not self.root.is_absolute() or '..' in self.root.parts
                or not isinstance(recipe, BuildRecipe) or not isinstance(base_archive, bytes)
                or not 1024 <= len(base_archive) <= MAX_ARCHIVE): _fail()
        self.recipe, self.base, self.git = recipe, base_archive, git or GitRuntime(max_output=64 * 1024 * 1024)

    def _directory(self, request):
        return self.root / digest(_encoded(request))[7:]

    def build(self, request, files, *, deadline):
        _deadline(deadline); _inputs(request, files, self.recipe)
        _private(self.root)
        directory = self._directory(request)
        try: directory.mkdir(mode=0o700)
        except FileExistsError: raise DeliveryError('oci_build_already_claimed') from None
        _sync(self.root)
        _write(directory / 'claim.json', _encoded(dict(request_digest=digest(_encoded(request)))))
        _sync(directory)
        inventory = {name: digest(raw) for name, raw in files.items()}
        for name, raw in files.items(): _write(directory / name, raw)
        _write(directory / 'request.json', _encoded(request))
        _write(directory / 'recipe.json', _encoded(asdict(self.recipe)))
        _write(directory / 'base.tar', self.base)
        _write(directory / 'inventory.json', _encoded(inventory)); _sync(directory)
        artifact, evidence = _assemble(request, files, self.recipe, self.base, self.git, self.root, deadline)
        _write(directory / 'artifact.tar', artifact); _sync(directory)
        _write(directory / 'receipt.json', _encoded(asdict(evidence))); _sync(directory)
        return evidence

    def observe(self, request, *, deadline):
        """Reads retained bytes only; creates no claim and never calls build."""
        _deadline(deadline)
        directory = self._directory(request)
        # Reject symlinks and permissive evidence directories without mkdir.
        for path in (self.root, directory):
            if any(parent.is_symlink() for parent in (path, *path.parents)): _fail()
            info = path.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077: _fail()
        if (_read(directory / 'request.json', MAX_SOURCE) != _encoded(request)
                or _read(directory / 'recipe.json', 16384) != _encoded(asdict(self.recipe))
                or _read(directory / 'claim.json', 4096) != _encoded(dict(request_digest=digest(_encoded(request))))): _fail()
        inventory = _json(_read(directory / 'inventory.json', 65536))
        if not isinstance(inventory, dict) or not 3 <= len(inventory) <= 65: _fail()
        files = {}
        for name, hashed in inventory.items():
            if name != 'software.json' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}-(?:source|candidate)\.bundle', name): _fail()
            raw = _read(directory / name, 64 * 1024 * 1024)
            if digest(raw) != _sha(hashed): _fail()
            files[name] = raw
        expected = set(files) | {'claim.json', 'request.json', 'recipe.json', 'base.tar', 'inventory.json', 'artifact.tar', 'receipt.json'}
        if {path.name for path in directory.iterdir()} != expected: _fail()
        base = _read(directory / 'base.tar', MAX_ARCHIVE)
        artifact, evidence = _assemble(request, files, self.recipe, base, self.git, self.root, deadline)
        if (_read(directory / 'artifact.tar', MAX_ARCHIVE) != artifact
                or _read(directory / 'receipt.json', 16384) != _encoded(asdict(evidence))): _fail()
        return evidence
