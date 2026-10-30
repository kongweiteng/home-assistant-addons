"""Private fixed-policy startup. No command-line URLs, secrets or shell."""
import base64
import fcntl
import ipaddress
import os
from pathlib import Path
import signal
import socket
import struct
import threading

from .http import make_server
from .image import decode,fail
from .service import Binding,RegistryStore,read

POLICY='/data/registry-policy.json'


def compose(policy):
    expected={'version','enabled','docker_bridge_host','public_key','writer_token','reader_token','bindings','grants'}
    if not isinstance(policy,dict) or set(policy)!=expected or type(policy['version']) is not int or policy['version']!=1:
        fail('registry_policy_invalid')
    try:
        bridge=ipaddress.ip_address(policy['docker_bridge_host'])
        public_key=base64.b64decode(policy['public_key'],validate=True)
    except (ValueError,TypeError):fail('registry_policy_invalid')
    # RFC1918 address of the exact Docker host bridge, measured by deployment.
    # Never a wildcard, LAN-selected hostname, loopback or public interface.
    ranges=[ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')]
    if bridge.version!=4 or not any(bridge in network for network in ranges):fail('registry_policy_invalid')
    if (not isinstance(policy['bindings'],list) or not isinstance(policy['grants'],list)
            or any(not isinstance(item,dict) or set(item)!={'target_ref','instance_ref','slug','repository','architecture',
                'recipe_digest','permissions_digest','qualification_digest'}
                   for item in policy['bindings'])):fail('registry_policy_invalid')
    store=RegistryStore('/data/registry',public_key=public_key,bindings=tuple(Binding(**item) for item in policy['bindings']),
        grants=policy['grants'],writer_token=policy['writer_token'],reader_token=policy['reader_token'],enabled=policy['enabled'])
    return store,str(bridge)


def main():
    servers=[];started=[];threads=[];stopped=threading.Event()
    try:
        if os.getuid()!=0 or os.geteuid()!=0:fail('registry_uid_invalid')
        store,bridge=compose(decode(read(POLICY,128*1024)))
        signal.signal(signal.SIGTERM,lambda *_:stopped.set());signal.signal(signal.SIGINT,lambda *_:stopped.set())
        if store.enabled:
            verify_bridge(bridge)
            from .tls import ephemeral_context
            servers.append(make_server(('127.0.0.1',5079),store,tls_context=ephemeral_context()))
            servers.append(make_server((bridge,5080),store,administrative=True))
            for server in servers:
                thread=threading.Thread(target=server.serve_forever,daemon=True)
                thread.start();threads.append(thread);started.append(server)
            print('artifact registry candidate ready',flush=True)
        else:print('artifact registry candidate disabled',flush=True)
        stopped.wait()
        return 0
    except Exception:
        print('artifact registry startup unavailable',flush=True);return 1
    finally:
        for server in started:server.shutdown()
        for server in servers:server.server_close()
        for thread in threads:thread.join(timeout=2)


def verify_bridge(expected):
    """Only the fixed Supervisor bridge interface, not another private/LAN IP."""
    if not Path('/sys/class/net/hassio/bridge').is_dir():fail('registry_bridge_unavailable')
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as channel:
            result=fcntl.ioctl(channel.fileno(),0x8915,struct.pack('256s',b'hassio'))
        actual=socket.inet_ntoa(result[20:24])
    except OSError:fail('registry_bridge_unavailable')
    if actual!=expected:fail('registry_bridge_changed')


if __name__=='__main__':raise SystemExit(main())
