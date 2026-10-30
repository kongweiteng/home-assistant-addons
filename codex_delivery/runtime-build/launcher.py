"""Run two bounded TCG-only boots using a disposable local 32 MiB disk."""
import json
import os
from pathlib import Path
import resource
import subprocess
import time

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
assert os.getuid() == 65534
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
assert int(status['CapEff'].strip(), 16) == 0
assert status['NoNewPrivs'].strip() == '1'
assert os.statvfs('/').f_flag & os.ST_RDONLY
quotas = {}
for label in ('cpu.max', 'memory.max', 'pids.max'):
    path = Path('/sys/fs/cgroup') / label
    if path.is_file():
        quotas[label] = path.read_text().strip()
if 'memory.max' in quotas:
    assert int(quotas['memory.max']) <= 1024 * 1024 * 1024
if 'pids.max' in quotas:
    assert int(quotas['pids.max']) <= 64
if 'cpu.max' in quotas:
    quota, period = map(int, quotas['cpu.max'].split())
    assert quota / period <= 2
work = Path('/work')
disk = work / 'task-disk.raw'
fd = os.open(disk, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'wb') as output:
    output.truncate(32 * 1024 * 1024)
subprocess.run(['mke2fs', '-q', '-t', 'ext4', '-F', '-m', '0', str(disk)], check=True)
command = ['qemu-system-x86_64', '-accel', 'tcg', '-machine', 'pc', '-smp', '2', '-m', '512',
    '-nodefaults', '-no-user-config', '-display', 'none', '-serial', 'stdio', '-monitor', 'none',
    '-no-reboot', '-nic', 'none', '-kernel', '/opt/ha-vm-poc/kernel',
    '-initrd', '/opt/ha-vm-poc/initramfs.gz',
    '-append', 'console=ttyS0 rdinit=/init panic=-1 quiet',
    '-drive', 'file=' + str(disk) + ',format=raw,if=virtio,cache=writeback']
print('POC_HOST=' + json.dumps({'uid': os.getuid(), 'accelerator': 'tcg',
    'cpus': 2, 'guest_memory_mib': 512, 'disk_bytes': disk.stat().st_size,
    'network': 'none', 'host_devices': [], 'cgroup_limits': quotas,
    'effective_caps': 0, 'no_new_privileges': True, 'root_readonly': True}), flush=True)
for expected in (1, 2):
    started = time.monotonic()
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=90, text=True)
    duration = round(time.monotonic() - started, 3)
    # This immutable payload never handles production inputs or secrets.
    if result.returncode or len(result.stdout) > 131072:
        print(result.stdout[-6000:], flush=True)
        raise RuntimeError('guest_failed')
    records = [json.loads(line.split('=', 1)[1]) for line in result.stdout.splitlines()
        if line.startswith('POC_RESULT=')]
    assert len(records) == 1 and records[0]['counter'] == expected, result.stdout[-6000:]
    assert 'POC_TASK_STATUS=0' in result.stdout, result.stdout[-6000:]
    memory_status = 137 if 'POC_MEMORY_STATUS=137' in result.stdout else 42
    assert memory_status == 137 or ('POC_MEMORY_STATUS=42' in result.stdout
        and 'POC_MEMORY_REJECTED=700MiB' in result.stdout), result.stdout[-6000:]
    assert 'POC_GUEST_DONE' in result.stdout, result.stdout[-6000:]
    print('POC_BOOT=' + json.dumps({'boot': expected, 'elapsed_seconds': duration,
        'result': records[0], 'guest_memory_child_status': memory_status,
        'memory_rejection': 'oom_killed' if memory_status == 137 else 'allocation_rejected',
        'qemu_return_code': result.returncode}), flush=True)
print('POC_PASS', flush=True)
