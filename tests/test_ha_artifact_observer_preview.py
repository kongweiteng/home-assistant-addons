"""Optional preview hook over real local HTTP; no Docker or HA access."""
import http.client
from pathlib import Path
import sys
import threading
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ha_artifact_observer'))
from ha_artifact_observer.api import create_server

ROUTE='/internal/v1/work-preview/qualification'
SECRET='fixture-private-never-return'
MAC='a'*64
RAW=b'{"version":1,"nonce":"fixture","policy_ref":"p","policy_sha256":"sha256:fixture","service_instance_ref":"s","record_digest":"sha256:fixture"}'
BODY=b'{"proof":"original signed bytes", "version":1}\n'


class PreviewHookTests(unittest.TestCase):
    def setUp(self):
        self.calls=[];self.mode='ok';self.entered=threading.Event();self.release=threading.Event()
        owner=self
        class Hook:
            def qualify(self,raw,authorization,*,deadline):
                owner.calls.append((raw,authorization,deadline))
                if owner.mode=='raise':raise ValueError(SECRET)
                if owner.mode=='invalid':return BODY,'bad-'+SECRET
                if owner.mode=='late':
                    owner.entered.set();owner.release.wait(3)
                return BODY,MAC
        self.hook=Hook()
        self.server=create_server('127.0.0.1',0,preview_qualification=self.hook)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.release.set);self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)

    def request(self,*,route=ROUTE,headers=None,body=RAW,port=None):
        connection=http.client.HTTPConnection('127.0.0.1',port or self.server.server_port,timeout=3)
        connection.putrequest('POST',route)
        values=headers if headers is not None else [('Authorization','Bearer '+SECRET),('Content-Length',str(len(body))),('Content-Type','application/json')]
        for name,value in values:connection.putheader(name,value)
        connection.endheaders(body)
        response=connection.getresponse();result=response.status,dict(response.getheaders()),response.read()
        connection.close();return result

    def test_default_has_no_hook_and_other_POST_routes_remain_absent(self):
        server=create_server('127.0.0.1',0)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        self.assertEqual(self.request(port=server.server_port)[0],404)
        self.assertEqual(self.request(route='/v1/artifacts/target')[0],405)
        self.assertEqual(self.calls,[])

    def test_success_preserves_original_bytes_and_detached_MAC(self):
        start=time.monotonic();status,headers,body=self.request()
        self.assertEqual((status,body),(200,BODY))
        self.assertEqual(headers['X-Qualification-MAC'],MAC)
        self.assertEqual(headers['Content-Length'],str(len(BODY)))
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(self.calls[0][:2],(RAW,'Bearer '+SECRET))
        self.assertGreater(self.calls[0][2],start)
        self.assertLessEqual(self.calls[0][2],time.monotonic()+2)

    def test_missing_or_duplicate_authorization_rejected_before_hook(self):
        common=[('Content-Length',str(len(RAW))),('Content-Type','application/json')]
        for extra in ([],[('Authorization','Bearer x'),('Authorization','Bearer y')]):
            status,headers,body=self.request(headers=common+extra)
            self.assertEqual(status,401);self.assertNotIn('X-Qualification-MAC',headers)
        self.assertEqual(self.calls,[])

    def test_ambiguous_or_oversized_frame_rejected_before_hook(self):
        common=[('Authorization','Bearer '+SECRET),('Content-Length',str(len(RAW))),('Content-Type','application/json')]
        cases=[common+[('Content-Length',str(len(RAW)))],common+[('Content-Type','application/json')],
            common+[('Transfer-Encoding','')],common+[('Content-Encoding','identity')],
            [(k,'4097' if k=='Content-Length' else v) for k,v in common],
            [(k,'application/json; charset=utf-8' if k=='Content-Type' else v) for k,v in common],
            [(k,v) for k,v in common if k!='Content-Length']]
        for values in cases:
            status,headers,body=self.request(headers=values)
            self.assertEqual(status,400);self.assertNotIn('X-Qualification-MAC',headers)
        self.assertEqual(self.calls,[])

    def test_failure_or_invalid_hook_output_returns_no_MAC_or_exception(self):
        for mode in ('raise','invalid'):
            self.mode=mode;status,headers,body=self.request()
            self.assertEqual(status,503);self.assertNotIn('X-Qualification-MAC',headers)
            self.assertNotIn(SECRET.encode(),body)

    def test_late_hook_cannot_send_a_MAC_after_total_deadline(self):
        self.mode='late';start=time.monotonic()
        with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError)):
            self.request()
        self.assertTrue(self.entered.is_set());self.assertLess(time.monotonic()-start,2.7)
        self.release.set()


if __name__=='__main__':unittest.main()
