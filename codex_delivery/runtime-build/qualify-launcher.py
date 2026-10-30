"""No credentials: deliberate same-UID sentinel and FD escape regression probe."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

assert os.getuid() == 65534 and Path('/outside-sentinel').is_file()
root = Path('/work/root')
for name in ('runtime', 'tmp'):
    (root / name).mkdir(parents=True, mode=0o700)
for name in ('runtime/readonly.txt', 'input.img', 'output.img', 'export.bin'):
    path = root / name
    path.write_bytes(b'qualification-only\n')
    path.chmod(0o600)
for path in (root / 'runtime/readonly.txt', root / 'input.img'): path.chmod(0o444)
for path in (root / 'runtime', root / 'tmp', root): path.chmod(0o555)
directory_fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
try:
    result = subprocess.run(['/isolate', str(root), '--self-test', str(os.getpid())],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={'LANG': 'C', 'TEST_CONTROL_SENTINEL': 'nonsecret-fixture', 'LD_PRELOAD': '/nonexistent'},
        pass_fds=(directory_fd,), timeout=15, text=True)
finally:
    os.close(directory_fd)
print(json.dumps({'host_uid': os.getuid(), 'launcher_sha256': hashlib.sha256(Path('/isolate').read_bytes()).hexdigest(),
    'return_code': result.returncode, 'stdout': result.stdout[-8000:], 'stderr': result.stderr[-2000:]}), flush=True)
assert result.returncode == 0
report = json.loads(result.stdout)
assert report['version'] == 1 and report['caps'] == 0
profile = subprocess.check_output(['/isolate', '--profile'], text=True).strip()
assert report['isolation_profile'] == profile
assert report['landlock'] is (profile == 'landlock-v1')
assert all(value is True for key, value in report.items() if key not in {'version', 'caps', 'landlock', 'isolation_profile'})
print('LAUNCHER_QUALIFICATION_PASS', flush=True)
