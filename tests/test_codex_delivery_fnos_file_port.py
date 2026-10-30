"""Real loopback HTTPS fnOS protocol fixtures, never real NAS qualification."""
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import re
import socket
import ssl
import tempfile
import threading
import time
import traceback
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from codex_delivery.codex_delivery.durability import Snapshot, digest
from codex_delivery.codex_delivery.encrypted_replica import EncryptedReplica, MAGIC
from codex_delivery.codex_delivery.fnos_file_replica import (
    FileBinding, FileReplicaError, FnosFileReplica, UploadResult,
)
from codex_delivery.codex_delivery.fnos_file_port import (
    DOWNLOAD, PROXY, UPLOAD, FilePortError, FnosHTTPFilePort,
)


BINDING = FileBinding('nas-http-fixture', 'objects-fixture', 'unqualified-fixture')
BEARER = 'fixture-oauth-bearer-'+'a'*40
DOWNLOAD_TOKEN = 'fixture-download-token/with?reserved&characters=1'
DIRECTORY = '/vol1/1000/fixture-objects'
FIXTURE_KEY = b'F'*32
REF = 'ER-'+'a'*64+'-'+'b'*64
ENVELOPE = MAGIC+b'opaque synthetic envelope for protocol fixture'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


class ProtocolFixture:
    def __init__(self):
        self.objects, self.calls, self.violations = {}, [], []
        self.listing, self.check, self.download_reply = None, None, {'token':DOWNLOAD_TOKEN}
        self.upload_reply = {'result':'succ'}
        self.modes = {}
        self.binary_override = None

    def handler(self):
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):
                pass
            def reply(self, value, *, status=200, binary=False, mode=None):
                raw = value if isinstance(value, bytes) else encoded(value)
                try:
                    if mode in {'slow_status', 'slow_headers'}:
                        initial = b'HTTP/1.1 200 ' if mode == 'slow_status' else b'HTTP/1.1 200 OK\r\nX-Slow: '
                        self.wfile.write(initial)
                        self.wfile.flush()
                        for _ in range(80):
                            time.sleep(0.02)
                            self.wfile.write(b'x')
                            self.wfile.flush()
                        return
                    self.send_response(status)
                    if mode != 'empty_upload':
                        self.send_header('Content-Type', 'text/plain;charset=utf-8' if mode == 'text_plain'
                                         else 'application/octet-stream' if binary else 'application/json')
                    if mode == 'chunked':
                        self.send_header('Transfer-Encoding', 'chunked')
                    else:
                        self.send_header('Content-Length', str(len(raw)))
                    if mode == 'duplicate_length':
                        self.send_header('content-length', str(len(raw)))
                    if mode == 'duplicate_type':
                        self.send_header('content-type', 'application/json')
                    if mode == 'conflicting_framing':
                        self.send_header('Transfer-Encoding', 'chunked')
                    if mode == 'gzip':
                        self.send_header('Content-Encoding', 'gzip')
                    if status == 302:
                        self.send_header('Location', '/credential-sink')
                    self.end_headers()
                    if mode == 'slow_body':
                        for value in raw:
                            self.wfile.write(bytes([value]))
                            self.wfile.flush()
                            time.sleep(0.02)
                    elif mode == 'chunked':
                        for index in range(0, len(raw), 9):
                            chunk = raw[index:index+9]
                            self.wfile.write(f'{len(chunk):x}\r\n'.encode()+chunk+b'\r\n')
                        self.wfile.write(b'0\r\n\r\n')
                    elif mode == 'truncated':
                        self.wfile.write(raw[:-1])
                        self.wfile.flush()
                        self.connection.shutdown(socket.SHUT_RDWR)
                    else:
                        self.wfile.write(raw)
                    self.wfile.flush()
                except (OSError, ssl.SSLError):
                    pass
            def do_POST(self):
                self.dispatch()
            def do_GET(self):
                self.dispatch()
            def dispatch(self):
                length = self.headers.get('Content-Length', '0')
                raw = self.rfile.read(int(length))
                parsed = urlsplit(self.path)
                fixture.calls.append((self.command, self.path, list(self.headers.raw_items()), raw))
                mode = fixture.modes.get((self.command, parsed.path))
                if self.headers.get_all('Authorization') != ['Bearer '+BEARER]:
                    self.reply({'result':'fail'}, status=401)
                    return
                if mode == 'reject':
                    self.reply({'result':'fail'}, status=403)
                    return
                if mode == 'redirect':
                    self.reply(b'not followed', status=302)
                    return
                if parsed.path == PROXY:
                    value = json.loads(raw)
                    if not re.fullmatch('[a-f0-9]{16}', value.get('reqid', '')):
                        fixture.violations.append('request id')
                    if value.get('req') == 'file.ls':
                        if set(value) != {'req','reqid','path'} or value['path'] != DIRECTORY:
                            fixture.violations.append('listing binding')
                        result = fixture.listing
                        if result is None:
                            files = [{'btim':1,'mtim':1,'name':name,'size':len(content),'uid':1000}
                                     for name,content in sorted(fixture.objects.items())]
                            result = [{'files':files[:1],'uver':3},{'files':files[1:],'uver':3},
                                      {'result':'succ','uver':3}]
                    elif value.get('req') == 'file.checkUpload':
                        if set(value) != {'req','reqid','path','size','overwrite'} or value['overwrite'] != 0:
                            fixture.violations.append('check fields')
                        if not value['path'].startswith(DIRECTORY+'/'):
                            fixture.violations.append('check binding')
                        basename = value['path'].rsplit('/',1)[-1]
                        if mode == 'lost_check_reply':
                            fixture.objects[basename+'.~#0'] = b''
                            self.connection.shutdown(socket.SHUT_RDWR)
                            self.connection.close()
                            return
                        result = fixture.check
                        if result is None:
                            result = [{'result':'succ','skip':1}] if basename in fixture.objects else [
                                {'result':'succ','uploadName':basename+'.~#0'}]
                    else:
                        fixture.violations.append('unknown proxy request')
                        result = [{'result':'fail'}]
                    self.reply(result, mode=mode)
                    return
                if parsed.path == UPLOAD:
                    content_type = self.headers.get('Content-Type')
                    message = BytesParser(policy=policy.default).parsebytes(
                        ('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
                    parts = list(message.iter_parts())
                    if len(parts) != 1 or parts[0].get_param('name',header='content-disposition') != 'trim-upload-file':
                        fixture.violations.append('multipart field')
                        self.reply({'result':'fail'})
                        return
                    filename = parts[0].get_filename()
                    content = parts[0].get_payload(decode=True)
                    if (self.headers.get('Trim-From') != '0' or self.headers.get('Trim-Overwrite') != '0'
                            or self.headers.get('Trim-Token') is not None
                            or not self.headers.get('Trim-Mtim','').isdigit()
                            or unquote(self.headers.get('Trim-Path','')) not in
                               {DIRECTORY+'/'+filename, DIRECTORY+'/'+filename+'.~#0'}):
                        fixture.violations.append('upload headers')
                    if filename not in fixture.objects:
                        fixture.objects[filename] = content
                    if mode == 'lost_reply':
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    self.reply(fixture.upload_reply, mode=mode)
                    return
                if parsed.path == DOWNLOAD and self.command == 'POST':
                    value = json.loads(raw)
                    if (set(value) != {'accessToken','paths','noZipWhenSingleFile'}
                            or value['accessToken'] != BEARER or value['noZipWhenSingleFile'] is not True
                            or len(value['paths']) != 1 or not value['paths'][0].startswith(DIRECTORY+'/')):
                        fixture.violations.append('download scope')
                    fixture.download_path = value['paths'][0]
                    self.reply(fixture.download_reply, mode=mode)
                    return
                if parsed.path == DOWNLOAD and self.command == 'GET':
                    if parse_qs(parsed.query) != {'token':[DOWNLOAD_TOKEN]}:
                        fixture.violations.append('download token encoding')
                    basename = fixture.download_path.rsplit('/',1)[-1]
                    content = fixture.objects[basename] if fixture.binary_override is None else fixture.binary_override
                    self.reply(content, binary=True, mode=mode)
                    return
                fixture.violations.append('unexpected route')
                self.reply({'result':'fail'}, status=404)
        return Handler


class FilePortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cert_directory = tempfile.TemporaryDirectory()
        root = Path(cls.cert_directory.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
            .sign(key,hashes.SHA256()))
        cert, key_path = root/'cert.pem', root/'key.pem'
        cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key_path.chmod(0o600)
        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(cert,key_path)
        cls.client_context = ssl.create_default_context(cafile=str(cert))

    @classmethod
    def tearDownClass(cls):
        cls.cert_directory.cleanup()

    def setUp(self):
        self.fixture = ProtocolFixture()
        self.server = ThreadingHTTPServer(('127.0.0.1',0),self.fixture.handler())
        self.server.socket = self.server_context.wrap_socket(self.server.socket,server_side=True)
        worker = threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        worker.start()
        def cleanup():
            self.server.shutdown()
            self.server.server_close()
            worker.join(2)
        self.addCleanup(cleanup)
        self.endpoint = 'https://localhost:'+str(self.server.server_port)
        self.credentials = []
        def provider(*,deadline):
            self.credentials.append(deadline)
            return BEARER
        self.provider = provider

    def port(self, **changes):
        options = {'maxbytes':2*1024*1024,'ssl_context':self.client_context, **changes}
        binding = options.pop('binding', BINDING)
        endpoint = options.pop('endpoint', self.endpoint)
        directory = options.pop('directory', DIRECTORY)
        provider = options.pop('credential_provider', self.provider)
        return FnosHTTPFilePort(binding,endpoint,directory,provider,**options)

    def invoke_get(self, ref=REF, port=None, seconds=2):
        return (port or self.port()).get_exact(ref,deadline=time.monotonic()+seconds)

    def invoke_upload(self, ref=REF, envelope=ENVELOPE, port=None, seconds=2):
        return (port or self.port()).upload_once(ref,envelope,deadline=time.monotonic()+seconds)

    def error(self, expected, invoke):
        with self.assertRaises(FilePortError) as caught:
            invoke()
        self.assertEqual(caught.exception.code,expected)
        formatted = ''.join(traceback.format_exception(caught.exception))
        for secret in (BEARER,DOWNLOAD_TOKEN):
            self.assertNotIn(secret,formatted)

    def test_real_HTTPS_upload_matches_observed_OAuth_proxy_and_zero_suffix(self):
        self.assertEqual(self.invoke_upload(), UploadResult.CREATED)
        self.assertEqual(self.fixture.objects[REF], ENVELOPE)
        calls = self.fixture.calls
        self.assertEqual([(c[0],c[1]) for c in calls],[('POST',PROXY),('POST',UPLOAD)])
        check = json.loads(calls[0][3])
        self.assertEqual(check['path'],DIRECTORY+'/'+REF)
        self.assertEqual(check['size'],len(ENVELOPE))
        self.assertEqual(check['overwrite'],0)
        self.assertEqual(self.fixture.violations,[])
        self.assertEqual(len(self.credentials),1)

    def test_exact_listing_and_authenticated_single_file_GET_returns_full_original_bytes(self):
        self.fixture.objects[REF] = ENVELOPE
        self.assertEqual(self.invoke_get(),ENVELOPE)
        self.assertEqual([call[1].split('?')[0] for call in self.fixture.calls], [PROXY,DOWNLOAD,DOWNLOAD])
        self.assertEqual(self.fixture.violations,[])
        self.assertEqual(len(self.credentials),1)
        self.assertNotIn(BEARER,self.fixture.calls[-1][1])

    def test_only_complete_exact_basename_absence_returns_None(self):
        self.fixture.objects[REF+'.~#0'] = b''
        self.assertIsNone(self.invoke_get())
        self.assertEqual(len(self.fixture.calls),1)

    def test_listing_real_shape_without_reqid_is_accepted_and_intermediate_packets_are_accumulated(self):
        self.fixture.objects[REF] = ENVELOPE
        self.fixture.listing = [{'files':[{'name':'unrelated','size':8,'btim':1,'mtim':1,'uid':1000}],'uver':1},
            {'files':[{'name':REF,'size':len(ENVELOPE),'btim':1,'mtim':1,'uid':1000}],'uver':1},
            {'result':'succ','uver':1}]
        self.assertEqual(self.invoke_get(),ENVELOPE)

    def test_incomplete_early_terminal_errors_duplicates_and_nonfinite_listing_never_mean_absence(self):
        cases = [[], {}, [{'files':[]}], [{'errno':0}], [{'result':'succ'},{'files':[]}],
            [{'result':'fail'}], [{'errno':403,'result':'succ'}], [{'errno':False,'result':'succ'}],
            [{'files':[{'name':'same'},{'name':'same'}]},{'result':'succ'}],
            b'[{"files":[],"files":[]},{"result":"succ"}]',
            b'[{"files":[],"uver":NaN},{"result":"succ"}]']
        for value in cases:
            with self.subTest(value=type(value).__name__):
                self.fixture.listing = value
                self.error('fnos_file_protocol_invalid',self.invoke_get)
        self.assertTrue(all(c[1]==PROXY for c in self.fixture.calls))

    def test_listing_target_size_and_bounds_are_strict(self):
        for value in (None, True, -1, 0, 2*1024*1024+1, '5'):
            self.fixture.listing = [{'files':[{'name':REF,'size':value}]},{'result':'succ'}]
            self.error('fnos_file_object_invalid',self.invoke_get)
        self.fixture.listing = [{'files':[{'name':'a'},{'name':'b'}]},{'result':'succ'}]
        self.error('fnos_file_listing_limit',lambda:self.invoke_get(port=self.port(max_entries=1)))
        self.fixture.listing = [{'files':[],'padding':'x'*2048},{'result':'succ'}]
        self.error('fnos_file_framing_invalid',lambda:self.invoke_get(port=self.port(max_listing_bytes=1024)))

    def test_no_redirect_refresh_or_authentication_retry(self):
        self.fixture.modes[('POST',PROXY)] = 'redirect'
        self.error('fnos_file_http_rejected',self.invoke_get)
        self.fixture.modes.clear()
        self.error('fnos_file_http_rejected',lambda:self.invoke_get(port=self.port(
            credential_provider=lambda **kwargs:'wrong-but-syntactically-valid-token')))
        self.assertEqual(len(self.fixture.calls),2)
        self.assertTrue(all(c[1]==PROXY for c in self.fixture.calls))

    def test_duplicate_headers_conflicting_framing_and_compression_are_rejected(self):
        for mode in ('duplicate_length','duplicate_type','conflicting_framing','gzip'):
            self.fixture.modes[('POST',PROXY)] = mode
            self.error('fnos_file_framing_invalid',self.invoke_get)

    def test_proper_chunked_framing_is_bounded_and_complete(self):
        self.fixture.objects[REF] = ENVELOPE
        self.fixture.modes[('POST',PROXY)] = 'chunked'
        self.fixture.modes[('GET',DOWNLOAD)] = 'chunked'
        self.assertEqual(self.invoke_get(),ENVELOPE)

    def test_download_cannot_return_a_zip_HTML_wrong_size_or_truncated_stream(self):
        self.fixture.objects[REF] = ENVELOPE
        for value in (b'PK'+b'x'*(len(ENVELOPE)-2), b'<html>', ENVELOPE[:-1]):
            self.fixture.binary_override = value
            self.error('fnos_file_object_invalid',self.invoke_get)
        self.fixture.binary_override = None
        self.fixture.modes[('GET',DOWNLOAD)] = 'truncated'
        with self.assertRaises(FilePortError):
            self.invoke_get()

    def test_download_token_only_comes_from_validated_root_field_and_never_an_external_URL(self):
        self.fixture.objects[REF] = ENVELOPE
        for reply in ({'data':{'token':'not accepted'}}, {'token':False}, {'token':'bad\nvalue'},
                      {'token':DOWNLOAD_TOKEN,'errno':401}, [{'token':DOWNLOAD_TOKEN}]):
            self.fixture.download_reply = reply
            self.error('fnos_file_protocol_invalid',self.invoke_get)
        self.assertTrue(all(c[0]=='POST' for c in self.fixture.calls))

    def test_skip_or_completed_returns_existing_without_upload_or_claiming_readback(self):
        for fields in ({'skip':1}, {'completed':len(ENVELOPE)}):
            self.fixture.check = [{'result':'succ', **fields}]
            self.assertEqual(self.invoke_upload(),UploadResult.EXISTING)
        self.assertEqual([c[1] for c in self.fixture.calls],[PROXY,PROXY])

    def test_uploadName_is_closed_to_same_directory_and_exact_zero_or_bounded_suffix(self):
        for name in (REF+'.~#01',REF+'.~#1000000','../'+REF,DIRECTORY+'/../'+REF,
                     '/vol2/1000/'+REF,REF+'%2fescape',REF+'\r\nInjected: value'):
            self.fixture.check = [{'result':'succ','uploadName':name}]
            self.error('fnos_file_upload_path_invalid',self.invoke_upload)
        for fields in ({'from':1},{'from':True},{'skip':2},{'completed':len(ENVELOPE)+1}):
            self.fixture.check = [{'result':'succ', **fields}]
            with self.assertRaises(FilePortError):
                self.invoke_upload()
        self.assertTrue(all(c[1]==PROXY for c in self.fixture.calls))

    def test_upload_positive_suffix_and_explicit_same_directory_are_valid_but_not_redirects(self):
        self.fixture.check = [{'uploadName':DIRECTORY+'/'+REF},{'from':0,'result':'succ'}]
        self.assertEqual(self.invoke_upload(),UploadResult.CREATED)
        self.assertEqual(self.fixture.violations,[])

    def test_upload_response_loss_is_one_attempt_then_real_core_exact_GET_settles(self):
        self.fixture.modes[('POST',UPLOAD)] = 'lost_reply'
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        root.chmod(0o700)
        port = self.port()
        replica = FnosFileReplica(port,FIXTURE_KEY,binding=BINDING,key_ref='fixture-key',
            failure_domain='fixture-nas-live',journal_path=root/'replica.json',timeout_seconds=3,maxbytes=65536)
        snapshot = Snapshot('EV-http-fixture',b'private AES source',digest(b'private AES source'))
        receipt = replica.save(snapshot)
        self.assertEqual(replica.get(receipt),snapshot)
        self.assertEqual(sum(c[1]==UPLOAD for c in self.fixture.calls),1)
        self.assertEqual(sum(c[1]==PROXY and json.loads(c[3])['req']=='file.checkUpload'
                             for c in self.fixture.calls),1)
        self.assertNotIn(snapshot.content,self.fixture.objects[receipt.object_ref])
        self.assertEqual(self.fixture.violations,[])

    def test_unknown_checkUpload_placeholder_stays_fused_across_restart_without_another_check_or_upload(self):
        self.fixture.modes[('POST',PROXY)] = 'lost_check_reply'
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        root.chmod(0o700)
        port = self.port()
        options = dict(binding=BINDING,key_ref='fixture-key',failure_domain='fixture-nas-live',
                       journal_path=root/'replica.json',timeout_seconds=2,maxbytes=65536)
        replica = FnosFileReplica(port,FIXTURE_KEY,**options)
        snapshot = Snapshot('EV-lost-precheck',b'private AES source',digest(b'private AES source'))
        with self.assertRaisesRegex(FileReplicaError,'^replica_write_unknown$'):
            replica.save(snapshot)
        first_calls = len(self.fixture.calls)
        with self.assertRaisesRegex(FileReplicaError,'^replica_write_unknown$'):
            replica.save(snapshot)
        self.assertEqual(len(self.fixture.calls),first_calls)
        restarted = FnosFileReplica(port,FIXTURE_KEY,**options)
        with self.assertRaisesRegex(FileReplicaError,'^replica_write_unknown$'):
            restarted.reconcile(snapshot)
        self.assertEqual(restarted.status()['state'],'fused')
        self.assertEqual(sum(c[1]==UPLOAD for c in self.fixture.calls),0)
        self.assertEqual(sum(c[1]==PROXY and json.loads(c[3])['req']=='file.checkUpload'
                             for c in self.fixture.calls),1)

    def test_core_hard_timeout_of_credential_provider_never_replaces_inflight_Port_or_sends_late_HTTP(self):
        entered,release = threading.Event(),threading.Event()
        self.addCleanup(release.set)
        def blocked_provider(*,deadline):
            entered.set()
            release.wait()
            return BEARER
        port = self.port(credential_provider=blocked_provider)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        root.chmod(0o700)
        replica = FnosFileReplica(port,FIXTURE_KEY,binding=BINDING,key_ref='fixture-key',
            failure_domain='fixture-nas-live',journal_path=root/'replica.json',timeout_seconds=0.25,maxbytes=65536)
        snapshot = Snapshot('EV-provider-timeout',b'private AES source',digest(b'private AES source'))
        started = time.monotonic()
        with self.assertRaisesRegex(FileReplicaError,'^replica_port_timeout$'):
            replica.save(snapshot)
        self.assertLess(time.monotonic()-started,0.9)
        self.assertTrue(entered.is_set())
        self.assertTrue(replica.status()['worker_active'])
        self.error('fnos_file_busy',lambda:self.invoke_get(port=port))
        with self.assertRaisesRegex(FileReplicaError,'^replica_port_fused$'):
            replica.reconcile(snapshot)
        release.set()
        for _ in range(100):
            if not replica.status()['worker_active']:
                break
            time.sleep(0.005)
        self.assertEqual(self.fixture.calls,[])
        with self.assertRaisesRegex(FileReplicaError,'^replica_port_fused$'):
            replica.save(snapshot)

    def test_undocumented_upload_success_body_is_unknown_never_a_second_wire_write(self):
        self.fixture.upload_reply = {'status':'possibly stored'}
        self.error('fnos_file_protocol_invalid',self.invoke_upload)
        self.assertEqual(sum(c[1]==UPLOAD for c in self.fixture.calls),1)
        self.assertEqual(self.fixture.objects[REF],ENVELOPE)

    def test_observed_empty_upload_and_text_plain_download_token_have_endpoint_specific_support(self):
        self.fixture.upload_reply = b''
        self.fixture.modes[('POST',UPLOAD)] = 'empty_upload'
        self.fixture.modes[('POST',DOWNLOAD)] = 'text_plain'
        self.assertEqual(self.invoke_upload(),UploadResult.CREATED)
        self.assertEqual(self.invoke_get(),ENVELOPE)
        self.assertEqual(self.fixture.violations,[])
        self.assertEqual(sum(c[1]==UPLOAD for c in self.fixture.calls),1)

    def test_text_plain_exception_never_applies_to_listing_or_non_JSON_download_tokens(self):
        self.fixture.modes[('POST',PROXY)] = 'text_plain'
        self.error('fnos_file_framing_invalid',self.invoke_get)
        self.fixture.modes.clear()
        self.fixture.objects[REF] = ENVELOPE
        self.fixture.download_reply = b'not JSON at all'
        self.fixture.modes[('POST',DOWNLOAD)] = 'text_plain'
        self.error('fnos_file_protocol_invalid',self.invoke_get)

    def test_slow_status_headers_and_body_are_aborted_at_total_wire_deadline(self):
        for mode in ('slow_status','slow_headers','slow_body'):
            self.fixture.modes[('POST',PROXY)] = mode
            start = time.monotonic()
            with self.assertRaises(FilePortError):
                self.invoke_get(seconds=0.12)
            self.assertLess(time.monotonic()-start,0.6)

    def test_connect_or_credentials_returning_after_deadline_never_send_HTTP(self):
        original = http.client.HTTPSConnection.connect
        def late(connection):
            time.sleep(0.12)
            original(connection)
        with patch.object(http.client.HTTPSConnection,'connect',late):
            self.error('fnos_file_deadline',lambda:self.invoke_get(seconds=0.06))
        def late_credentials(*,deadline):
            time.sleep(0.08)
            return BEARER
        self.error('fnos_file_deadline',lambda:self.invoke_get(seconds=0.04,
            port=self.port(credential_provider=late_credentials)))
        self.assertEqual(self.fixture.calls,[])

    def test_default_missing_provider_invalid_ref_and_expired_deadline_do_no_wire_work(self):
        self.error('fnos_file_disabled',lambda:self.invoke_get(port=self.port(credential_provider=None)))
        self.error('fnos_file_reference_invalid',lambda:self.invoke_get('../outside'))
        self.error('fnos_file_deadline',lambda:self.invoke_get(seconds=-1))
        self.error('fnos_file_object_invalid',lambda:self.invoke_upload(envelope=b'plain text'))
        self.assertEqual(self.fixture.calls,[])

    def test_configuration_rejects_HTTP_userinfo_paths_and_insecure_TLS(self):
        for endpoint in ('http://localhost:1','https://user:password@localhost',self.endpoint+'/path',
                         self.endpoint+'?q=1',self.endpoint+'#fragment','https://localhost:0'):
            self.error('fnos_file_config_invalid',lambda:self.port(endpoint=endpoint))
        for directory in ('/','/vol1','/vol1/../escape','/vol1/a/','/vol1/a//b','/vol1/a%2fb'):
            self.error('fnos_file_config_invalid',lambda:self.port(directory=directory))
        insecure = ssl._create_unverified_context()
        self.error('fnos_file_config_invalid',lambda:self.port(ssl_context=insecure))
        self.assertEqual(self.fixture.calls,[])

    def test_accidental_TLS_verification_drift_is_rejected_before_HTTP(self):
        context = ssl.create_default_context()
        port = self.port(ssl_context=context)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.error('fnos_file_config_invalid',lambda:self.invoke_get(port=port))
        self.assertEqual(self.fixture.calls,[])

    def test_wrong_server_certificate_fails_before_HTTP_without_secret_in_error(self):
        with self.assertRaises(FilePortError) as failure:
            self.invoke_get(port=self.port(ssl_context=ssl.create_default_context()))
        self.assertNotIn(BEARER,''.join(traceback.format_exception(failure.exception)))
        self.assertEqual(self.fixture.calls,[])

    def test_provider_failure_never_echoes_its_raw_secret_or_tries_login(self):
        def failed(*,deadline):
            raise OSError('private value '+BEARER)
        self.error('fnos_file_credentials_unavailable',lambda:self.invoke_get(port=self.port(credential_provider=failed)))
        self.assertEqual(self.fixture.calls,[])


if __name__ == '__main__':
    unittest.main()
