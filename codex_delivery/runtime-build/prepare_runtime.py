"""Create a credential-free, relocatable, fully hashed native QEMU asset tree."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

root = Path('/opt/qemu-runtime')
for directory in ('bin', 'lib', 'modules', 'share/qemu', 'guest', 'etc'):
    (root / directory).mkdir(parents=True, exist_ok=True)
qemu = Path('/usr/bin/qemu-system-x86_64')
shutil.copy2(qemu, root / 'bin/qemu-system-x86_64')
mke2fs = Path('/usr/sbin/mke2fs')
shutil.copy2(mke2fs, root / 'bin/mke2fs')
shutil.copy2('/etc/mke2fs.conf', root / 'etc/mke2fs.conf')
modules = [*Path('/usr/lib').glob('*/qemu/*.so'), *Path('/usr/lib/qemu').glob('*.so')]
libraries = set()
loader = None
for program in [qemu, mke2fs, *modules]:
    result = subprocess.run(['ldd', str(program)], capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        match = re.search(r'(/[^ ()]+)', line)
        if match:
            path = Path(match.group(1))
            libraries.add(path)
            if 'ld-linux' in path.name:
                loader = 'lib/' + path.name
for library in sorted(libraries):
    destination = root / 'lib' / library.name
    if destination.exists() and destination.read_bytes() != library.read_bytes():
        raise RuntimeError('library_name_collision')
    shutil.copy2(library, destination, follow_symlinks=True)
for module in modules:
    shutil.copy2(module, root / 'modules' / module.name)
for firmware in (Path('/usr/share/qemu'), Path('/usr/share/seabios')):
    if not firmware.is_dir():
        continue
    for source in firmware.rglob('*'):
        if source.is_file():
            target = root / 'share/qemu' / source.relative_to(firmware)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=True)
for source, target in (('kernel', 'kernel'), ('initramfs.gz', 'initramfs.gz')):
    shutil.copy2('/opt/ha-vm-poc/' + source, root / 'guest' / target)
assert loader
files = []
for path in sorted(root.rglob('*')):
    if path.is_file() and path != root / 'manifest.json':
        assert not path.is_symlink()
        files.append({'name': path.relative_to(root).as_posix(), 'size_bytes': path.stat().st_size,
            'sha256': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()})
kind = os.environ.get('RUNTIME_KIND', 'qemu-poc-v1')
assert kind in {'qemu-poc-v1', 'work-qemu-v1', 'browser-qemu-v1', 'preview-qemu-v1'}
profile = subprocess.check_output([str(root / 'bin/isolate'), '--profile'], text=True).strip()
assert profile in {'landlock-v1', 'readonly-jail-v1'}
manifest = {'version': 1, 'kind': kind, 'guest_protocol': {'qemu-poc-v1': 0, 'work-qemu-v1': 1, 'browser-qemu-v1': 2, 'preview-qemu-v1':4}[kind],
    'isolation_profile': profile,
    'host_arch': platform.machine(), 'loader': loader, 'qemu': 'bin/qemu-system-x86_64', 'mke2fs': 'bin/mke2fs',
    'guest_kernel': 'guest/kernel', 'guest_initramfs': 'guest/initramfs.gz', 'files': files}
if kind in {'browser-qemu-v1', 'preview-qemu-v1'} or os.environ.get('RESOURCE_PROFILE') == 'native-resource-v1':
    resource_profile = subprocess.check_output([str(root / 'bin/isolate'), '--resource-profile'], text=True).strip()
    assert resource_profile in {'cgroup-v1', 'native-resource-v1'}
    manifest['resource_profile'] = resource_profile
document = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
(root / 'manifest.json').write_bytes(document)
print('RUNTIME_ASSET_SHA256=' + hashlib.sha256(document).hexdigest(), flush=True)
