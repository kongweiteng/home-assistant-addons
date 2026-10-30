"""Create a new credential-free Delivery build context; never builds/deploys.

Only policy-pinned asset files and the public single-source Python packages are
copied. Runtime ownership is checked again inside the eventual image at boot.
There is no session/key/options input and no arbitrary Dockerfile or executable
selection. Failed staging leaves its private partial directory for inspection;
only a complete context has staging-receipt.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time

ADDON = Path(__file__).resolve().parents[1]
if str(ADDON.parent) not in sys.path:
    sys.path.insert(0, str(ADDON.parent))
try:
    from codex_delivery.codex_delivery.execution_policy import ExecutionPolicy, digest as policy_digest
    from codex_delivery.codex_delivery.release_authority_composition import parse_policy as parse_authority_policy
except ModuleNotFoundError:
    from codex_delivery.execution_policy import ExecutionPolicy, digest as policy_digest
    from codex_delivery.release_authority_composition import parse_policy as parse_authority_policy

SHA = re.compile(r'sha256:[a-f0-9]{64}\Z')
MAX_ASSETS = 2048
MAX_BYTES = 2*1024**3


class StagingError(RuntimeError):
    pass


def fail():
    raise StagingError('execution_staging_invalid') from None


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail()
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: fail())


def hashed(raw):
    return 'sha256:'+hashlib.sha256(raw).hexdigest()


def name(value):
    if (type(value) is not str or not value or len(value) > 255 or '\\' in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or PurePosixPath(value).is_absolute() or PurePosixPath(value).as_posix() != value
            or any(part in {'', '.', '..'} for part in value.split('/'))):
        fail()
    return value


def read(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 <= before.st_size <= limit:
            fail()
        raw = source.read(limit+1)
        after = os.fstat(source.fileno())
    if (len(raw) != before.st_size or (before.st_size, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)):
        fail()
    return raw


def copy(path, destination, sha, size, mode):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size
                or type(size) is not int or not 0 <= size <= MAX_BYTES):
            fail()
        checksum, count = hashlib.sha256(), 0
        output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(output, 'wb') as target:
            while block := source.read(1024*1024):
                count += len(block)
                if count > size:
                    fail()
                checksum.update(block)
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
        after = os.fstat(source.fileno())
    if (count != size or 'sha256:'+checksum.hexdigest() != sha
            or (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
                != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)):
        fail()
    destination.chmod(mode)


def stage(policy_path, destination, runtime_roots, *, cli_path=None, cli_digest=None, authority_policy_path=None,
          repository_policy_path=None, registry_build_policy_path=None, registry_base_path=None,
          repository_git_policy_path=None):
    try:
        raw = read(Path(policy_path), 65536)
        policy = ExecutionPolicy.parse(raw)
        expected = {runtime.runtime_ref for runtime in policy.runtimes}
        if type(runtime_roots) is not dict or set(runtime_roots) != expected:
            fail()
        authority_raw = None
        authority_source = None
        software_assets = []
        repository_policy = registry_policy = git_policy = None
        if authority_policy_path is not None:
            authority_source = Path(authority_policy_path)
            if authority_source.resolve(strict=True) != authority_source:
                fail()
            authority_raw = read(authority_source,32768)
            parse_authority_policy(authority_raw)
        # These are closed image data, never private registration or keys.
        # Executable module locations are fixed in this source, not in JSON.
        for source, filename, limit, kind in (
                (repository_policy_path, 'repository-policy.json', 131072, 'repository'),
                (registry_build_policy_path, 'registry-build-policy.json', 32768, 'registry')):
            if source is None:
                continue
            source = Path(source)
            if source.resolve(strict=True) != source:
                fail()
            content = read(source, limit)
            if kind == 'repository':
                try:
                    from codex_delivery.codex_delivery.repository_boot import _policy as parse_repository
                except ModuleNotFoundError:
                    from codex_delivery.repository_boot import _policy as parse_repository
                repository_policy, _, _ = parse_repository(content)
                selected = repository_policy
                if any(item['runtime_id'] not in expected for item in selected['test_bindings']):
                    fail()
            else:
                try:
                    from codex_delivery.codex_delivery.registry_build_composition import _policy as parse_registry
                except ModuleNotFoundError:
                    from codex_delivery.registry_build_composition import _policy as parse_registry
                registry_policy, recipe, _, _ = parse_registry(content)
                selected = registry_policy
            if selected['service_uid'] != policy.host.uid:
                fail()
            software_assets.append((source, filename, content))
        if (registry_build_policy_path is None) != (registry_base_path is None):
            fail()
        if repository_git_policy_path is not None:
            if repository_policy is None:
                fail()
            source = Path(repository_git_policy_path)
            if source.resolve(strict=True) != source:
                fail()
            content = read(source,32768)
            try:
                from codex_delivery.codex_delivery.repository_git_auth import parse_policy as parse_git
            except ModuleNotFoundError:
                from codex_delivery.repository_git_auth import parse_policy as parse_git
            git_policy, _ = parse_git(content)
            canonical = lambda value: json.dumps(value,sort_keys=True,separators=(',',':'))
            if (git_policy['service_uid'] != policy.host.uid
                    or git_policy['git_sha256'] != repository_policy['git_sha256']
                    or {canonical(item['policy']) for item in git_policy['repositories']}
                       != {canonical(item) for item in repository_policy['repositories']}):
                fail()
            software_assets.append((source, 'repository-git-policy.json', content))
        if registry_policy is not None:
            try:
                from codex_delivery.codex_delivery.oci_builder import _base, MAX_ARCHIVE
            except ModuleNotFoundError:
                from codex_delivery.oci_builder import _base, MAX_ARCHIVE
            source = Path(registry_base_path)
            if source.resolve(strict=True) != source:
                fail()
            content = read(source, MAX_ARCHIVE)
            _base(content, recipe, time.monotonic()+30)
            software_assets.append((source, 'registry-build/base.oci.tar', content))
        if repository_policy is not None and registry_policy is not None:
            scopes = [grant for grant in repository_policy['grants']
                      if grant['owner_ref'] == registry_policy['owner_ref']
                      and grant['project_ref'] == registry_policy['project_ref']
                      and grant['mode'] != 'work']
            expected_repos = registry_policy['repo_policies']
            if (not scopes or any({item['repo_ref']: item['policy_digest'] for item in grant['repositories']}
                                  != expected_repos for grant in scopes)
                    or repository_policy['target']['target_ref'] != registry_policy['binding']['target_ref']
                    or repository_policy['target']['permissions_digest'] != recipe.permissions_digest):
                fail()
        target = Path(destination)
        if (not target.is_absolute() or target.exists() or target.is_symlink()
                or target.parent.resolve(strict=True) != target.parent):
            fail()
        target.mkdir(mode=0o700)
        root = target/'runtime-assets'
        root.mkdir(mode=0o755)
        pins = {ref: (launcher, isolation) for ref, launcher, isolation in policy.runtime_pins}
        manifests = {}
        arch = None
        for runtime in policy.runtimes:
            source = Path(runtime_roots[runtime.runtime_ref])
            if source.resolve(strict=True) != source or not source.is_dir():
                fail()
            manifest_raw = read(source/'manifest.json',1024*1024)
            manifest = decode(manifest_raw)
            if (hashed(manifest_raw) != runtime.image or type(manifest.get('version')) is not int or manifest['version'] != 1
                    or manifest.get('kind') != runtime.kind or manifest.get('isolation_profile') != pins[runtime.runtime_ref][1]
                    or manifest.get('resource_profile','cgroup-v1') != policy.host.resource_profile
                    or manifest.get('host_arch') not in {'x86_64','aarch64'}
                    or type(manifest.get('files')) is not list or not 1 <= len(manifest['files']) <= MAX_ASSETS):
                fail()
            if arch is not None and arch != manifest['host_arch']:
                fail()
            arch = manifest['host_arch']
            declared, total = {}, 0
            for item in manifest['files']:
                if type(item) is not dict or set(item) != {'name','size_bytes','sha256'}:
                    fail()
                relative = name(item['name'])
                if (relative in declared or relative == 'manifest.json' or type(item['size_bytes']) is not int
                        or not 0 <= item['size_bytes'] <= MAX_BYTES or type(item['sha256']) is not str
                        or SHA.fullmatch(item['sha256']) is None):
                    fail()
                total += item['size_bytes']
                declared[relative] = item
            if total > MAX_BYTES or declared.get('bin/isolate',{}).get('sha256') != pins[runtime.runtime_ref][0]:
                fail()
            actual = set()
            for path in source.rglob('*'):
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    fail()
                actual.add(name(path.relative_to(source).as_posix()))
            if actual != set(declared)|{'manifest.json'}:
                fail()
            destination_root = root/'runtimes'/runtime.runtime_ref
            destination_root.mkdir(parents=True,mode=0o755)
            for relative,item in declared.items():
                original = source.joinpath(*PurePosixPath(relative).parts)
                mode = 0o555 if original.stat().st_mode & 0o111 else 0o444
                copy(original,destination_root/relative,item['sha256'],item['size_bytes'],mode)
            copy(source/'manifest.json',destination_root/'manifest.json',runtime.image,len(manifest_raw),0o444)
            manifests[runtime.runtime_ref] = runtime.image
        source_files = []
        for package in ('codex_delivery','codex_work_runtime'):
            origin = ADDON/package
            sources = list(origin.rglob('*.py'))
            if package == 'codex_delivery':
                sources.append(origin/'schema.sql')
            for path in sorted(sources):
                if ('__pycache__' in path.parts or path.is_symlink()
                        or any(part.startswith('.') for part in path.relative_to(origin).parts)):
                    fail()
                relative = name(path.relative_to(origin).as_posix())
                content = read(path,1024*1024)
                copy(path,target/package/relative,hashed(content),len(content),0o444)
                source_files.append(dict(name=package+'/'+relative,sha256=hashed(content),size_bytes=len(content)))
        for filename,mode in (('run.sh',0o555),('Dockerfile',0o644)):
            content = read(ADDON/filename,65536)
            copy(ADDON/filename,target/filename,hashed(content),len(content),mode)
            source_files.append(dict(name=filename,sha256=hashed(content),size_bytes=len(content)))
        if authority_raw is not None:
            copy(authority_source,root/'release-authority-policy.json',hashed(authority_raw),len(authority_raw),0o444)
            source_files.append(dict(name='runtime-assets/release-authority-policy.json',
                sha256=hashed(authority_raw),size_bytes=len(authority_raw)))
        for source, filename, content in software_assets:
            copy(source,root/filename,hashed(content),len(content),0o444)
            source_files.append(dict(name='runtime-assets/'+filename,sha256=hashed(content),size_bytes=len(content)))
        if registry_policy is not None:
            # A fixed public dependency is copied from its single canonical
            # source. No registration, config, test, key or instance is copied.
            dependency = ADDON.parent/'ha_artifact_registry/ha_artifact_registry'
            for filename in ('__init__.py','service.py','http.py','image.py','tls.py','main.py'):
                source = dependency/filename
                content = read(source,1024*1024)
                relative = 'ha_artifact_registry/ha_artifact_registry/'+filename
                copy(source,root/relative,hashed(content),len(content),0o444)
                source_files.append(dict(name='runtime-assets/'+relative,sha256=hashed(content),size_bytes=len(content)))
        source_raw = json.dumps(source_files,sort_keys=True,separators=(',',':')).encode()
        (root/'source-manifest.json').write_bytes(source_raw)
        (root/'source-manifest.json').chmod(0o444)
        (root/'execution-policy.json').write_bytes(raw)
        (root/'execution-policy.json').chmod(0o444)
        cli = None
        if (cli_path is None) != (cli_digest is None):
            fail()
        if cli_path is not None:
            if type(cli_digest) is not str or SHA.fullmatch(cli_digest) is None:
                fail()
            cli = Path(cli_path)
            size = cli.stat().st_size
            if not 1 <= size <= 256*1024**2:
                fail()
            binary = read(cli,256*1024**2)
            if (len(binary) < 20 or binary[:4] != b'\x7fELF' or binary[4:6] != b'\x02\x01'
                    or int.from_bytes(binary[18:20],'little') != {'x86_64':62,'aarch64':183}[arch]):
                fail()
            copy(cli,root/'fnos/bin/trim-cli',cli_digest,size,0o555)
        receipt = dict(version=1, policy_digest=policy_digest(policy.registry_policy()),
            execution_policy_sha256=hashed(raw), host_arch=arch, manifests=manifests,
            source_manifest_digest=hashed(source_raw), cli_digest=cli_digest,
            release_authority_policy_sha256=hashed(authority_raw) if authority_raw is not None else None,
            software_assets={filename:hashed(content) for _,filename,content in software_assets},
            credential_inputs_supported=False, deployed=False)
        # Runtime paths are an exact generated allowlist, so a late-added
        # registration/key/session file cannot enter COPY runtime-assets/.
        files = {path.relative_to(target).as_posix() for path in target.rglob('*') if path.is_file()}
        directories = set()
        for filename in files:
            for parent in PurePosixPath(filename).parents:
                if parent != PurePosixPath('.'):
                    directories.add(parent.as_posix())
        ignore = ['**']
        for item in sorted(files | directories):
            if item in directories:
                # Docker re-includes a directory's descendants implicitly.
                # Immediately exclude them again before exact child exceptions.
                ignore.extend(('!'+item+'/', item+'/**'))
            else:
                ignore.append('!'+item)
        (target/'.dockerignore').write_text('\n'.join(ignore)+'\n')
        (target/'staging-receipt.json').write_text(json.dumps(receipt,sort_keys=True,separators=(',',':')))
        (target/'staging-receipt.json').chmod(0o600)
        return receipt
    except StagingError:
        raise
    except Exception:
        fail()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy',required=True)
    parser.add_argument('--destination',required=True)
    parser.add_argument('--runtime',action='append',required=True,metavar='ID=ABSOLUTE_DIR')
    parser.add_argument('--cli')
    parser.add_argument('--cli-digest')
    parser.add_argument('--release-authority-policy',help='optional closed, nonsecret image-owned authority policy')
    parser.add_argument('--repository-policy',help='optional closed, nonsecret fixed repository policy')
    parser.add_argument('--registry-build-policy',help='optional closed, nonsecret fixed Registry build policy')
    parser.add_argument('--registry-base',help='exact offline OCI base required with Registry build policy')
    parser.add_argument('--repository-git-policy',help='optional closed nonsecret Git identity policy; requires repository policy')
    args = parser.parse_args()
    roots = {}
    for item in args.runtime:
        key, separator, value = item.partition('=')
        if not separator or key in roots:
            parser.error('unique runtime ID and directory required')
        roots[key] = value
    try:
        print(json.dumps(stage(args.policy,args.destination,roots,cli_path=args.cli,cli_digest=args.cli_digest,
                               authority_policy_path=args.release_authority_policy,
                               repository_policy_path=args.repository_policy,
                               registry_build_policy_path=args.registry_build_policy,
                               registry_base_path=args.registry_base,
                               repository_git_policy_path=args.repository_git_policy),
                         sort_keys=True,separators=(',',':')))
    except StagingError:
        print('{"status":"failed","code":"execution_staging_invalid"}',file=sys.stderr)
        raise SystemExit(1)
