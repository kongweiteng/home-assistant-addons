"""Administrator composition over synthetic Unix sockets; no real engine access."""
from contextlib import redirect_stderr,redirect_stdout
from copy import deepcopy
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

import test_ha_artifact_observer as fixture
from ha_artifact_observer.contract import ObserverError,canonical,decode
from ha_artifact_observer.settings import (DEFAULTS,DOCKER_SOCKET,MAX_OPTIONS_BYTES,
    Settings,compose,load_settings,parse_settings)
import ha_artifact_observer.main as entry


class ObserverCompositionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ArtifactObserverTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.raw=deepcopy(DEFAULTS)|{'observation_enabled':True,'docker_socket_enabled':True,
            'bindings':[{'target_ref':'canary-target','instance_ref':'ha-fixture','slug':'local_canary',
                         'container_name':'app_local_canary'}],
            'principals':[{'principal_ref':'manager-observer','token':self.f.token,'target_refs':['canary-target']}]}

    def code(self,code,action):
        with self.assertRaises(ObserverError) as caught:action()
        self.assertEqual(caught.exception.code,code)
        self.assertNotIn(self.f.token,str(caught.exception));self.assertNotIn(fixture.SECRET,str(caught.exception))

    def test_default_options_never_construct_inspector_or_access_unix_socket(self):
        with patch('ha_artifact_observer.docker.DockerInspector',side_effect=AssertionError('no socket')):
            service=compose(parse_settings({}))
        self.assertFalse(service.enabled);self.assertIsNone(service.inspector);self.assertEqual(self.f.requests,[])
        settings=parse_settings(self.raw|{'observation_enabled':False,'docker_socket_enabled':True})
        with patch('ha_artifact_observer.docker.DockerInspector',side_effect=AssertionError('no socket')):
            self.assertFalse(compose(settings).enabled)
        self.assertNotIn(self.f.token,repr(settings));self.assertNotIn(self.f.token,repr(settings.principals[0]))

    def test_two_explicit_flags_and_nonempty_binding_principal_are_required(self):
        for change in ({'docker_socket_enabled':False},{'bindings':[],'principals':[]},
                       {'principals':[]}):
            self.code('observer_enablement_incomplete',lambda:parse_settings(self.raw|change))
        for field in ('observation_enabled','docker_socket_enabled'):
            for value in (1,'true',None):self.code('observer_options_invalid',lambda:parse_settings(self.raw|{field:value}))

    def test_closed_options_reject_paths_urls_environment_and_unknown_nested_authority(self):
        for name in ('socket_path','docker_host','url','command','SUPERVISOR_TOKEN','enable'):
            self.code('observer_options_invalid',lambda:parse_settings(self.raw|{name:fixture.SECRET}))
        for name in ('container','image','url','approved'):
            value=deepcopy(self.raw);value['bindings'][0][name]=fixture.SECRET
            self.code('observer_options_invalid',lambda:parse_settings(value))
        value=deepcopy(self.raw);value['principals'][0]['admin']=True
        self.code('observer_options_invalid',lambda:parse_settings(value))

    def test_binding_and_principal_registration_reject_duplicate_or_cross_scope(self):
        for modify in ('binding','principal','token','instance','target','repeated-target','empty-target'):
            value=deepcopy(self.raw)
            if modify=='binding':value['bindings']*=2
            elif modify=='principal':value['principals']*=2
            elif modify=='token':value['principals'].append(value['principals'][0]|{'principal_ref':'second'})
            elif modify=='instance':value['bindings'].append({'target_ref':'second','instance_ref':'another',
                                                            'slug':'second','container_name':'app_second'})
            elif modify=='target':value['principals'][0]['target_refs']=['unregistered']
            elif modify=='repeated-target':value['principals'][0]['target_refs']*=2
            else:value['principals'][0]['target_refs']=[]
            with self.assertRaises(ObserverError):parse_settings(value)
        value=deepcopy(self.raw);value['bindings'][0]['container_name']='app_other'
        self.code('observer_binding_invalid',lambda:parse_settings(value))

    def test_bounds_types_and_Docker_version_are_explicit_without_fallback(self):
        for field,values in [('timeout_seconds',[True,0,31,float('nan'),float('inf'),'3']),
                ('max_response_bytes',[True,4095,4194305,4096.0]),
                ('docker_api_version',['1.44','v2.44','auto','v1.4','v1.44/containers/json']),
                ('bindings',[{},None]),('principals',[{},None])]:
            for value in values:self.code('observer_options_invalid',lambda:parse_settings(self.raw|{field:value}))
        self.code('observer_options_invalid',lambda:parse_settings(self.raw|{'bindings':[{}]*65}))
        self.code('observer_options_invalid',lambda:parse_settings(self.raw|{'principals':[{}]*17}))
        self.code('observer_options_invalid',lambda:compose(Settings(bindings=('invalid',))))

    def test_composition_passes_only_fixed_socket_and_configured_bounds(self):
        calls=[]
        def local_factory(path,**kwargs):calls.append((path,kwargs));return self.f.inspector
        with patch.dict(os.environ,{'DOCKER_HOST':'tcp://127.0.0.1:1','HTTP_PROXY':'http://127.0.0.1:1'}):
            service=compose(parse_settings(self.raw),inspector_factory=local_factory)
            fact=service.observe('canary-target','Bearer '+self.f.token)
        self.assertEqual(calls,[(DOCKER_SOCKET,{'api_version':'v1.44','timeout_seconds':3,'maxbytes':1048576})])
        self.assertEqual(fact['docker_image_id']['digest'],fixture.IMAGE_ID)
        self.assertEqual([r[:2] for r in self.f.requests],[('GET','/v1.44/containers/app_local_canary/json'),
            ('GET','/v1.44/images/'+fixture.IMAGE_ID+'/json'),('GET','/v1.44/containers/app_local_canary/json')])
        self.assertNotIn(fixture.SECRET,canonical(fact).decode());self.assertNotIn(self.f.token,canonical(fact).decode())

    def test_enabled_composition_HTTP_preserves_existing_protocol_and_principal_scope(self):
        service=compose(parse_settings(self.raw),inspector_factory=lambda *a,**kw:self.f.inspector)
        server=fixture.create_server('127.0.0.1',0,service=service);self.f.run_server(server)
        base='http://127.0.0.1:'+str(server.server_port)
        with urlopen(Request(base+'/v1/artifacts/canary-target',headers={'Authorization':'Bearer '+self.f.token})) as response:
            fact=decode(response.read())
        self.assertEqual(fact['version'],1);self.assertEqual(fact['target_ref'],'canary-target')
        with urlopen(base+'/healthz') as response:self.assertEqual(decode(response.read()),
            {'status':'ok','version':1,'observation_enabled':True})
        self.assertNotIn(self.f.token,canonical(fact).decode());count=len(self.f.requests)
        for path,token in (('/v1/artifacts/canary-target','wrong-'+40*'w'),
                           ('/v1/artifacts/other',self.f.token),('/v1/artifacts/canary-target?path=/containers/json',self.f.token)):
            with self.assertRaises(HTTPError):urlopen(Request(base+path,headers={'Authorization':'Bearer '+token}))
        self.assertEqual(len(self.f.requests),count)

    def test_options_read_is_bounded_regular_file_only_and_rejects_duplicate_JSON(self):
        path=self.f.root/'options.json';path.write_bytes(canonical(self.raw))
        self.assertTrue(load_settings(path).observation_enabled)
        link=self.f.root/'linked.json';link.symlink_to(path)
        self.code('observer_options_unavailable',lambda:load_settings(link))
        self.code('observer_options_unavailable',lambda:load_settings(self.f.root))
        fifo=self.f.root/'pipe';os.mkfifo(fifo)
        self.code('observer_options_unavailable',lambda:load_settings(fifo))
        path.write_bytes(b'x'*(MAX_OPTIONS_BYTES+1));self.code('observer_options_unavailable',lambda:load_settings(path))
        path.write_bytes(b'{"observation_enabled":false,"observation_enabled":true}')
        self.code('observer_json_invalid',lambda:load_settings(path))
        path.write_bytes(('{"token":"'+self.f.token).encode());self.code('observer_json_invalid',lambda:load_settings(path))
        self.assertEqual(self.f.requests,[])

    def test_main_uses_only_fixed_loader_and_bind_address_with_disabled_defaults(self):
        created=[]
        class Server:
            def serve_forever(inner):created.append('served')
        def server(host,port,**kwargs):created.append((host,port,kwargs['service'].enabled));return Server()
        with patch.object(entry,'load_settings',return_value=parse_settings({})) as loader,patch.object(entry,'create_server',side_effect=server):
            self.assertEqual(entry.main(),0)
        loader.assert_called_once_with();self.assertEqual(created,[('0.0.0.0',8099,False),'served'])
        self.assertEqual(self.f.requests,[])

    def test_startup_failures_never_echo_options_tokens_paths_or_exception_text(self):
        for error,expected in ((ObserverError('observer_options_invalid'),'observer_options_invalid'),
                               (OSError(fixture.SECRET+self.f.token),'observer_startup_unavailable')):
            out,err=StringIO(),StringIO()
            with patch.object(entry,'load_settings',side_effect=error),patch.object(entry,'create_server') as server,redirect_stderr(err),redirect_stdout(out):
                self.assertEqual(entry.main(),2)
            self.assertEqual(err.getvalue(),expected+'\n');self.assertEqual(out.getvalue(),'');server.assert_not_called()
            self.assertNotIn(self.f.token,err.getvalue());self.assertNotIn(fixture.SECRET,err.getvalue())

    def test_metadata_remains_socket_disabled_without_supervisor_or_host_exposure(self):
        config=(fixture.ROOT/'ha_artifact_observer/config.yaml').read_text()
        self.assertIn('docker_api: false',config);self.assertIn('hassio_api: false',config)
        self.assertIn('observation_enabled: false',config);self.assertIn('docker_socket_enabled: false',config)
        self.assertIn('8099/tcp: null',config);self.assertNotIn('host_network: true',config)
        self.assertNotIn('privileged:',config);self.assertNotIn('map:',config)


if __name__=='__main__':unittest.main()
