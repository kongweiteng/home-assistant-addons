"""Independent bounded OCI image graph verifier, with no host extraction."""
from dataclasses import dataclass
import gzip
import hashlib
from io import BytesIO
import json
from pathlib import PurePosixPath
import re
import tarfile
import time

MANIFEST='application/vnd.oci.image.manifest.v1+json'
CONFIG='application/vnd.oci.image.config.v1+json'
LAYER='application/vnd.oci.image.layer.v1.tar'
SHA=re.compile(r'sha256:[a-f0-9]{64}\Z')
MAX_ARCHIVE=256*1024*1024
MAX_EXPANDED=512*1024*1024


class RegistryError(RuntimeError):
    def __init__(self,code):super().__init__(code);self.code=code


def fail(code='registry_evidence_invalid'):raise RegistryError(code)
def sha(raw):return 'sha256:'+hashlib.sha256(raw).hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def decode(raw):
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:fail()
            value[key]=item
        return value
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:fail())
    except (ValueError,TypeError,UnicodeError,RecursionError):fail()


def deadline_check(deadline):
    if time.monotonic()>=deadline:fail('registry_deadline')


def descriptor(value,kinds,blobs):
    if (not isinstance(value,dict) or set(value)-{'mediaType','digest','size'}
            or value.get('mediaType') not in kinds or not isinstance(value.get('digest'),str)
            or not SHA.fullmatch(value['digest']) or type(value.get('size')) is not int):fail()
    raw=blobs.get(value['digest'])
    if not isinstance(raw,bytes) or len(raw)!=value['size'] or sha(raw)!=value['digest']:fail()
    return raw


@dataclass(frozen=True)
class ImageGraph:
    manifest:bytes
    config:bytes
    blobs:dict
    graph_digest:str


def verify_graph(manifest_raw,blobs,*,architecture,manifest_digest,config_digest,deadline):
    deadline_check(deadline)
    if (not isinstance(manifest_raw,bytes) or len(manifest_raw)>1024*1024
            or sha(manifest_raw)!=manifest_digest or not isinstance(blobs,dict) or len(blobs)>62
            or sum(len(raw) for raw in blobs.values())>MAX_ARCHIVE):fail()
    manifest=decode(manifest_raw)
    if (not isinstance(manifest,dict) or set(manifest)!={'schemaVersion','mediaType','config','layers'}
            or type(manifest['schemaVersion']) is not int or manifest['schemaVersion']!=2
            or manifest['mediaType']!=MANIFEST or not isinstance(manifest['layers'],list)
            or not 1<=len(manifest['layers'])<=61):fail()
    config=descriptor(manifest['config'],{CONFIG},blobs)
    if sha(config)!=config_digest or len(config)>1024*1024:fail()
    metadata=decode(config)
    if not isinstance(metadata,dict) or metadata.get('os')!='linux' or metadata.get('architecture')!=architecture:fail()
    used={config_digest};diffs=[];expanded=0
    for item in manifest['layers']:
        raw=descriptor(item,{LAYER,LAYER+'+gzip'},blobs);used.add(item['digest'])
        compressed=item['mediaType'].endswith('+gzip')
        if compressed!=raw.startswith(b'\x1f\x8b'):fail()
        checksum=hashlib.sha256()
        try:
            stream=gzip.GzipFile(fileobj=BytesIO(raw)) if compressed else BytesIO(raw)
            with stream:
                while True:
                    deadline_check(deadline)
                    part=stream.read(min(65536,MAX_EXPANDED-expanded+1))
                    if not part:break
                    expanded+=len(part)
                    if expanded>MAX_EXPANDED:fail('registry_expansion_limit')
                    checksum.update(part)
        except (OSError,ValueError,EOFError):fail()
        diffs.append('sha256:'+checksum.hexdigest())
    if set(blobs)!=used or metadata.get('rootfs')!={'type':'layers','diff_ids':diffs}:fail()
    inventory={'manifest':{'sha256':sha(manifest_raw),'size':len(manifest_raw)},
               'blobs':{key:{'sha256':sha(raw),'size':len(raw)} for key,raw in sorted(blobs.items())}}
    return ImageGraph(manifest_raw,config,dict(blobs),sha(canonical(inventory)))


def verify_archive(raw,*,artifact_digest,architecture,manifest_digest,config_digest,deadline):
    if not isinstance(raw,bytes) or not 1024<=len(raw)<=MAX_ARCHIVE or sha(raw)!=artifact_digest:fail()
    files={};total=0
    try:
        with tarfile.open(fileobj=BytesIO(raw),mode='r:') as archive:
            for item in archive:
                deadline_check(deadline);name=item.name
                if item.isdir():name=name.rstrip('/')
                if (not name or len(name)>240 or str(PurePosixPath(name))!=name or name.startswith('/')
                        or '\\' in name or '..' in name.split('/') or name in files or len(files)>=128
                        or item.size<0 or item.size>MAX_ARCHIVE or item.pax_headers or item.issparse()
                        or not (item.isfile() or item.isdir())):fail()
                total+=item.size
                if total>MAX_ARCHIVE:fail()
                files[name]=None if item.isdir() else archive.extractfile(item).read(item.size+1)
                if files[name] is not None and len(files[name])!=item.size:fail()
    except (tarfile.TarError,OSError,ValueError,EOFError):fail()
    if decode(files.get('oci-layout'))!={'imageLayoutVersion':'1.0.0'}:fail()
    index=decode(files.get('index.json'))
    if (not isinstance(index,dict) or set(index)!={'schemaVersion','manifests'}
            or type(index['schemaVersion']) is not int or index['schemaVersion']!=2
            or not isinstance(index['manifests'],list) or len(index['manifests'])!=1):fail()
    blobs={}
    for name,data in files.items():
        if data is None:continue
        if name.startswith('blobs/sha256/'):
            hashed='sha256:'+name[len('blobs/sha256/'):]
            if not SHA.fullmatch(hashed) or sha(data)!=hashed:fail()
            blobs[hashed]=data
        elif name not in {'oci-layout','index.json','manifest.json'}:fail()
    manifest=descriptor(index['manifests'][0],{MANIFEST},blobs)
    if sha(manifest)!=manifest_digest:fail()
    del blobs[manifest_digest]
    graph=verify_graph(manifest,blobs,architecture=architecture,manifest_digest=manifest_digest,
                       config_digest=config_digest,deadline=deadline)
    if 'manifest.json' in files:
        value=decode(files['manifest.json']);document=decode(manifest)
        expected=[{'Config':'blobs/sha256/'+config_digest[7:],'RepoTags':[],
                   'Layers':['blobs/sha256/'+item['digest'][7:] for item in document['layers']]}]
        if value!=expected:fail()
    return graph
