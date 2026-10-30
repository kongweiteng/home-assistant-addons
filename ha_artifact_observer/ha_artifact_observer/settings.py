"""Bounded administrator options. Never log options, tokens or parse errors."""
from dataclasses import dataclass,field
import math
import os
from pathlib import Path
import re
import stat

from .contract import Binding,Principal,decode,fail
from .service import ObserverService

OPTIONS_PATH=Path('/data/options.json')
DOCKER_SOCKET='/var/run/docker.sock'
MAX_OPTIONS_BYTES=128*1024
DEFAULTS={'observation_enabled':False,'docker_socket_enabled':False,'docker_api_version':'v1.44',
          'timeout_seconds':3,'max_response_bytes':1024*1024,'bindings':[],'principals':[]}


@dataclass(frozen=True,repr=False)
class Settings:
    observation_enabled:bool=False
    docker_socket_enabled:bool=False
    docker_api_version:str='v1.44'
    timeout_seconds:float=3
    max_response_bytes:int=1024*1024
    bindings:tuple[Binding,...]=()
    principals:tuple[Principal,...]=field(default=(),repr=False)


def _closed(value,fields):
    if not isinstance(value,dict) or set(value)!=set(fields):fail('observer_options_invalid')
    return value


def parse_settings(raw):
    if not isinstance(raw,dict) or set(raw)-set(DEFAULTS):fail('observer_options_invalid')
    value=DEFAULTS|raw
    if (type(value['observation_enabled']) is not bool or type(value['docker_socket_enabled']) is not bool
            or not isinstance(value['docker_api_version'],str)
            or re.fullmatch(r'v1\.[0-9]{2}',value['docker_api_version']) is None
            or type(value['timeout_seconds']) not in {int,float} or not math.isfinite(value['timeout_seconds'])
            or not 0.05<=value['timeout_seconds']<=30 or type(value['max_response_bytes']) is not int
            or not 4096<=value['max_response_bytes']<=4*1024*1024
            or not isinstance(value['bindings'],list) or len(value['bindings'])>64
            or not isinstance(value['principals'],list) or len(value['principals'])>16):
        fail('observer_options_invalid')
    bindings=[];principals=[]
    for item in value['bindings']:
        item=_closed(item,{'target_ref','instance_ref','slug','container_name'})
        if any(not isinstance(field,str) for field in item.values()):fail('observer_options_invalid')
        bindings.append(Binding(**item))
    for item in value['principals']:
        item=_closed(item,{'principal_ref','token','target_refs'})
        targets=item['target_refs']
        if (not isinstance(targets,list) or not targets or len(targets)>64
                or any(not isinstance(target,str) for target in targets) or len(set(targets))!=len(targets)):
            fail('observer_options_invalid')
        principals.append(Principal(item['principal_ref'],item['token'],frozenset(targets)))
    # Registration is checked even while disabled; malformed admin configuration
    # must not silently become usable when the explicit flags change later.
    ObserverService(bindings=tuple(bindings),principals=tuple(principals))
    if value['observation_enabled'] and (not value['docker_socket_enabled'] or not bindings or not principals):
        fail('observer_enablement_incomplete')
    return Settings(value['observation_enabled'],value['docker_socket_enabled'],value['docker_api_version'],
                    value['timeout_seconds'],value['max_response_bytes'],tuple(bindings),tuple(principals))


def load_settings(path=OPTIONS_PATH):
    descriptor=None
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        metadata=os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0<=metadata.st_size<=MAX_OPTIONS_BYTES:
            fail('observer_options_unavailable')
        with os.fdopen(descriptor,'rb') as stream:
            descriptor=None
            raw=stream.read(MAX_OPTIONS_BYTES+1)
        if len(raw)>MAX_OPTIONS_BYTES:fail('observer_options_unavailable')
    except (OSError,ValueError):fail('observer_options_unavailable')
    finally:
        if descriptor is not None:os.close(descriptor)
    return parse_settings(decode(raw))


def compose(settings,*,inspector_factory=None):
    """Only this trusted composition can select the fixed production socket."""
    if (not isinstance(settings,Settings) or not isinstance(settings.bindings,tuple)
            or not isinstance(settings.principals,tuple)
            or any(not isinstance(item,Binding) for item in settings.bindings)
            or any(not isinstance(item,Principal) for item in settings.principals)):
        fail('observer_options_invalid')
    # Validate constructed Settings as strictly as parsed administrator options.
    checked=parse_settings({'observation_enabled':settings.observation_enabled,
        'docker_socket_enabled':settings.docker_socket_enabled,'docker_api_version':settings.docker_api_version,
        'timeout_seconds':settings.timeout_seconds,'max_response_bytes':settings.max_response_bytes,
        'bindings':[{'target_ref':b.target_ref,'instance_ref':b.instance_ref,'slug':b.slug,'container_name':b.container_name}
                    for b in settings.bindings],
        'principals':[{'principal_ref':p.principal_ref,'token':p.token,'target_refs':sorted(p.target_refs)}
                      for p in settings.principals]})
    inspector=None
    if checked.observation_enabled:
        if inspector_factory is None:
            from .docker import DockerInspector
            inspector_factory=DockerInspector
        inspector=inspector_factory(DOCKER_SOCKET,api_version=checked.docker_api_version,
                    timeout_seconds=checked.timeout_seconds,maxbytes=checked.max_response_bytes)
    return ObserverService(inspector=inspector,bindings=checked.bindings,principals=checked.principals,
                           enabled=checked.observation_enabled)
