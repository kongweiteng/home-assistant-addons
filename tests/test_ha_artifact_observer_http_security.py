"""Real TCP framing and slow-header limits for the standalone observer."""
import http.client
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ha_artifact_observer'))
from ha_artifact_observer.api import create_server


class ObserverHTTPSecurityTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        def observe(target,authorization):
            self.calls.append((target,authorization))
            return {'observed':True}
        self.server=create_server('127.0.0.1',0,service=SimpleNamespace(enabled=True,observe=observe))
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self,headers=(),path='/v1/artifacts/canary-target'):
        with socket.create_connection(self.server.server_address,timeout=2) as connection:
            raw=('GET '+path+' HTTP/1.1\r\nHost: fixture\r\nAuthorization: Bearer fixture-token\r\n'
                 +''.join(name+': '+value+'\r\n' for name,value in headers)+'\r\n').encode('ascii')
            connection.sendall(raw)
            response=http.client.HTTPResponse(connection);response.begin()
            return response.status,response.read()

    def test_ambiguous_or_encoded_GET_is_rejected_before_observation(self):
        cases=(
            [('Content-Length','0'),('Content-Length','0')],
            [('Content-Length','0'),('Content-Length','1')],
            [('Content-Length','0, 0')],
            [('Content-Length','00')],
            [('Content-Length','1')],
            [('Transfer-Encoding','')],
            [('Transfer-Encoding','chunked')],
            [('Content-Encoding','')],
            [('Content-Encoding','identity')],
        )
        for headers in cases:
            with self.subTest(headers=headers):
                status,body=self.request(headers)
                self.assertEqual(status,400)
                self.assertIn(b'observer_body_rejected',body)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.request([('Transfer-Encoding','')],path='/healthz')[0],400)

    def test_bodyless_GET_remains_available_with_absent_or_single_zero_length(self):
        self.assertEqual(self.request()[0],200)
        self.assertEqual(self.request([('Content-Length','0')])[0],200)
        self.assertEqual(len(self.calls),2)

    def test_four_slow_request_lines_or_headers_expire_and_release_all_slots(self):
        clients=[];closed=set();started=time.monotonic()
        try:
            for index in range(4):
                connection=socket.create_connection(self.server.server_address,timeout=2)
                clients.append(connection)
                # Both reads happen before do_GET, including the request-line
                # read that precedes parse_request in the standard handler.
                connection.sendall(b'GET /v1/artifacts/canary-target' if index%2 else
                    b'GET /v1/artifacts/canary-target HTTP/1.1\r\nHost: fixture\r\nX-Slow: ')
                connection.setblocking(False)
            while len(closed)<4 and time.monotonic()-started<6.5:
                for index,connection in enumerate(clients):
                    if index in closed:continue
                    try:connection.send(b'x')
                    except (BrokenPipeError,ConnectionResetError):closed.add(index);continue
                    try:
                        if connection.recv(128)==b'':closed.add(index)
                    except BlockingIOError:pass
                    except ConnectionResetError:closed.add(index)
                time.sleep(.1)
            self.assertEqual(len(closed),4)
            self.assertLess(time.monotonic()-started,6.5)
            self.assertEqual(self.calls,[])
            self.assertEqual(self.request(path='/healthz')[0],200)
        finally:
            for connection in clients:connection.close()


if __name__=='__main__':unittest.main()
