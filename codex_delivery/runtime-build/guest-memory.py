"""Bounded deliberate OOM check inside a guest limited to 512 MiB."""
from pathlib import Path
import sys

Path('/proc/self/oom_score_adj').write_text('1000')
try:
    allocation = bytearray(700 * 1024 * 1024)
except MemoryError:
    print('POC_MEMORY_REJECTED=700MiB', flush=True)
    sys.exit(42)
for offset in range(0, len(allocation), 4096):
    allocation[offset] = 1
print('POC_MEMORY_UNEXPECTED_SUCCESS', len(allocation), flush=True)
