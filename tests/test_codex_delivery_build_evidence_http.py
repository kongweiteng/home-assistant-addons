"""Actual trusted Delivery HTTP listener and Routes; projection is a fixture."""
import http.client
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace

from codex_delivery.codex_delivery.registry_build_routes import BuildEvidenceRoutes
from codex_delivery.codex_delivery.service import DeliveryService, ServiceConfig

TOKEN = 'fixture-build-evidence-' + 'e'*40
PATH = '/internal/v1/build-evidence/' + 'a'*64 + '/input'


class EvidenceHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        config = ServiceConfig.from_dict(dict(extension_bearer='x'*32, origin_key='y'*32,
            allowed_actors=['actor:'+'a'*64], worker_enabled=False))
        self.service = DeliveryService(config, Path(temp.name)/'state', address=('127.0.0.1', 0))
        self.addCleanup(self.service.close)
        self.reads = []
        def request(digest, deadline):
            self.reads.append(('scope', digest, deadline))
            return {'owner_ref': 'owner'}
        def input(digest, *, deadline):
            self.reads.append(('input', digest, deadline))
            return {'request_digest': digest, 'fixture': True}
        self.projection = SimpleNamespace(_request=request, input=input, artifact=input)
        self.routes = BuildEvidenceRoutes(self.projection, token=TOKEN, owner_refs=frozenset({'owner'}))
        self.service.server.build_evidence = self.routes
        self.service.start()

    def call(self, *, method='GET', path=PATH, headers=None, body=None, duplicates=None):
        client = http.client.HTTPConnection('127.0.0.1', self.service.server.server_port, timeout=3)
        self.addCleanup(client.close)
        if duplicates is None:
            client.request(method, path, body=body, headers=headers if headers is not None else {'Authorization': 'Bearer '+TOKEN})
        else:
            client.putrequest(method, path)
            for key, value in duplicates: client.putheader(key, value)
            client.endheaders()
        result = client.getresponse()
        return result.status, dict(result.getheaders()), json.loads(result.read())

    def test_exact_read_only_views_share_listener_and_original_deadline(self):
        before = time.monotonic()
        for stage in ('input', 'artifact'):
            status, headers, body = self.call(path=PATH.rsplit('/', 1)[0]+'/'+stage)
            self.assertEqual(status, 200)
            self.assertEqual(body, {'request_digest': 'sha256:'+'a'*64, 'fixture': True})
            self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(len(self.reads), 4)
        self.assertTrue(all(before < deadline <= time.monotonic()+5 for _, _, deadline in self.reads))

    def test_missing_registration_wrong_credentials_and_duplicate_auth_never_read(self):
        for headers in ({}, {'Authorization': 'Bearer '+'x'*32}, {'Authorization':'Bearer wrong'}):
            self.assertEqual(self.call(headers=headers)[0], 401)
        self.assertEqual(self.call(duplicates=[('Authorization', 'Bearer '+TOKEN)]*2)[0], 401)
        self.service.server.build_evidence = None
        self.assertEqual(self.call()[0], 503)
        self.assertEqual(self.reads, [])

    def test_closed_paths_methods_and_GET_framing_never_read(self):
        for suffix in ('?owner=owner', '/', '/extra', '#fragment'):
            self.assertEqual(self.call(path=PATH+suffix)[0], 404)
        self.assertEqual(self.call(method='POST', body='{}')[0], 405)
        for extra in [('Content-Length', '1'), ('Content-Encoding', 'identity'), ('Transfer-Encoding', 'chunked')]:
            headers = {'Authorization':'Bearer '+TOKEN, extra[0]:extra[1]}
            self.assertEqual(self.call(headers=headers)[0], 400)
        self.assertEqual(self.call(duplicates=[('Authorization','Bearer '+TOKEN),('Content-Length','0'),('Content-Length','0')])[0],400)
        self.assertEqual(self.reads, [])

    def test_owner_scope_rejection_and_projection_failure_are_not_success(self):
        self.routes.owners = frozenset({'other-owner'})
        self.assertEqual(self.call()[0], 503)
        self.assertEqual([stage for stage, _, _ in self.reads], ['scope'])
        self.routes.owners = frozenset({'owner'})
        def bad(*args, **kwargs): raise RuntimeError('PRIVATE must not appear')
        self.projection.input = bad
        status, _, body = self.call()
        self.assertEqual(status, 503)
        self.assertEqual(body['code'], 'registry_build_unavailable')
        self.assertNotIn('PRIVATE', json.dumps(body))


if __name__ == '__main__': unittest.main()
