"""Fixed trusted public-fetch worker. Only the owner process writes its stdin.

It is not an authorization endpoint or an interpreter for caller code. Parent
pinning and hard limits precede importing the HTTP broker or reading requests.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import resource
import signal
import sys


def _boundary(expected_parent: int, cpu_count: int) -> None:
    if expected_parent <= 0 or os.getppid() != expected_parent or cpu_count not in {1, 2}:
        raise RuntimeError('parent')
    libc = ctypes.CDLL(None)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != expected_parent:
        raise RuntimeError('parent')
    if libc.prctl(38, 1, 0, 0, 0):
        raise RuntimeError('identity')
    for kind, amount in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_AS, 256*1024**2),
            (resource.RLIMIT_NPROC, 4), (resource.RLIMIT_CPU, 30),
            (resource.RLIMIT_NOFILE, 32), (resource.RLIMIT_FSIZE, 8*1024**2)):
        resource.setrlimit(kind, (amount, amount))
    cpus = sorted(os.sched_getaffinity(0))[:cpu_count]
    if not cpus:
        raise RuntimeError('cpu')
    os.sched_setaffinity(0, cpus)
    if os.sched_getaffinity(0) != set(cpus) or os.getppid() != expected_parent:
        raise RuntimeError('boundary')


def main() -> None:
    if len(sys.argv) != 3 or not sys.argv[1].isdigit() or not sys.argv[2].isdigit():
        raise RuntimeError('arguments')
    _boundary(int(sys.argv[1]), int(sys.argv[2]))
    # -I ignores cwd, PYTHONPATH and user site packages. This fixed image-owned
    # file selects only its own package directory, never a caller module.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import base64
    import datetime as dt
    import json
    import math
    import re
    import struct
    import time
    from codex_work_runtime.errors import WorkRuntimeError
    from codex_work_runtime.public_browser import PublicBrowserGrant, PublicFetchBroker, _url, SHANGHAI

    maximum = 8*1024**2
    total_read = 0
    total_written = 0

    def read_exact(size):
        data = bytearray()
        while len(data) < size:
            value = os.read(0, size-len(data))
            if not value:
                raise EOFError
            data.extend(value)
        return bytes(data)

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate')
            value[key] = item
        return value

    def receive():
        nonlocal total_read
        size = struct.unpack('!I', read_exact(4))[0]
        if not 1 <= size <= maximum:
            raise ValueError('frame')
        total_read += size+4
        if total_read > 512*1024:
            raise ValueError('input')
        value = json.loads(read_exact(size), object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
        if not isinstance(value, dict):
            raise ValueError('object')
        return value

    def send(value):
        nonlocal total_written
        raw = json.dumps(value, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
        total_written += len(raw)+4
        if len(raw) > maximum or total_written > 32*1024**2:
            raise ValueError('output')
        view = memoryview(struct.pack('!I', len(raw))+raw)
        while view:
            count = os.write(1, view)
            if count <= 0:
                raise RuntimeError('output')
            view = view[count:]

    config = receive()
    if set(config) != {'version', 'grant'} or type(config['version']) is not int or config['version'] != 1:
        raise ValueError('config')
    value = config['grant']
    if (not isinstance(value, dict) or set(value) != {'urls', 'expires_at', 'monotonic_deadline', 'grant_ref', 'input_digest'}
            or not isinstance(value['urls'], list) or not 1 <= len(value['urls']) <= 64
            or any(not isinstance(url, str) for url in value['urls']) or sum(map(len, value['urls'])) > 32768
            or len(set(value['urls'])) != len(value['urls'])
            or type(value['monotonic_deadline']) not in {int, float} or not math.isfinite(value['monotonic_deadline'])
            or not isinstance(value['grant_ref'], str) or not re.fullmatch(r'BG-[a-f0-9]{32}', value['grant_ref'])
            or not isinstance(value['input_digest'], str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', value['input_digest'])):
        raise ValueError('grant')
    expires = dt.datetime.fromisoformat(value['expires_at'])
    instant = dt.datetime.now(SHANGHAI)
    if (expires.utcoffset() != dt.timedelta(hours=8) or expires.isoformat() != value['expires_at']
            or not instant < expires <= instant+dt.timedelta(seconds=120)
            or not time.monotonic() < value['monotonic_deadline'] <= time.monotonic()+120):
        raise ValueError('expiry')
    # Authority was authenticated by the owner before opening this private pipe.
    # The subprocess merely preserves that exact grant; it cannot issue one.
    grant = PublicBrowserGrant(tuple(_url(url) for url in value['urls']), expires,
        value['monotonic_deadline'], value['grant_ref'], value['input_digest'])
    # The pidfd owner imposes an independent <=8s deadline on every fetch.
    # Do not create Timer threads under shared-UID RLIMIT_NPROC accounting.
    broker = PublicFetchBroker(grant, owner_deadline=True)
    sequence = 0
    while True:
        message = receive()
        sequence += 1
        if (set(message) != {'id', 'url', 'method'} or type(message['id']) is not int
                or message['id'] != sequence or sequence > 64
                or not isinstance(message['url'], str) or len(message['url']) > 4096
                or message['method'] not in {'GET', 'HEAD'}):
            raise ValueError('request')
        try:
            grant.require(message['url'])
            response = broker.fetch(message['url'], message['method'])
            grant.require(message['url'])
            send({'id': sequence, 'url': response.url, 'status': response.status,
                'content_type': response.content_type, 'location': response.location,
                'body': base64.b64encode(response.body).decode()})
        except WorkRuntimeError as error:
            code = error.code if re.fullmatch(r'[a-z][a-z0-9_]{0,79}', error.code) else 'browser_fetch_failed'
            send({'id': sequence, 'error': code})


if __name__ == '__main__':
    try:
        main()
    except EOFError:
        raise SystemExit(0)
    except Exception:
        raise SystemExit(1)
