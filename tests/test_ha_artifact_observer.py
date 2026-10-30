"""Real Unix HTTP socket + TCP API fixtures; never a Docker daemon."""
from copy import deepcopy
from datetime import datetime
from http.server import BaseHTTPRequestHandler
import http.client
import importlib.util
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ha_artifact_observer'))
from ha_artifact_observer.contract import Binding,Principal,ObserverError,canonical,decode,stamp
from ha_artifact_observer.docker import DockerInspector
from ha_artifact_observer.service import ObserverService
from ha_artifact_observer.api import create_server

SECRET='synthetic-private-value-never-expose'
IMAGE_ID='sha256:'+'2'*64
OCI_DIGEST='sha256:'+'3'*64


class UnixServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True
    def handle_error(self,*args):pass


class ArtifactObserverTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='ao-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.socket_path=str(self.root/'d.sock');self.requests=[];self.mode='ok';self.inspect_count=0
        self.binding=Binding('canary-target','ha-fixture','local_canary','app_local_canary')
        self.container={'Id':'1'*64,'Name':'/app_local_canary','Image':IMAGE_ID,
            'State':{'Running':True,'Status':'running','Restarting':False,'Dead':False,'StartedAt':'2026-10-28T00:00:00Z'},
            'Config':{'Env':['PASSWORD='+SECRET],'Labels':{'secret':SECRET}},'Mounts':[{'Source':SECRET}], 'LogPath':SECRET}
        self.image={'Id':IMAGE_ID,'RepoDigests':['registry.example.invalid/canary@'+OCI_DIGEST],
                    'Config':{'Env':['PASSWORD='+SECRET]},'RootFS':{'Type':'layers','Layers':['sha256:'+'4'*64]}}
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                owner.requests.append((self.command,self.path,self.headers.get('Authorization')))
                if self.path=='/v1.44/containers/app_local_canary/json':
                    owner.inspect_count+=1;value=deepcopy(owner.container)
                    if owner.mode=='wrong_name':value['Name']='/app_other'
                    if owner.mode=='stopped':value['State']['Running']=False;value['State']['Status']='exited'
                    if owner.mode=='race' and owner.inspect_count%2==0:value['Id']='5'*64
                    if owner.mode=='restart_race' and owner.inspect_count%2==0:value['State']['StartedAt']='2026-10-28T00:00:01Z'
                elif self.path=='/v1.44/images/'+IMAGE_ID+'/json':
                    value=deepcopy(owner.image)
                    if owner.mode=='wrong_image':value['Id']='sha256:'+'6'*64
                else:self.send_error(404);return
                body=canonical(value)
                if owner.mode=='duplicate':body=b'{"Id":"first","Id":"second"}'
                if owner.mode=='nan':body=b'{"value":NaN}'
                if owner.mode=='oversize':body=canonical({'private':SECRET*1000})
                if owner.mode=='redirect':
                    self.send_response(302);self.send_header('Location','http://127.0.0.1:1/'+SECRET);self.end_headers();return
                if owner.mode=='slow_headers':
                    try:
                        for byte in b'HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n':
                            self.wfile.write(bytes([byte]));self.wfile.flush();time.sleep(0.025)
                    except OSError:pass
                    return
                self.send_response(200)
                if owner.mode=='slow':
                    self.send_header('Transfer-Encoding','chunked');self.end_headers()
                    try:
                        for offset in range(0,len(body),16):
                            chunk=body[offset:offset+16]
                            self.wfile.write(hex(len(chunk))[2:].encode()+b'\r\n'+chunk+b'\r\n');self.wfile.flush();time.sleep(0.025)
                        self.wfile.write(b'0\r\n\r\n')
                    except OSError:pass
                    return
                self.send_header('Content-Length',str(len(body)+(1 if owner.mode=='truncated' else 0)))
                self.end_headers();self.wfile.write(body)
        self.unix=UnixServer(self.socket_path,Handler);self.run_server(self.unix)
        self.inspector=DockerInspector(self.socket_path,timeout_seconds=1,maxbytes=4096)
        self.token='observer-fixture-'+40*'o'
        self.principal=Principal('manager-observer',self.token,frozenset({'canary-target'}))
        self.clock=lambda:datetime(2026,10,29,7,0,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.service=ObserverService(inspector=self.inspector,bindings=(self.binding,),principals=(self.principal,),enabled=True,clock=self.clock)
        self.api=create_server('127.0.0.1',0,service=self.service);self.run_server(self.api)
        self.base='http://127.0.0.1:'+str(self.api.server_port)

    def run_server(self,server):
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)

    def observe(self):return self.service.observe('canary-target','Bearer '+self.token)
    def code(self,code,call):
        with self.assertRaises(ObserverError) as error:call()
        self.assertEqual(error.exception.code,code)
        self.assertNotIn(SECRET,str(error.exception))

    def test_real_unix_socket_exact_container_to_actual_image_and_repo_digest(self):
        fact=self.observe();self.assertEqual(set(fact),{'version','target_ref','instance_ref','slug','container_id','docker_image_id','oci_repo_digests','running','observed_at'})
        self.assertEqual(fact['docker_image_id'],{'kind':'docker_image_id','digest':IMAGE_ID})
        self.assertEqual(fact['oci_repo_digests'],[{'kind':'oci_repo_digest','repository':'registry.example.invalid/canary','digest':OCI_DIGEST}])
        self.assertEqual(fact['container_id'],self.container['Id']);self.assertTrue(fact['observed_at'].endswith('+08:00'))
        self.assertEqual(self.requests,[('GET','/v1.44/containers/app_local_canary/json',None),
             ('GET','/v1.44/images/'+IMAGE_ID+'/json',None),('GET','/v1.44/containers/app_local_canary/json',None)])
        self.assertNotIn(SECRET,canonical(fact).decode());self.assertNotIn('Config',canonical(fact).decode())

    def test_local_build_empty_repo_digests_stays_typed_image_id_without_fabricated_OCI(self):
        self.image['RepoDigests']=[];fact=self.observe()
        self.assertEqual(fact['oci_repo_digests'],[]);self.assertEqual(fact['docker_image_id']['kind'],'docker_image_id')
        self.assertNotIn('artifact_digest',fact);self.assertNotEqual(IMAGE_ID,OCI_DIGEST)
        self.image['RepoDigests']=None
        self.assertEqual(self.observe()['oci_repo_digests'],[])

    def test_wrong_container_image_stopped_and_recreate_races_are_rejected(self):
        for mode,code in (('wrong_name','observer_container_binding_invalid'),('wrong_image','observer_image_binding_invalid'),
                ('stopped','observer_container_binding_invalid'),('race','observer_container_changed'),('restart_race','observer_container_changed')):
            self.mode=mode;self.inspect_count=0;self.code(code,self.observe)

    def test_untrusted_arbitrary_image_pointer_never_generates_another_Docker_route(self):
        for value in ('../containers/json','http://example.invalid/steal','sha256:'+'a'*65,{'fake':True}):
            self.container['Image']=value;self.requests.clear();self.code('observer_container_binding_invalid',self.observe)
            self.assertEqual(len(self.requests),1)

    def test_invalid_RepoDigests_duplicates_missing_or_mismatched_image_fail(self):
        for value in ([SECRET],['registry.example.invalid/canary@sha256:'+'a'*63],['registry.example.invalid/canary@'+OCI_DIGEST]*2,
                      ['http://example.invalid/a@'+OCI_DIGEST],SECRET):
            self.image['RepoDigests']=value
            with self.assertRaises(ObserverError):self.observe()
        self.image.pop('RepoDigests')
        self.code('observer_image_binding_invalid',self.observe)

    def test_bounded_duplicate_json_nonfinite_redirect_and_truncated_fail_sanitized(self):
        for mode,code in (('duplicate','observer_json_invalid'),('nan','observer_json_invalid'),('redirect','observer_docker_rejected'),
                           ('truncated','observer_response_incomplete'),('oversize','observer_response_too_large')):
            self.mode=mode;self.code(code,self.observe)

    def test_slow_chunked_response_cannot_reset_total_deadline(self):
        self.mode='slow';inspector=DockerInspector(self.socket_path,timeout_seconds=0.08,maxbytes=4096)
        start=time.monotonic()
        with self.assertRaises(ObserverError):inspector.inspect(self.binding,deadline=start+10)
        self.assertLess(time.monotonic()-start,0.4)

    def test_slow_status_and_headers_cannot_extend_total_deadline(self):
        self.mode='slow_headers';inspector=DockerInspector(self.socket_path,timeout_seconds=0.08,maxbytes=4096)
        start=time.monotonic()
        with self.assertRaises(ObserverError):inspector.inspect(self.binding,deadline=start+10)
        self.assertLess(time.monotonic()-start,0.4)

    def test_concurrent_capacity_is_bounded_without_extra_socket_requests(self):
        self.service._capacity.acquire();self.service._capacity.acquire()
        try:self.code('observer_busy',self.observe);self.assertEqual(self.requests,[])
        finally:self.service._capacity.release();self.service._capacity.release()

    def test_admin_binding_socket_config_and_registration_cannot_be_caller_authority(self):
        for container in ('other','app_other','app_local_canary/../json','http://example.invalid/a'):
            self.code('observer_binding_invalid',lambda:Binding('t','ha-fixture','local_canary',container))
        for path in ('relative','http://example.invalid/','tcp://127.0.0.1:2375'):
            self.code('observer_docker_config_invalid',lambda:DockerInspector(path))
        linked=self.root/'linked.sock';linked.symlink_to(self.socket_path)
        self.code('observer_docker_config_invalid',lambda:DockerInspector(str(linked)))
        self.code('observer_registration_invalid',lambda:ObserverService(inspector=self.inspector,bindings=(self.binding,self.binding)))
        other=Binding('other','another-instance','other','app_other')
        self.code('observer_registration_invalid',lambda:ObserverService(bindings=(self.binding,other)))
        self.code('observer_registration_invalid',lambda:ObserverService(bindings=(self.binding,),principals=(self.principal,self.principal)))

    def test_bearer_exact_principal_target_scope_and_no_discovery(self):
        for token in ('Bearer '+self.token+'x','Bearer manager-token-'+40*'m','Bearer backup-token-'+40*'b',''):
            self.code('observer_not_authorized',lambda:self.service.observe('canary-target',token))
        self.code('observer_target_denied',lambda:self.service.observe('not-registered','Bearer '+self.token))
        self.assertEqual(self.requests,[])
        second=Binding('second','ha-fixture','second','app_second')
        service=ObserverService(inspector=self.inspector,bindings=(self.binding,second),principals=(self.principal,),enabled=True)
        self.code('observer_target_denied',lambda:service.observe('second','Bearer '+self.token));self.assertEqual(self.requests,[])

    def test_HTTP_API_filters_secrets_and_rejects_all_mutating_or_arbitrary_routes(self):
        request=Request(self.base+'/v1/artifacts/canary-target',headers={'Authorization':'Bearer '+self.token})
        with urlopen(request) as response:fact=decode(response.read())
        self.assertNotIn(SECRET,canonical(fact).decode());initial=len(self.requests)
        for method in ('POST','PUT','PATCH','DELETE','OPTIONS','TRACE','CONNECT'):
            with self.assertRaises(HTTPError) as error:urlopen(Request(self.base+'/v1/artifacts/canary-target',headers={'Authorization':'Bearer '+self.token},method=method))
            self.assertEqual(error.exception.code,405)
        for path in ('/v1/artifacts','/v1/artifacts/canary-target?url=abc','/v1/artifacts/canary-target/json',
                     '/v1/artifacts/%63anary-target','/containers/json','/images/json','/v1/artifacts/../json'):
            with self.assertRaises(HTTPError) as error:urlopen(Request(self.base+path,headers={'Authorization':'Bearer '+self.token}))
            self.assertIn(error.exception.code,{403,404})
        self.assertEqual(len(self.requests),initial)

    def test_HTTP_duplicate_Authorization_and_GET_body_rejected_before_Docker(self):
        connection=http.client.HTTPConnection('127.0.0.1',self.api.server_port)
        connection.putrequest('GET','/v1/artifacts/canary-target');connection.putheader('Authorization','Bearer '+self.token)
        connection.putheader('Authorization','Bearer '+self.token);connection.endheaders();response=connection.getresponse()
        self.assertEqual(response.status,401);response.read();connection.close()
        with self.assertRaises(HTTPError) as error:urlopen(Request(self.base+'/v1/artifacts/canary-target',data=b'{}',headers={'Authorization':'Bearer '+self.token},method='GET'))
        self.assertEqual(error.exception.code,400);self.assertEqual(self.requests,[])

    def test_missing_ports_disabled_and_production_server_are_unavailable(self):
        service=ObserverService(enabled=True)
        self.code('observer_authority_unavailable',lambda:service.observe('canary-target','Bearer '+self.token))
        service.enabled=False;self.code('observer_disabled',lambda:service.observe('canary-target','Bearer '+self.token))
        server=create_server('127.0.0.1',0);self.run_server(server);base='http://127.0.0.1:'+str(server.server_port)
        with urlopen(base+'/healthz') as response:self.assertFalse(decode(response.read())['observation_enabled'])
        with self.assertRaises(HTTPError) as error:urlopen(base+'/v1/artifacts/canary-target')
        self.assertEqual(error.exception.code,404);self.assertEqual(self.requests,[])

    def test_Typed_port_output_rejects_extra_nested_Config_before_HTTP_return(self):
        original=self.inspector.inspect
        def forged(*args,**kwargs):
            value=original(*args,**kwargs);value['docker_image_id']['Config']={'Env':[SECRET]};return value
        self.inspector.inspect=forged
        self.code('observer_fact_invalid',self.observe)

    def test_midnight_month_year_and_UTC_clock_convert_to_Shanghai(self):
        from datetime import timezone
        for value,expected in ((datetime(2026,12,31,16,0,tzinfo=timezone.utc),'2027-01-01T00:00:00'),
                (datetime(2026,10,31,16,0,tzinfo=timezone.utc),'2026-11-01T00:00:00'),
                (datetime(2026,10,28,15,59,59,tzinfo=timezone.utc),'2026-10-28T23:59:59')):
            self.assertTrue(stamp(value).startswith(expected));self.assertTrue(stamp(value).endswith('+08:00'))

    def test_candidate_packaging_is_disabled_without_socket_Docker_API_or_supervisor(self):
        config=(ROOT/'ha_artifact_observer/config.yaml').read_text();main=(ROOT/'ha_artifact_observer/ha_artifact_observer/main.py').read_text()
        self.assertIn('docker_api: false',config);self.assertIn('hassio_api: false',config)
        self.assertNotIn('DockerInspector',main);self.assertNotIn('socket_path',main)
        self.assertNotIn('SUPERVISOR_TOKEN',main)
        self.assertTrue((ROOT/'ha_artifact_observer/DESIGN.md').is_file())


if __name__=='__main__':unittest.main()
