"""Fixed, non-user POC payload; never receives real files or credentials."""
import errno
import json
import os
from pathlib import Path
import sys

root = Path('/work')
ready_guest_uptime_seconds = float(Path('/proc/uptime').read_text().split()[0])
counter = root / 'counter.txt'
old = int(counter.read_text()) if counter.exists() else 0
fd = os.open(counter, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'w') as output:
    output.write(str(old + 1))
    output.flush()
    os.fsync(output.fileno())
capacity = os.statvfs(root)
disk_bytes = capacity.f_blocks * capacity.f_frsize
memory_kib = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:')))
assert 200_000 <= memory_kib <= 524_288
assert 1_000_000 <= disk_bytes <= 32 * 1024 * 1024
written = 0
enospc = False
path = root / 'quota-probe.bin'
try:
    with path.open('wb', buffering=0) as output:
        chunk = b'P' * (1024 * 1024)
        while written <= 33 * 1024 * 1024:
            written += output.write(chunk)
except OSError as error:
    if error.errno != errno.ENOSPC:
        raise
    enospc = True
finally:
    path.unlink(missing_ok=True)
assert enospc
interfaces = sorted(p.name for p in Path('/sys/class/net').iterdir())
assert interfaces == ['lo']
print('POC_RESULT=' + json.dumps({'counter': old + 1, 'python': sys.version.split()[0],
    'guest_kernel': os.uname().release,
    'ready_guest_uptime_seconds': ready_guest_uptime_seconds,
    'network_interfaces': interfaces,
    'memory_kib': memory_kib, 'disk_bytes': disk_bytes, 'disk_enospc': enospc,
    'uid': os.getuid(), 'code_execution': 6 * 7}), flush=True)
