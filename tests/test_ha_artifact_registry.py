"""Real source-built OCI bytes, signatures and HTTP; no HA/Engine acceptance."""
import base64
from dataclasses import replace
import gzip
from datetime import datetime,timedelta
import http.client
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import ssl
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat

from ha_artifact_registry.ha_artifact_registry.image import (
    RegistryError,canonical,decode,sha,verify_archive,verify_graph,
)
from ha_artifact_registry.ha_artifact_registry.service import Binding,RegistryStore,verify_grant
from ha_artifact_registry.ha_artifact_registry.http import PublicationClient,RegistryObserver,make_server
from ha_artifact_registry.ha_artifact_registry.main import compose,verify_bridge
import test_codex_delivery_oci_builder as builder_fixture


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=builder_fixture.OCIBuilderTests();cls.fixture.setUp()
        cls.addClassCleanup(cls.fixture.doCleanups)
        cls.fixture.recipe=replace(cls.fixture.recipe,profile='ha-canary-python-v1',target_ref='target')
        cls.build_evidence=cls.fixture.build()
        cls.artifact=(cls.fixture.builder()._directory(cls.fixture.request)/'artifact.tar').read_bytes()
        cls.graph=verify_archive(cls.artifact,artifact_digest=cls.build_evidence.artifact_blob_digest,
            architecture='amd64',manifest_digest=cls.build_evidence.image_manifest_digest,
            config_digest=cls.build_evidence.image_config_digest,deadline=time.monotonic()+30)

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name).resolve()/'registry'
        self.key=Ed25519PrivateKey.generate()
        self.public=self.key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
        self.now=datetime.now(ZoneInfo('Asia/Shanghai'))
        self.binding=Binding('target','instance','local_codex_work_canary','codex-work/amd64-canary','amd64',
            self.build_evidence.recipe_digest,self.fixture.recipe.permissions_digest,self.fixture.recipe.qualification_digest)
        self.writer='fixture-writer-bearer-never-a-real-credential'
        self.reader='fixture-reader-bearer-never-a-real-credential'
        self.grant={'version':1,'grant_ref':'grant-B','target_ref':'target','instance_ref':'instance',
            'slug':'local_codex_work_canary','repository':self.binding.repository,'architecture':'amd64',
            'image_version':'0.1.0','artifact_digest':sha(self.artifact),'build_profile':'ha-canary-python-v1',
            'manifest_digest':self.build_evidence.image_manifest_digest,
            'config_digest':self.build_evidence.image_config_digest,'graph_digest':self.graph.graph_digest,
            'request_digest':self.build_evidence.request_digest,
            'source_evidence_digest':self.build_evidence.evidence_digest,
            'source_digest':self.fixture.request['source_digest'],'recipe_digest':self.build_evidence.recipe_digest,
            'context_digest':self.build_evidence.context_digest,
            'permissions_digest':self.fixture.recipe.permissions_digest,
            'qualification_digest':self.fixture.recipe.qualification_digest,
            'archive_receipt_digest':sha(b'fixture receipt, not an actual off-device archive'),
            'issued_at':(self.now-timedelta(seconds=5)).isoformat(),
            'expires_at':(self.now+timedelta(hours=1)).isoformat()}
        self.envelope=self.signed(self.grant);self.store=self.new_store()

    def signed(self,grant):
        return {'grant':dict(grant),'signature':base64.b64encode(self.key.sign(canonical(grant))).decode()}

    def new_store(self,**kwargs):
        return RegistryStore(self.root,public_key=self.public,bindings=(self.binding,),
            grants=kwargs.pop('grants',(self.envelope,)),writer_token=self.writer,reader_token=self.reader,
            enabled=kwargs.pop('enabled',True),clock=kwargs.pop('clock',lambda:self.now),**kwargs)

    def publish(self,store=None,raw=None):
        return (store or self.store).publish('grant-B',self.artifact if raw is None else raw,'Bearer '+self.writer,
                                             deadline=time.monotonic()+30)

    def server(self,administrative=True):
        server=make_server(('127.0.0.1',0),self.store,administrative=administrative)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def cleanup():server.shutdown();server.server_close();thread.join(timeout=2)
        self.addCleanup(cleanup)
        return 'http://127.0.0.1:'+str(server.server_port),server.server_port

    def observer(self,origin):
        return RegistryObserver(origin,self.reader,public_key=self.public,binding=self.binding,grants=(self.envelope,))

    def test_source_git_oci_publication_independent_http_full_graph_and_repository_identity(self):
        origin,_=self.server()
        client=PublicationClient(origin,self.writer)
        published=client.publish('grant-B',self.artifact,deadline=time.monotonic()+30)
        self.assertEqual('known',published['state'])
        with patch.object(RegistryStore,'publish',side_effect=AssertionError('observer cannot publish')):
            observed=self.observer(origin).observe('grant-B',deadline=time.monotonic()+30)
        self.assertEqual(observed['artifact_identity'],{'kind':'oci_repo_digest',
            'repository':'127.0.0.1:5079/codex-work/amd64-canary','digest':self.build_evidence.image_manifest_digest})
        self.assertEqual(observed['request_digest'],self.build_evidence.request_digest)
        self.assertFalse(self.fixture.marker.exists())

    def test_restart_retains_original_slot_and_replay_never_writes(self):
        expected=self.publish();restarted=self.new_store()
        before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in restarted.directory(self.grant).iterdir()}
        with patch('ha_artifact_registry.ha_artifact_registry.service.write',side_effect=AssertionError('no replay')):
            self.assertEqual(expected,self.publish(restarted,raw=b'foreign'))
        after={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in restarted.directory(self.grant).iterdir()}
        self.assertEqual(before,after)

    def test_new_signed_grant_cannot_overwrite_previous_version_after_restart(self):
        self.publish();directory=self.store.directory(self.grant)
        original={p.name:p.read_bytes() for p in directory.iterdir()}
        replacement=self.signed(dict(self.grant,grant_ref='replacement-grant'))
        restarted=self.new_store(grants=(replacement,))
        result=restarted.publish('replacement-grant',self.artifact,'Bearer '+self.writer,deadline=time.monotonic()+3)
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(original,{p.name:p.read_bytes() for p in directory.iterdir()})

    def test_interruption_after_claim_is_unknown_and_never_redispatched(self):
        from ha_artifact_registry.ha_artifact_registry.service import write
        def interrupted(path,raw):
            if path.name=='ready.json':raise OSError('fixture crash')
            write(path,raw)
        with patch('ha_artifact_registry.ha_artifact_registry.service.write',side_effect=interrupted):
            with self.assertRaises(OSError):self.publish()
        restarted=self.new_store()
        with patch('ha_artifact_registry.ha_artifact_registry.service.write',side_effect=AssertionError('no replay')):
            self.assertEqual('unknown',self.publish(restarted)['state'])
        with self.assertRaises(FileNotFoundError):restarted.manifest(self.binding.repository,'0.1.0')

    def test_unauthorized_publisher_and_invalid_artifact_create_no_claim(self):
        with self.assertRaisesRegex(RegistryError,'authorized'):
            self.store.publish('grant-B',self.artifact,'Bearer '+self.reader,deadline=time.monotonic()+30)
        with self.assertRaises(RegistryError):self.publish(raw=self.artifact+b'changed')
        self.assertFalse(self.root.exists())

    def test_signature_target_and_duplicate_immutable_versions_rejected(self):
        bad=dict(self.envelope,signature=base64.b64encode(b'x'*64).decode())
        with self.assertRaisesRegex(RegistryError,'signature'):self.new_store(grants=(bad,))
        with self.assertRaisesRegex(RegistryError,'invalid'):
            self.new_store(grants=(self.signed(dict(self.grant,target_ref='foreign')),))
        with self.assertRaisesRegex(RegistryError,'invalid'):
            self.new_store(grants=(self.envelope,self.signed(dict(self.grant,grant_ref='another-grant'))))

    def test_expiry_revokes_publication_but_preserves_completed_reads(self):
        self.publish();self.now+=timedelta(hours=2)
        with self.assertRaisesRegex(RegistryError,'expired'):self.publish()
        _,manifest=self.store.manifest(self.binding.repository,'0.1.0')
        self.assertEqual(sha(manifest),self.grant['manifest_digest'])

    def test_read_auth_separate_from_write_and_public_listener_has_no_mutations(self):
        self.publish();_,admin=self.server();_,public=self.server(False)
        for port,method,path,token,expected in (
            (admin,'GET','/v2/',self.writer,401),(admin,'GET','/v2/',self.reader,200),
            (public,'GET','/v2/','',200),(public,'POST','/internal/v1/publications/grant-B',self.writer,404),
            (admin,'PUT','/v2/codex-work/amd64-canary/manifests/0.1.0',self.writer,404),
            (public,'GET','/v2/_catalog','',404),(public,'GET','/v2/codex-work/amd64-canary/tags/list','',404)):
            with self.subTest(method=method,path=path,port=port):
                connection=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
                connection.request(method,path,headers={'Authorization':'Bearer '+token})
                response=connection.getresponse();response.read();connection.close()
                self.assertEqual(expected,response.status)

    def test_head_manifest_and_exact_blob_digest_routes(self):
        self.publish();_,port=self.server(False)
        for reference in ('0.1.0',self.grant['manifest_digest']):
            connection=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
            connection.request('HEAD','/v2/'+self.binding.repository+'/manifests/'+reference)
            response=connection.getresponse();self.assertEqual(response.status,200)
            self.assertEqual(response.getheader('Docker-Content-Digest'),self.grant['manifest_digest'])
            self.assertEqual(response.read(),b'');connection.close()

    def test_unknown_target_blob_routes_and_path_tricks_rejected(self):
        self.publish();origin,_=self.server();observer=self.observer(origin)
        with self.assertRaises(RegistryError):observer.observe('foreign',deadline=time.monotonic()+3)
        for path in ('/v2/foreign/manifests/0.1.0','/v2/codex-work/amd64-canary/manifests/%2e%2e',
                     '/v2/codex-work/amd64-canary/manifests/0.1.0?secret=not-read'):
            with self.subTest(path=path),self.assertRaises(RegistryError):
                observer.call(path,deadline=time.monotonic()+3,limit=4096)
        with self.assertRaises(RegistryError):self.store.blob(self.binding.repository,sha(b'unknown'))

    def test_actual_retained_layer_drift_is_detected_by_independent_reader(self):
        self.publish();origin,_=self.server()
        layer=decode(self.graph.manifest)['layers'][-1]['digest']
        path=self.store.directory(self.grant)/layer[7:];path.write_bytes(b'changed')
        with self.assertRaises(RegistryError):self.observer(origin).observe('grant-B',deadline=time.monotonic()+5)

    def test_config_diff_id_descriptor_and_graph_mismatch_rejected(self):
        graph=self.graph;metadata=decode(graph.config);metadata['rootfs']['diff_ids']=[sha(b'foreign')]
        wrong_config=canonical(metadata);document=decode(graph.manifest)
        document['config']={'mediaType':document['config']['mediaType'],'digest':sha(wrong_config),'size':len(wrong_config)}
        wrong_manifest=canonical(document);blobs=dict(graph.blobs)
        del blobs[sha(graph.config)];blobs[sha(wrong_config)]=wrong_config
        with self.assertRaises(RegistryError):verify_graph(wrong_manifest,blobs,architecture='amd64',
            manifest_digest=sha(wrong_manifest),config_digest=sha(wrong_config),deadline=time.monotonic()+3)
        with self.assertRaises(RegistryError):verify_graph(graph.manifest,dict(graph.blobs,**{sha(b'extra'):b'extra'}),
            architecture='amd64',manifest_digest=sha(graph.manifest),config_digest=sha(graph.config),deadline=time.monotonic()+3)
        envelope=self.signed(dict(self.grant,graph_digest=sha(b'foreign')))
        with self.assertRaisesRegex(RegistryError,'graph'):self.publish(self.new_store(grants=(envelope,)))

    def test_gzip_layer_revalidated_and_bounded_independently(self):
        document=decode(self.graph.manifest);first=document['layers'][0]
        original=self.graph.blobs[first['digest']];compressed=gzip.compress(original,mtime=0)
        blobs=dict(self.graph.blobs);del blobs[first['digest']];blobs[sha(compressed)]=compressed
        document['layers'][0]={'mediaType':first['mediaType']+'+gzip','digest':sha(compressed),'size':len(compressed)}
        manifest=canonical(document)
        kwargs={'architecture':'amd64','manifest_digest':sha(manifest),
                'config_digest':self.grant['config_digest'],'deadline':time.monotonic()+3}
        self.assertIsNotNone(verify_graph(manifest,blobs,**kwargs))
        with patch('ha_artifact_registry.ha_artifact_registry.image.MAX_EXPANDED',1):
            with self.assertRaisesRegex(RegistryError,'expansion'):verify_graph(manifest,blobs,**kwargs)
        broken=compressed[:-8];del blobs[sha(compressed)];blobs[sha(broken)]=broken
        document['layers'][0].update(digest=sha(broken),size=len(broken));manifest=canonical(document)
        kwargs['manifest_digest']=sha(manifest)
        with self.assertRaises(RegistryError):verify_graph(manifest,blobs,**kwargs)

    def test_duplicate_outer_archive_and_descriptor_size_rejected(self):
        output=BytesIO()
        with tarfile.open(fileobj=BytesIO(self.artifact)) as source,tarfile.open(fileobj=output,mode='w') as target:
            for item in source:
                target.addfile(item,source.extractfile(item))
            item=tarfile.TarInfo('index.json');item.size=2;target.addfile(item,BytesIO(b'{}'))
        raw=output.getvalue()
        with self.assertRaises(RegistryError):verify_archive(raw,artifact_digest=sha(raw),architecture='amd64',
            manifest_digest=self.grant['manifest_digest'],config_digest=self.grant['config_digest'],deadline=time.monotonic()+3)
        document=decode(self.graph.manifest);document['layers'][0]['size']+=1;manifest=canonical(document)
        with self.assertRaises(RegistryError):verify_graph(manifest,self.graph.blobs,architecture='amd64',
            manifest_digest=sha(manifest),config_digest=self.grant['config_digest'],deadline=time.monotonic()+3)

    def test_disabled_and_bad_private_startup_configuration_fail_closed(self):
        disabled=self.new_store(enabled=False)
        with self.assertRaisesRegex(RegistryError,'disabled'):self.publish(disabled)
        for address in ('0.0.0.0','127.0.0.1','8.8.8.8'):
            policy={'version':1,'enabled':False,'docker_bridge_host':address,
                'public_key':base64.b64encode(self.public).decode(),'writer_token':self.writer,'reader_token':self.reader,
                'bindings':[self.binding.__dict__],'grants':[self.envelope]}
            with self.subTest(address=address),self.assertRaises(RegistryError):compose(policy)
        with patch('ha_artifact_registry.ha_artifact_registry.main.Path.is_dir',return_value=False):
            with self.assertRaisesRegex(RegistryError,'bridge'):verify_bridge('172.30.32.1')

    def test_publication_claim_is_exclusive_across_threads(self):
        results=[];errors=[]
        def run():
            try:results.append(self.publish())
            except Exception as error:errors.append(error)
        threads=[threading.Thread(target=run) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=10)
        self.assertFalse(errors);self.assertEqual(2,len(results))
        self.assertEqual('known',self.store.status('grant-B')['state'])

    def test_client_deadline_is_aggregate_and_unknown_post_is_not_retried(self):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
        port=listener.getsockname()[1];accepted=[]
        def slow():
            connection,_=listener.accept();accepted.append(1)
            try:
                connection.recv(65536);time.sleep(.4)
            finally:connection.close();listener.close()
        thread=threading.Thread(target=slow,daemon=True);thread.start()
        client=PublicationClient('http://127.0.0.1:'+str(port),self.writer,timeout=.1)
        start=time.monotonic()
        with self.assertRaises(RegistryError):client.publish('grant-B',self.artifact,deadline=time.monotonic()+3)
        self.assertLess(time.monotonic()-start,.35);thread.join(timeout=1);self.assertEqual([1],accepted)

    def test_unsafe_origins_and_malformed_observer_grants_rejected(self):
        for origin in ('https://127.0.0.1:5080','http://8.8.8.8:5080','http://0.0.0.0:5080',
                       'http://user:password@127.0.0.1:5080','http://127.0.0.1:5080/path'):
            with self.subTest(origin=origin),self.assertRaises(RegistryError):PublicationClient(origin,self.writer)
        with self.assertRaises(RegistryError):RegistryObserver('http://127.0.0.1:5080',self.reader,
            public_key=self.public,binding=self.binding,grants=(dict(self.envelope,signature='invalid'),))

    def test_signed_wrong_runtime_source_or_version_is_not_publishable(self):
        for changes in ({'source_digest':sha(b'foreign source')},{'image_version':'0.9.0'},
                        {'context_digest':sha(b'foreign context')}):
            envelope=self.signed(dict(self.grant,**changes))
            with self.subTest(changes=changes),self.assertRaisesRegex(RegistryError,'runtime'):
                self.publish(self.new_store(grants=(envelope,)))
        self.assertFalse(self.root.exists())

    def test_dynamic_signed_registration_publish_restart_and_independent_get(self):
        self.store=self.new_store(grants=());origin,_=self.server()
        client=PublicationClient(origin,self.writer)
        observer=RegistryObserver(origin,self.reader,public_key=self.public,binding=self.binding,grants=())
        self.assertEqual('registered',client.register(self.envelope,deadline=time.monotonic()+5)['state'])
        self.assertEqual(self.envelope,observer.observe_grant('grant-B',deadline=time.monotonic()+5))
        observer.bind(self.envelope)
        client.publish('grant-B',self.artifact,deadline=time.monotonic()+5)
        self.assertEqual(observer.observe('grant-B',deadline=time.monotonic()+5)['artifact_identity']['digest'],
                         self.grant['manifest_digest'])
        restarted=self.new_store(grants=())
        self.assertEqual(restarted.registration('grant-B')['envelope'],self.envelope)
        self.assertEqual(restarted.status('grant-B')['state'],'known')

    def test_dynamic_registration_rejects_scope_drift_and_immutable_conflicts(self):
        self.store=self.new_store(grants=())
        for field in ('recipe_digest','permissions_digest','qualification_digest'):
            with self.subTest(field=field),self.assertRaises(RegistryError):
                self.store.register(self.signed(dict(self.grant,**{field:sha(b'foreign')})),
                    'Bearer '+self.writer,deadline=time.monotonic()+3)
        self.store.register(self.envelope,'Bearer '+self.writer,deadline=time.monotonic()+3)
        for value in (dict(self.grant,grant_ref='different-grant'),dict(self.grant,image_version='0.9.0')):
            with self.assertRaisesRegex(RegistryError,'conflict'):
                self.store.register(self.signed(value),'Bearer '+self.writer,deadline=time.monotonic()+3)

    def test_incomplete_dynamic_registration_is_read_only_unknown_and_blocks_writes(self):
        from ha_artifact_registry.ha_artifact_registry.service import write
        self.store=self.new_store(grants=())
        def partial(path,raw):
            if path.name=='grant-B.json':write(path,b'{');raise OSError('fixture partial')
            write(path,raw)
        with patch('ha_artifact_registry.ha_artifact_registry.service.write',side_effect=partial):
            with self.assertRaises(OSError):self.store.register(self.envelope,'Bearer '+self.writer,deadline=time.monotonic()+3)
        restarted=self.new_store(grants=())
        self.assertEqual(restarted.registration('grant-B')['state'],'unknown')
        with patch('ha_artifact_registry.ha_artifact_registry.service.write',side_effect=AssertionError('no replay')):
            with self.assertRaisesRegex(RegistryError,'unknown'):
                restarted.register(self.envelope,'Bearer '+self.writer,deadline=time.monotonic()+3)

    def raw_http(self,port,request,timeout=8):
        with socket.create_connection(('127.0.0.1',port),timeout=timeout) as channel:
            channel.settimeout(timeout);channel.sendall(request);output=bytearray()
            while True:
                try:part=channel.recv(65536)
                except ConnectionResetError:break
                if not part:break
                output.extend(part)
            return bytes(output)

    def test_duplicate_headers_http_versions_bodies_and_ranges_are_rejected(self):
        self.publish();_,port=self.server(False);host='127.0.0.1:'+str(port)
        for version,headers,expected in (
            ('HTTP/1.0',[],b'400'),('HTTP/1.1',['Host: '+host],b'400'),
            ('HTTP/1.1',['Authorization: x','Authorization: y'],b'400'),
            ('HTTP/1.1',['Content-Length: 0','Content-Length: 0'],b'400'),
            ('HTTP/1.1',['Content-Length: 0','Transfer-Encoding: chunked'],b'400'),
            ('HTTP/1.1',['Content-Length: 1'],b'400'),('HTTP/1.1',['Range: bytes=0-10'],b'416'),
            ('HTTP/1.1',['Content-Type: a','Content-Type: b'],b'400')):
            request=('GET /v2/ '+version+'\r\nHost: '+host+'\r\n'+'\r\n'.join(headers)+'\r\n\r\n').encode()
            with self.subTest(version=version,headers=headers):
                self.assertIn(expected,self.raw_http(port,request).split(b'\r\n',1)[0])
        request=('GET /v2/ HTTP/1.1\r\nHost: '+host+'\r\nX-Long: '+'x'*5000+'\r\n\r\n').encode()
        self.assertIn(b'431',self.raw_http(port,request).split(b'\r\n',1)[0])

    def test_slow_header_total_deadline_and_connection_capacity(self):
        _,port=self.server(False)
        channels=[socket.create_connection(('127.0.0.1',port),timeout=2) for _ in range(2)]
        try:
            for channel in channels:channel.sendall(b'GET /v2/ HTTP/1.1\r\nHost:')
            time.sleep(.05)
            with socket.create_connection(('127.0.0.1',port),timeout=2) as third:
                third.settimeout(1);self.assertEqual(third.recv(1),b'')
            start=time.monotonic();channels[0].settimeout(6)
            self.assertEqual(channels[0].recv(1),b'')
            self.assertLess(time.monotonic()-start,5.9)
        finally:
            for channel in channels:channel.close()

    def test_ephemeral_loopback_tls_uses_no_retained_key_and_serves_exact_manifest(self):
        from ha_artifact_registry.ha_artifact_registry.tls import ephemeral_context
        self.publish();before=set(self.root.parent.iterdir());context=ephemeral_context(self.root.parent)
        self.assertEqual(before,set(self.root.parent.iterdir()))
        server=make_server(('127.0.0.1',0),self.store,tls_context=context)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def cleanup():server.shutdown();server.server_close();thread.join(timeout=2)
        self.addCleanup(cleanup)
        verifier=ssl.create_default_context()
        connection=http.client.HTTPSConnection('127.0.0.1',server.server_port,context=verifier,timeout=3)
        with self.assertRaises(ssl.SSLCertVerificationError):connection.request('GET','/v2/')
        connection.close()
        # Mirrors the existing loopback-insecure Engine policy only. This is
        # not a CA or peer-identity qualification.
        verifier=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);verifier.check_hostname=False;verifier.verify_mode=ssl.CERT_NONE
        connection=http.client.HTTPSConnection('127.0.0.1',server.server_port,context=verifier,timeout=3)
        connection.request('GET','/v2/'+self.binding.repository+'/manifests/'+self.grant['image_version'])
        response=connection.getresponse();self.assertEqual(response.status,200)
        self.assertEqual(sha(response.read()),self.grant['manifest_digest']);connection.close()

    def test_tls_handshake_has_total_deadline_before_header_processing(self):
        from ha_artifact_registry.ha_artifact_registry.tls import ephemeral_context
        context=ephemeral_context(self.root.parent)
        server=make_server(('127.0.0.1',0),self.store,tls_context=context)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def cleanup():server.shutdown();server.server_close();thread.join(timeout=2)
        self.addCleanup(cleanup)
        start=time.monotonic()
        with socket.create_connection(('127.0.0.1',server.server_port),timeout=4) as channel:
            channel.settimeout(4);self.assertEqual(channel.recv(1),b'')
        self.assertLess(time.monotonic()-start,3.9)

    def test_policy_owner_private_ancestors_links_and_uid_drift_fail_closed(self):
        from ha_artifact_registry.ha_artifact_registry.service import read
        parent=self.root.parent;path=parent/'policy.json';path.write_bytes(b'{}');path.chmod(0o600)
        self.assertEqual(read(path),b'{}')
        path.chmod(0o644)
        with self.assertRaises(RegistryError):read(path)
        path.chmod(0o600);os.link(path,parent/'hardlink')
        with self.assertRaises(RegistryError):read(path)
        (parent/'hardlink').unlink()
        link=parent/'linked';link.symlink_to(parent,target_is_directory=True)
        with self.assertRaises(OSError):read(link/'policy.json')
        parent.chmod(0o755)
        with self.assertRaises(RegistryError):read(path)
        parent.chmod(0o700)
        with patch('ha_artifact_registry.ha_artifact_registry.service.os.getuid',return_value=self.store.uid+1):
            with self.assertRaisesRegex(RegistryError,'uid'):self.store.authenticate('Bearer '+self.writer,'writer')

    def test_policy_before_after_fstat_detects_mutation(self):
        from ha_artifact_registry.ha_artifact_registry.service import read
        path=self.root.parent/'policy.json';path.write_bytes(b'{}');path.chmod(0o600)
        original=os.fstat;seen=[]
        def changed(fd):
            value=original(fd)
            if value.st_mode&0o170000==0o100000:
                seen.append(1)
                if len(seen)>1:
                    values={key:getattr(value,key) for key in dir(value) if key.startswith('st_')}
                    values['st_mtime_ns']+=1
                    return SimpleNamespace(**values)
            return value
        with patch('ha_artifact_registry.ha_artifact_registry.service.os.fstat',side_effect=changed):
            with self.assertRaises(RegistryError):read(path)

    def template(self):
        path=Path(__file__).resolve().parents[1]/'ha_artifact_registry/canary-template/main.py'
        spec=importlib.util.spec_from_file_location('registry_canary_fixture',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_independent_canary_template_b_c_fault_and_local_marker_restore(self):
        canary=self.template();data=self.root.parent/'canary-data';variant_path=self.root.parent/'variant.json'
        source=self.grant['source_digest'];environment={'CODEX_RELEASE_SOURCE_DIGEST':source,'CODEX_RELEASE_VERSION':'0.2.0'}
        b={'variant':'B','service_version':'0.2.0','expected_health_failure':False}
        c={'variant':'C','service_version':'0.3.0','expected_health_failure':True}
        with patch.dict('os.environ',environment):
            variant_path.write_bytes(canonical(b));canary.prepare(data,b)
            before=canary.health(data,variant_path)
            self.assertTrue(before['healthy']);self.assertEqual(before['source_digest'],source)
            self.assertEqual((data/'canary-marker.txt').stat().st_mode&0o777,0o600)
            original=(data/'canary-marker.txt').read_bytes()
            variant_path.write_bytes(canonical(c));canary.prepare(data,c)
            with patch.dict('os.environ',{'CODEX_RELEASE_VERSION':'0.3.0'}):
                fault=canary.health(data,variant_path)
            self.assertFalse(fault['healthy']);self.assertNotEqual(fault['marker_sha256'],before['marker_sha256'])
            self.assertEqual(fault['failure_code'],'canary_expected_health_failure')
            # Local data exercise only; actual Supervisor restore is a separate gate.
            canary.write_marker(data,original);variant_path.write_bytes(canonical(b))
            self.assertEqual(canary.health(data,variant_path),before)

    def test_canary_template_metadata_mismatch_fails_closed(self):
        canary=self.template();data=self.root.parent/'canary-data';variant_path=self.root.parent/'variant.json'
        b={'variant':'B','service_version':'0.2.0','expected_health_failure':False}
        variant_path.write_bytes(canonical(b));canary.prepare(data,b)
        with patch.dict('os.environ',{'CODEX_RELEASE_SOURCE_DIGEST':'invalid','CODEX_RELEASE_VERSION':'0.2.0'}):
            with self.assertRaises(RuntimeError):canary.health(data,variant_path)
        with patch.dict('os.environ',{'CODEX_RELEASE_SOURCE_DIGEST':self.grant['source_digest'],'CODEX_RELEASE_VERSION':'0.3.0'}):
            with self.assertRaises(RuntimeError):canary.health(data,variant_path)


if __name__=='__main__':unittest.main()
