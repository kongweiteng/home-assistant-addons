"""Copy only the signed Debian binaries and modules required by the fixed guest."""
from pathlib import Path
import re
import shutil
import subprocess

root = Path('/guest/root')
for name in ('bin', 'sbin', 'usr/bin', 'usr/lib', 'lib', 'lib64', 'etc', 'proc',
             'sys', 'dev', 'tmp', 'work', 'opt/poc'):
    (root / name).mkdir(parents=True, exist_ok=True)

def copy(source, target=None):
    source = Path(source)
    destination = root / (target or source.as_posix()).lstrip('/')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=True)

copy('/bin/busybox')
copy('/usr/bin/python3.11', '/usr/bin/python3')
shutil.copytree('/usr/lib/python3.11', root / 'usr/lib/python3.11',
    ignore=shutil.ignore_patterns('__pycache__', 'test', 'tkinter', 'idlelib'))
executables = ['/usr/bin/python3.11', *map(str, Path('/usr/lib/python3.11/lib-dynload').glob('*.so'))]
libraries = set()
for executable in executables:
    result = subprocess.run(['ldd', executable], capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        match = re.search(r'(/[^ ()]+)', line)
        if match:
            libraries.add(match.group(1))
for library in sorted(libraries):
    copy(library)

kernel_root = Path('/guest/kernel-package')
kernel = sorted((kernel_root / 'lib/modules').iterdir())[-1].name
subprocess.run(['depmod', '-b', str(kernel_root), kernel], check=True)
for driver in ('virtio_pci', 'virtio_blk', 'virtio_console', 'crc32c_generic', 'ext4'):
    result = subprocess.run(['modprobe', '-d', str(kernel_root), '-S', kernel, '--show-depends', driver],
        capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        if line.startswith('insmod '):
            module = Path(line.split()[1])
            copy(module, '/' + str(module.relative_to(kernel_root)))
for metadata in (kernel_root / 'lib/modules' / kernel).glob('modules.*'):
    if metadata.is_file():
        copy(metadata, '/' + str(metadata.relative_to(kernel_root)))
shutil.copy2(kernel_root / ('boot/vmlinuz-' + kernel), '/guest/kernel')
subprocess.run(['chroot', str(root), '/bin/busybox', '--install', '-s', '/bin'], check=True)
