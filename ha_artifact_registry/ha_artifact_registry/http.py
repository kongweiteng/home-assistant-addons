"""Closed Distribution reads and authenticated exact-grant publication."""
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import ipaddress
import math
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from .image import (CONFIG,MANIFEST,MAX_ARCHIVE,RegistryError,SHA,canonical,decode,
                    deadline_check,fail,sha,verify_graph)
from .service import REF,REPOSITORY,RegistryStore


class _Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,*args,**kwargs):
        self.capacity=threading.BoundedSemaphore(2)
        super().__init__(*args,**kwargs)
    def process_request(self,request,address):
        if not self.capacity.acquire(blocking=False):self.shutdown_request(request);return
        try:super().process_request(request,address)
        except Exception:self.capacity.release();raise
    def process_request_thread(self,request,address):
        try:
            if self.tls_context is not None:
                request.settimeout(3)
                request=self.tls_context.wrap_socket(request,server_side=True,do_handshake_on_connect=False)
                request.do_handshake()
            super().process_request_thread(request,address)
        except Exception:self.shutdown_request(request)
        finally:self.capacity.release()
    def handle_error(self,request,address):pass


class _HeaderBudget:
    def __init__(self,stream):self.stream=stream;self.remaining=16384;self.complete=False
    def readline(self,limit=-1):
        raw=self.stream.readline(min(4097,limit) if limit>=0 else 4097)
        if not self.complete:
            self.remaining-=len(raw)
            if len(raw)>4096 or self.remaining<0:raise http.client.LineTooLong('registry header budget')
            if raw in {b'\r\n',b'\n'}:self.complete=True
        return raw
    def __getattr__(self,name):return getattr(self.stream,name)


def make_server(address,store,*,administrative=False,host_authority=None,tls_context=None):
    if (not isinstance(store,RegistryStore) or type(administrative) is not bool
            or tls_context is not None and not isinstance(tls_context,ssl.SSLContext)):fail('registry_config_invalid')
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def setup(self):
            super().setup();self.connection.settimeout(30)
            self.rfile=_HeaderBudget(self.rfile)
            self.timer=None;self.arm(5)
        def arm(self,seconds):
            if self.timer is not None:self.timer.cancel()
            def expire():
                try:self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            self.timer=threading.Timer(seconds,expire);self.timer.daemon=True;self.timer.start()
        def parse_request(self):
            accepted=super().parse_request()
            if accepted:self.arm(120)
            return accepted
        def finish(self):
            try:super().finish()
            finally:
                if self.timer is not None:self.timer.cancel()
        def guard(self,*,body=False):
            expected=host_authority or str(self.server.server_address[0])+':'+str(self.server.server_address[1])
            hosts=self.headers.get_all('Host',[]);auth=self.headers.get_all('Authorization',[])
            lengths=self.headers.get_all('Content-Length',[]);transfer=self.headers.get_all('Transfer-Encoding',[])
            if (self.request_version!='HTTP/1.1' or hosts!=[expected] or len(auth)>1
                    or len(lengths)>1 or transfer or len(self.headers)>32
                    or len(self.headers.get_all('Content-Type',[]))>1
                    or not body and lengths not in ([],['0'])):fail('registry_protocol_invalid')
            if self.headers.get_all('Range',[]):fail('registry_range_unsupported')
        def auth(self,role):
            values=self.headers.get_all('Authorization',[])
            store.authenticate(values[0] if len(values)==1 else None,role)
        def response(self,status,raw,kind='application/json',hashed=None,head=False):
            self.send_response(status);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(raw)));self.send_header('Connection','close')
            self.send_header('Docker-Distribution-Api-Version','registry/2.0')
            if hashed:self.send_header('Docker-Content-Digest',hashed)
            self.end_headers();self.close_connection=True
            if not head:self.wfile.write(raw)
        def error(self,error):
            code=error.code if isinstance(error,RegistryError) else 'registry_unavailable'
            status=(401 if code=='registry_not_authorized' else 503 if code in {'registry_disabled','registry_unavailable'}
                    else 400 if code=='registry_protocol_invalid' else 416 if code=='registry_range_unsupported' else 404)
            self.response(status,canonical({'errors':[{'code':code}]}),head=self.command=='HEAD')
        def get(self,head=False):
            try:
                self.guard()
                if administrative:self.auth('reader')
                if not store.enabled:fail('registry_disabled')
                if self.path=='/v2/':return self.response(200,b'{}',head=head)
                if administrative and self.path.startswith('/internal/v1/grants/'):
                    ref=self.path.removeprefix('/internal/v1/grants/')
                    if not REF.fullmatch(ref):fail('registry_grant_denied')
                    return self.response(200,canonical(store.registration(ref)),head=head)
                if administrative and self.path.startswith('/internal/v1/publications/'):
                    ref=self.path.removeprefix('/internal/v1/publications/')
                    if not REF.fullmatch(ref):fail('registry_grant_denied')
                    return self.response(200,canonical(store.status(ref)),head=head)
                match=re.fullmatch(r'/v2/(codex-work/(?:amd64|aarch64)-canary)/(manifests|blobs)/([^/?%]+)',self.path)
                if match is None:fail('registry_route_denied')
                repository,kind,reference=match.groups()
                if kind=='manifests':
                    grant,raw=store.manifest(repository,reference)
                    return self.response(200,raw,MANIFEST,grant['manifest_digest'],head)
                raw=store.blob(repository,reference)
                return self.response(200,raw,'application/octet-stream',sha(raw),head)
            except Exception as error:self.error(error)
        def do_GET(self):self.get()
        def do_HEAD(self):self.get(head=True)
        def do_POST(self):
            try:
                self.guard(body=True)
                if not administrative:fail('registry_route_denied')
                self.auth('writer')
                registration=self.path=='/internal/v1/grants'
                match=re.fullmatch(r'/internal/v1/publications/([A-Za-z0-9][A-Za-z0-9._-]{0,95})',self.path)
                if match is None and not registration:fail('registry_route_denied')
                lengths=self.headers.get_all('Content-Length',[])
                low,high=(2,65536) if registration else (1024,MAX_ARCHIVE)
                kind='application/json' if registration else 'application/x-tar'
                if (len(lengths)!=1 or not lengths[0].isdigit() or not low<=int(lengths[0])<=high
                        or self.headers.get_all('Transfer-Encoding',[]) or self.headers.get('Content-Type')!=kind):
                    fail('registry_body_invalid')
                # Reject invalid/expired scope before reading its large artifact.
                if not registration:store.grant(match[1],write_access=True)
                deadline=time.monotonic()+110;remaining=int(lengths[0]);chunks=[]
                while remaining:
                    deadline_check(deadline);self.connection.settimeout(max(.001,deadline-time.monotonic()))
                    part=self.rfile.read(min(65536,remaining))
                    if not part:fail('registry_body_incomplete')
                    chunks.append(part);remaining-=len(part)
                raw=b''.join(chunks)
                result=(store.register(decode(raw),self.headers['Authorization'],deadline=deadline) if registration
                        else store.publish(match[1],raw,self.headers['Authorization'],deadline=deadline))
                self.response(200,canonical(result))
            except Exception as error:self.error(error)
        def do_PUT(self):self.error(RegistryError('registry_route_denied'))
        def do_DELETE(self):self.error(RegistryError('registry_route_denied'))
        def do_PATCH(self):self.error(RegistryError('registry_route_denied'))
    server=_Server(address,Handler);server.tls_context=tls_context
    return server


class _HTTP:
    """One exact administrator origin, no proxy, redirect or automatic retry."""
    def __init__(self,origin,token,*,timeout=120):
        value=urlsplit(origin)
        try:address=ipaddress.ip_address(value.hostname)
        except ValueError:fail('registry_origin_invalid')
        if (value.scheme!='http' or value.username or value.password or value.path not in {'','/'}
                or value.query or value.fragment or not value.port or address.version!=4
                or not (address.is_private or address.is_loopback) or address.is_unspecified or address.is_multicast
                or not isinstance(token,str) or not 32<=len(token)<=256 or not token.isascii()
                or any(c.isspace() for c in token)
                or type(timeout) not in {int,float} or not math.isfinite(timeout) or not .1<=timeout<=120):
            fail('registry_origin_invalid')
        self.host,self.port,self.token,self.timeout=value.hostname,value.port,token,float(timeout)

    def call(self,path,*,deadline,limit,body=None,content_type='application/x-tar'):
        deadline=min(deadline,time.monotonic()+self.timeout);deadline_check(deadline)
        connection=http.client.HTTPConnection(self.host,self.port,timeout=deadline-time.monotonic());timer=None
        try:
            connection.connect();deadline_check(deadline);watched=connection.sock
            def expire():
                try:watched.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            timer=threading.Timer(max(.001,deadline-time.monotonic()),expire);timer.daemon=True;timer.start()
            headers={'Authorization':'Bearer '+self.token,'Connection':'close','Accept':MANIFEST}
            if body is not None:headers['Content-Type']=content_type
            deadline_check(deadline)
            connection.request('GET' if body is None else 'POST',path,body=body,headers=headers)
            response=connection.getresponse()
            lengths=response.headers.get_all('Content-Length',[])
            names=[name.lower() for name,_ in response.headers.items()]
            if (response.version!=11 or response.status!=200 or len(lengths)!=1 or not lengths[0].isdigit()
                    or len(set(names))!=len(names)
                    or int(lengths[0])>limit or response.headers.get_all('Transfer-Encoding',[])):fail('registry_http_rejected')
            expected=int(lengths[0]);raw=bytearray()
            while len(raw)<expected:
                deadline_check(deadline);watched.settimeout(max(.001,deadline-time.monotonic()))
                part=response.read1(min(65536,expected-len(raw)))
                if not part:fail('registry_response_incomplete')
                raw.extend(part)
            deadline_check(deadline)
            return bytes(raw),dict(response.headers)
        except RegistryError:raise
        except Exception:fail('registry_publication_unknown' if body is not None else 'registry_observation_unavailable')
        finally:
            if timer is not None:timer.cancel()
            connection.close()


class PublicationClient(_HTTP):
    def register(self,envelope,*,deadline):
        if not isinstance(envelope,dict) or set(envelope)!={'grant','signature'}:fail()
        grant=envelope['grant']
        if not isinstance(grant,dict) or not isinstance(grant.get('grant_ref'),str) or not REF.fullmatch(grant['grant_ref']):fail()
        raw=canonical(envelope)
        if len(raw)>65536:fail()
        result,_=self.call('/internal/v1/grants',deadline=deadline,limit=65536,body=raw,content_type='application/json')
        result=decode(result)
        if result!={'version':1,'grant_ref':grant['grant_ref'],'state':'registered','execute_allowed':False}:
            fail('registry_registration_unknown')
        return result

    def publish(self,grant_ref,raw,*,deadline):
        if not isinstance(grant_ref,str) or not REF.fullmatch(grant_ref) or not isinstance(raw,bytes) or len(raw)>MAX_ARCHIVE:fail()
        result,_=self.call('/internal/v1/publications/'+grant_ref,deadline=deadline,limit=65536,body=raw)
        result=decode(result)
        if result!={'version':1,'grant_ref':grant_ref,'state':'known','execute_allowed':False}:
            fail('registry_publication_unknown')
        return result


class RegistryObserver(_HTTP):
    @property
    def binding(self):return self._binding

    def __init__(self,origin,token,*,public_key,binding,grants,timeout=120):
        super().__init__(origin,token,timeout=timeout)
        from .service import Binding,verify_grant
        if not isinstance(binding,Binding) or not isinstance(grants,(tuple,list)) or not 0<=len(grants)<=24:fail()
        self._public_key,self._binding=public_key,binding
        self._grants={};self._envelopes={}
        for envelope in grants:self.bind(envelope)

    def bind(self,envelope):
        """Trust only a verified original authority envelope, with no network effect."""
        from .service import verify_grant
        grant=verify_grant(envelope,self._public_key,self._binding,allow_expired=True)
        ref=grant['grant_ref']
        if ref in self._envelopes and self._envelopes[ref]!=envelope:fail('registry_registration_conflict')
        if ref not in self._grants and len(self._grants)>=24:fail('registry_registration_limit')
        self._grants[ref]=grant;self._envelopes[ref]=decode(canonical(envelope))
        return decode(canonical(grant))

    def observe_grant(self,grant_ref,*,deadline):
        if not isinstance(grant_ref,str) or not REF.fullmatch(grant_ref):fail()
        raw,_=self.call('/internal/v1/grants/'+grant_ref,deadline=deadline,limit=65536)
        value=decode(raw)
        if (not isinstance(value,dict) or set(value)!={'version','grant_ref','state','envelope','execute_allowed'}
                or value['version']!=1 or value['grant_ref']!=grant_ref or value['state']!='registered'
                or value['execute_allowed'] is not False):fail('registry_registration_unknown')
        from .service import verify_grant
        grant=verify_grant(value['envelope'],self._public_key,self._binding,allow_expired=True)
        if grant['grant_ref']!=grant_ref or grant_ref in self._envelopes and self._envelopes[grant_ref]!=value['envelope']:
            fail('registry_registration_conflict')
        return decode(canonical(value['envelope']))

    def observe(self,grant_ref,*,deadline):
        # This reader verifies its own original signed grant at construction;
        # publication cannot select or replace its expected graph/source facts.
        grant=self._grants.get(grant_ref)
        if grant is None:fail('registry_grant_denied')
        repository=grant.get('repository') if isinstance(grant,dict) else None
        version=grant.get('image_version') if isinstance(grant,dict) else None
        if not isinstance(repository,str) or not REPOSITORY.fullmatch(repository) or not isinstance(version,str):fail()
        from .service import VERSION
        if not VERSION.fullmatch(version):fail()
        for name in ('manifest_digest','config_digest','graph_digest','request_digest','source_evidence_digest','archive_receipt_digest'):
            if not isinstance(grant.get(name),str) or not SHA.fullmatch(grant[name]):fail()
        path='/v2/'+repository+'/manifests/'
        manifest,headers=self.call(path+version,deadline=deadline,limit=1024*1024)
        if (headers.get('Content-Type')!=MANIFEST or headers.get('Docker-Content-Digest')!=sha(manifest)
                or sha(manifest)!=grant['manifest_digest']):fail('registry_manifest_mismatch')
        immutable,_=self.call(path+grant['manifest_digest'],deadline=deadline,limit=1024*1024)
        if immutable!=manifest:fail('registry_manifest_changed')
        document=decode(manifest)
        if (not isinstance(document,dict) or not isinstance(document.get('config'),dict)
                or not isinstance(document.get('layers'),list) or not 1<=len(document['layers'])<=61):fail()
        blobs={};total=0
        for item in [document['config'],*document['layers']]:
            if not isinstance(item,dict) or not isinstance(item.get('digest'),str) or not SHA.fullmatch(item['digest']):fail()
            if item['digest'] in blobs:continue
            raw,_=self.call('/v2/'+repository+'/blobs/'+item['digest'],deadline=deadline,limit=MAX_ARCHIVE-total)
            total+=len(raw)
            if sha(raw)!=item['digest']:fail('registry_blob_mismatch')
            blobs[item['digest']]=raw
        graph=verify_graph(manifest,blobs,architecture=grant['architecture'],manifest_digest=grant['manifest_digest'],
                           config_digest=grant['config_digest'],deadline=deadline)
        if graph.graph_digest!=grant['graph_digest']:fail('registry_graph_mismatch')
        from .service import verify_runtime
        verify_runtime(graph,grant)
        again,_=self.call(path+version,deadline=deadline,limit=1024*1024)
        if again!=manifest:fail('registry_manifest_changed')
        return {'version':1,'grant_ref':grant['grant_ref'],'target_ref':grant['target_ref'],
            'request_digest':grant['request_digest'],'source_evidence_digest':grant['source_evidence_digest'],
            'source_digest':grant['source_digest'],'recipe_digest':grant['recipe_digest'],
            'context_digest':grant['context_digest'],
            'archive_receipt_digest':grant['archive_receipt_digest'],'graph_digest':graph.graph_digest,
            'artifact_identity':{'kind':'oci_repo_digest','repository':'127.0.0.1:5079/'+repository,
                                 'digest':sha(manifest)}}
