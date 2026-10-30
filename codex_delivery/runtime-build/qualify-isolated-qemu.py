"""Fixed guest only: qualify native sandbox plus QEMU threads and Linux boots."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

source = Path('/opt/qemu-runtime')
manifest = json.loads((source / 'manifest.json').read_text())
for item in manifest['files']:
    assert hashlib.sha256((source / item['name']).read_bytes()).hexdigest() == item['sha256'].split(':')[1]
root = Path('/work/root')
root.mkdir(mode=0o700)
shutil.copytree(source, root / 'runtime')
(root / 'tmp').mkdir(mode=0o700)
(root / 'input.img').write_bytes(b'fixed-readonly-qualification-input\n')
(root / 'export.bin').write_bytes(b'')
output = root / 'output.img'
with output.open('xb') as target:
    target.truncate(32 * 1024 * 1024)
subprocess.run(['/usr/sbin/mke2fs', '-q', '-t', 'ext4', '-F', '-m', '0', str(output)], check=True)
(root / 'input.img').chmod(0o444)
for name in ('output.img', 'export.bin'):
    (root / name).chmod(0o600)
for path in (root / 'runtime').rglob('*'):
    if path.is_dir(): path.chmod(0o555)
    elif path.is_file(): path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
for path in (root / 'runtime', root / 'tmp', root): path.chmod(0o555)
command = [str(source / 'bin/isolate'), str(root), '--exec', str(64 * 1024 * 1024), '35', '/runtime/' + manifest['loader'],
    '--inhibit-cache', '--library-path', '/runtime/lib', '/runtime/' + manifest['qemu'],
    '-L', '/runtime/share/qemu', '-accel', 'tcg', '-machine', 'pc', '-smp', '2', '-m', '512',
    '-nodefaults', '-no-user-config', '-display', 'none', '-serial', 'stdio', '-monitor', 'none',
    '-no-reboot', '-nic', 'none', '-kernel', '/runtime/' + manifest['guest_kernel'],
    '-initrd', '/runtime/' + manifest['guest_initramfs'],
    '-append', 'console=ttyS0 rdinit=/init panic=-1 quiet',
    '-drive', 'file=/output.img,format=raw,if=virtio,cache=writeback']
print('ISOLATED_QEMU_ASSETS=' + json.dumps({'launcher_sha256': hashlib.sha256((source / 'bin/isolate').read_bytes()).hexdigest(),
    'runtime_manifest_sha256': hashlib.sha256((source / 'manifest.json').read_bytes()).hexdigest(), 'host_uid': os.getuid()}), flush=True)
for expected in (1, 2):
    started = time.monotonic()
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env={'LANG': 'C'}, timeout=45, text=True)
    print('ISOLATED_QEMU_BOOT=' + json.dumps({'boot': expected, 'seconds': round(time.monotonic() - started, 3),
        'return_code': result.returncode, 'output': result.stdout[-10000:]}), flush=True)
    assert result.returncode == 0 and 'POC_GUEST_DONE' in result.stdout
    records = [json.loads(line.split('=', 1)[1]) for line in result.stdout.splitlines() if line.startswith('POC_RESULT=')]
    assert len(records) == 1 and records[0]['counter'] == expected
    assert 'POC_TASK_STATUS=0' in result.stdout
for item in manifest['files']:
    assert hashlib.sha256((root / 'runtime' / item['name']).read_bytes()).hexdigest() == item['sha256'].split(':')[1]
assert (root / 'input.img').read_bytes() == b'fixed-readonly-qualification-input\n'
assert not any((root / 'tmp').iterdir())
print('ISOLATED_QEMU_QUALIFICATION_PASS', flush=True)
