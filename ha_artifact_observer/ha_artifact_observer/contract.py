"""Closed, typed identities, never a generic Docker request language."""
from dataclasses import dataclass,field
from datetime import datetime
import json
import re
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')
REF=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
SLUG=re.compile(r'[a-z0-9][a-z0-9_]{0,63}\Z')
HEX=re.compile(r'[a-f0-9]{64}\Z')
SHA=re.compile(r'sha256:[a-f0-9]{64}\Z')
REPOSITORY=re.compile(r'[a-z0-9][a-z0-9._-]*(?::[0-9]{1,5})?(?:/[a-z0-9][a-z0-9._-]*)+\Z')
TOKEN=re.compile(r'[A-Za-z0-9._~+-]{32,512}\Z')


class ObserverError(RuntimeError):
    def __init__(self,code):super().__init__(code);self.code=code


def fail(code):raise ObserverError(code)


def ref(value):
    if not isinstance(value,str) or REF.fullmatch(value) is None:fail('observer_reference_invalid')
    return value


def canonical(value):
    try:return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    except (ValueError,TypeError,RecursionError):fail('observer_json_invalid')


def decode(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:fail('observer_json_invalid')
            result[key]=value
        return result
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:fail('observer_json_invalid'))
    except (ValueError,UnicodeError,RecursionError):fail('observer_json_invalid')


@dataclass(frozen=True)
class Binding:
    target_ref:str
    instance_ref:str
    slug:str
    container_name:str
    def __post_init__(self):
        ref(self.target_ref);ref(self.instance_ref)
        if (not isinstance(self.slug,str) or not SLUG.fullmatch(self.slug)
                or self.container_name not in {'app_'+self.slug,'addon_'+self.slug}):fail('observer_binding_invalid')


@dataclass(frozen=True,repr=False)
class Principal:
    principal_ref:str
    token:str=field(repr=False)
    target_refs:frozenset[str]=field(default_factory=frozenset)
    def __post_init__(self):
        ref(self.principal_ref)
        if (not isinstance(self.token,str) or not TOKEN.fullmatch(self.token)
                or not isinstance(self.target_refs,frozenset)):fail('observer_principal_invalid')
        for item in self.target_refs:ref(item)


def stamp(now):
    if not isinstance(now,datetime) or now.tzinfo is None:fail('observer_clock_invalid')
    return now.astimezone(TZ).isoformat(timespec='microseconds')


def validate_fact(value):
    if (not isinstance(value,dict) or set(value)!={'container_id','docker_image_id','oci_repo_digests','running'}
            or not isinstance(value['container_id'],str) or not HEX.fullmatch(value['container_id'])
            or value['running'] is not True):fail('observer_fact_invalid')
    image=value['docker_image_id']
    if (not isinstance(image,dict) or set(image)!={'kind','digest'} or image['kind']!='docker_image_id'
            or not isinstance(image['digest'],str) or not SHA.fullmatch(image['digest'])):fail('observer_fact_invalid')
    digests=value['oci_repo_digests']
    if not isinstance(digests,list) or len(digests)>64:fail('observer_fact_invalid')
    seen=set()
    for item in digests:
        if (not isinstance(item,dict) or set(item)!={'kind','repository','digest'} or item['kind']!='oci_repo_digest'
                or not isinstance(item['repository'],str) or len(item['repository'])>440
                or not REPOSITORY.fullmatch(item['repository']) or not isinstance(item['digest'],str)
                or not SHA.fullmatch(item['digest'])):fail('observer_fact_invalid')
        identity=(item['repository'],item['digest'])
        if identity in seen:fail('observer_fact_invalid')
        seen.add(identity)
    return value
