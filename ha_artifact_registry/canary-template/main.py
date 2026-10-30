"""Disposable A/B/C release target; only bounded read-only health HTTP."""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
import stat
from pathlib import Path

MARKER=b'codex-independent-canary-data-v1\n'
FAULT_MARKER=b'codex-independent-canary-data-C-fault\n'
VARIANTS={'A':('0.1.0',False),'B':('0.2.0',False),'C':('0.3.0',True)}


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def decode(content):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate canary JSON key')
            result[key]=value
        return result
    def reject(value):raise ValueError('nonfinite canary JSON')
    return json.loads(content,object_pairs_hook=pairs,parse_constant=reject)


def digest(content):return 'sha256:'+hashlib.sha256(content).hexdigest()


def read_private(path,limit):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):raise RuntimeError('canary_data_invalid')
        content=source.read(limit+1)
    if len(content)>limit:raise RuntimeError('canary_data_invalid')
    return content


def write_marker(root,content):
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=root/'canary-marker.txt'
    if path.is_symlink():raise RuntimeError('canary_data_invalid')
    temporary=root/'.canary-marker-new'
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as target:
        target.write(content);target.flush();os.fsync(target.fileno())
    os.replace(temporary,path)
    directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)


def load_variant(path):
    value=decode(read_private(path,1024))
    if (not isinstance(value,dict) or set(value)!={'variant','service_version','expected_health_failure'}
            or value.get('variant') not in VARIANTS
            or (value['service_version'],value['expected_health_failure'])!=VARIANTS[value['variant']]
            or type(value['expected_health_failure']) is not bool):raise RuntimeError('canary_variant_invalid')
    return value


def source_digest(variant_path):
    # The source-to-image bridge injects the exact original sealed manifest.
    # Runtime image identity is independently observed; this value alone is not proof.
    value=os.environ.get('CODEX_RELEASE_SOURCE_DIGEST','')
    if len(value)!=71 or not value.startswith('sha256:') or any(c not in '0123456789abcdef' for c in value[7:]):
        raise RuntimeError('canary_release_metadata_invalid')
    return value


def prepare(root,variant):
    root=Path(root)
    if not (root/'canary-marker.txt').exists():write_marker(root,MARKER)
    actual=read_private(root/'canary-marker.txt',1024)
    if actual not in {MARKER,FAULT_MARKER}:raise RuntimeError('canary_data_invalid')
    if variant['variant']=='C' and actual!=FAULT_MARKER:write_marker(root,FAULT_MARKER)
    # A/B never repair C data. Only actual restore can return the marker to B.


def health(root,variant_path):
    variant=load_variant(variant_path)
    if os.environ.get('CODEX_RELEASE_VERSION')!=variant['service_version']:
        raise RuntimeError('canary_release_metadata_invalid')
    marker=read_private(Path(root)/'canary-marker.txt',1024)
    options_path=Path(root)/'options.json'
    options=decode(read_private(options_path,16384)) if options_path.exists() else {}
    if not isinstance(options,dict):raise RuntimeError('canary_options_invalid')
    expected=variant['expected_health_failure']
    healthy=not expected and marker==MARKER
    return {'version':1,'variant':variant['variant'],'service_version':variant['service_version'],
            'source_digest':source_digest(variant_path),'marker_sha256':digest(marker),
            'config_digest':digest(canonical(options)),'healthy':healthy,
            'failure_code':'canary_expected_health_failure' if expected else ('' if healthy else 'canary_marker_mismatch')}


def create_server(host,port,*,data_root,variant_path):
    root=Path(data_root);variant_path=Path(variant_path)
    prepare(root,load_variant(variant_path))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path!='/healthz':self.send_error(404);return
            try:body=canonical(health(root,variant_path));code=200
            except Exception:body=b'{"error":"canary_observation_unavailable"}';code=503
            self.send_response(code);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
            self.end_headers();self.wfile.write(body)
    return ThreadingHTTPServer((host,port),Handler)


if __name__=='__main__':
    create_server('0.0.0.0',8769,data_root=Path('/data'),
                  variant_path=Path(__file__).with_name('canary-variant.json')).serve_forever()
