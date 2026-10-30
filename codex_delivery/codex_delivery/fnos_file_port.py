"""Fixed HTTPS fnOS file protocol candidate; no production OAuth or enrollment.

Only a trusted, independently qualified credential provider may supply a bearer.
No refresh, retry, redirect, arbitrary path, mount, overwrite or resume occurs.
Every wire request checks the shared absolute deadline before/after connect and
I/O. The enclosing FnosFileReplica supplies the hard parent-wait/thread fuse if
DNS, credential acquisition or any trusted library call fails to return.
"""
from __future__ import annotations

import http.client
import json
import math
import re
import secrets
import socket
import ssl
import threading
import time
import unicodedata
from urllib.parse import quote, urlsplit

from .encrypted_replica import MAGIC, OBJECT, OVERHEAD, ReplicaError
from .fnos_file_replica import FileBinding, UploadResult


PROXY = '/ogh/ac/w'
UPLOAD = '/ogh/ac/h/upload'
DOWNLOAD = '/ogh/ac/h/multiple-download'
TOKEN = re.compile(r'[A-Za-z0-9._~+/-]+={0,2}\Z')
HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


class FilePortError(ReplicaError):
    """Stable public code only; no bearer, download URL, path or raw error."""


def _fail(code='fnos_file_unavailable'):
    raise FilePortError(code) from None


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except Exception:
        _fail('fnos_file_protocol_invalid')


def _decode(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError()
            value[key] = item
        return value
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except Exception:
        _fail('fnos_file_protocol_invalid')


class FnosHTTPFilePort:
    """Administrator-fixed transport. Missing credential provider is disabled.

    binding references remain expected identities, not live qualification. The
    provider signature is provider(*, deadline)->raw access-token string; this
    module never reads sessions, secrets, environment variables or credentials
    from disk and never implements OAuth login/refresh. Production main/config
    intentionally do not construct or qualify this class.
    """
    def __init__(self, binding, endpoint, directory, credential_provider=None, *,
                 maxbytes=128*1024*1024+OVERHEAD, max_listing_bytes=1024*1024,
                 max_entries=4096, ssl_context=None):
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
            context = ssl_context or ssl.create_default_context()
            if (type(binding) is not FileBinding or not isinstance(endpoint, str)
                    or any(ord(c) < 33 or ord(c) > 126 for c in endpoint)
                    or parsed.scheme != 'https' or not parsed.hostname
                    or port is not None and not 1 <= port <= 65535
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
                    or not isinstance(directory, str) or len(directory.encode('utf-8')) > 1024
                    or re.fullmatch(r'/vol[1-9][0-9]*/[^/]+(?:/[^/]+)*', directory) is None
                    or any(part in {'.', '..'} for part in directory.split('/')[1:])
                    or unicodedata.normalize('NFC', directory) != directory
                    or any(ord(c) < 32 or ord(c) == 127 or c in '\\%?#' for c in directory)
                    or credential_provider is not None and not callable(credential_provider)
                    or type(maxbytes) is not int or not 1 <= maxbytes <= 512*1024*1024+OVERHEAD
                    or type(max_listing_bytes) is not int or not 1024 <= max_listing_bytes <= 4*1024*1024
                    or type(max_entries) is not int or not 1 <= max_entries <= 16384
                    or not isinstance(context, ssl.SSLContext) or not context.check_hostname
                    or context.verify_mode != ssl.CERT_REQUIRED):
                raise ValueError()
        except Exception:
            _fail('fnos_file_config_invalid')
        self._binding, self._parsed, self._port = binding, parsed, port
        self._directory, self._credentials, self._context = directory, credential_provider, context
        self._maxbytes, self._listing_limit, self._max_entries = maxbytes, max_listing_bytes, max_entries
        self._lock = threading.Lock()

    @property
    def binding(self):
        return self._binding

    @staticmethod
    def _remaining(deadline):
        if type(deadline) not in {int, float} or not math.isfinite(deadline):
            _fail('fnos_file_deadline')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _fail('fnos_file_deadline')
        return remaining

    def _token(self, deadline):
        self._remaining(deadline)
        if self._credentials is None:
            _fail('fnos_file_disabled')
        try:
            value = self._credentials(deadline=deadline)
        except BaseException:
            _fail('fnos_file_credentials_unavailable')
        self._remaining(deadline)
        if (type(value) is not str or not 16 <= len(value) <= 4096 or TOKEN.fullmatch(value) is None):
            _fail('fnos_file_credentials_unavailable')
        return value

    def _path(self, ref):
        if not isinstance(ref, str) or OBJECT.fullmatch(ref) is None:
            _fail('fnos_file_reference_invalid')
        return self._directory + '/' + ref

    def _wire(self, method, path, token, deadline, *, body=None, headers=None, limit=None, binary=False):
        self._remaining(deadline)
        connection = response = timer = None
        try:
            if not self._context.check_hostname or self._context.verify_mode != ssl.CERT_REQUIRED:
                _fail('fnos_file_config_invalid')
            connection = http.client.HTTPSConnection(self._parsed.hostname, self._port,
                timeout=self._remaining(deadline), context=self._context)
            # connect includes DNS/TCP/TLS. A late DNS result never authorizes a
            # late HTTP request, even if the enclosing caller has already left.
            connection.connect()
            remaining = self._remaining(deadline)
            if not self._context.check_hostname or self._context.verify_mode != ssl.CERT_REQUIRED:
                _fail('fnos_file_config_invalid')
            watched = connection.sock
            if watched is None:
                _fail()
            watched.settimeout(remaining)
            def abort():
                try:
                    watched.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(remaining, abort)
            timer.daemon = True
            timer.start()
            outgoing = {'Authorization':'Bearer '+token, 'Connection':'close', 'Accept-Encoding':'identity',
                        'Accept':'application/octet-stream' if binary else 'application/json'}
            outgoing.update(headers or {})
            if body is not None:
                outgoing['Content-Length'] = str(len(body))
            self._remaining(deadline)
            connection.request(method, path, body=body, headers=outgoing)
            self._remaining(deadline)
            response = connection.getresponse()
            self._remaining(deadline)
            if response.status != 200:
                _fail('fnos_file_http_rejected')
            values = {}
            pairs = response.getheaders()
            if len(pairs) > 80:
                _fail('fnos_file_framing_invalid')
            for name, value in pairs:
                key = name.lower()
                if (not HEADER.fullmatch(name) or key in values or len(value) > 8192
                        or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                    _fail('fnos_file_framing_invalid')
                values[key] = value
            maximum = self._listing_limit if limit is None else limit
            length = values.get('content-length')
            transfer = values.get('transfer-encoding')
            media = values.get('content-type', '').split(';')[0].strip().lower()
            # These exceptions are fixed endpoint contracts observed through
            # the official OAuth proxy, not general MIME sniffing/fallback.
            empty_upload = method == 'POST' and path == UPLOAD and length == '0' and transfer is None
            json_media = {'application/json', 'text/plain'} if method == 'POST' and path == DOWNLOAD else {'application/json'}
            if (length is not None and (not re.fullmatch(r'[0-9]{1,10}', length) or int(length) > maximum)
                    or transfer is not None and (transfer.lower() != 'chunked' or length is not None)
                    or values.get('content-encoding', 'identity').lower() != 'identity'
                    or not binary and not empty_upload and media not in json_media):
                _fail('fnos_file_framing_invalid')
            raw = response.read(maximum+1)
            self._remaining(deadline)
            if (len(raw) > maximum or length is not None and len(raw) != int(length)
                    or method == 'POST' and path == UPLOAD and not raw and not empty_upload):
                _fail('fnos_file_framing_invalid')
            return raw
        except FilePortError:
            raise
        except BaseException:
            _fail('fnos_file_unavailable')
        finally:
            if timer is not None:
                timer.cancel()
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()

    def _packets(self, raw, *, req=None, reqid=None):
        packets = _decode(raw)
        if not isinstance(packets, list) or not 1 <= len(packets) <= 128:
            _fail('fnos_file_protocol_invalid')
        for index, packet in enumerate(packets):
            if not isinstance(packet, dict):
                _fail('fnos_file_protocol_invalid')
            if ('errno' in packet and (type(packet['errno']) is not int or packet['errno'] != 0)
                    or packet.get('errmsg') not in {None, ''}
                    or 'result' in packet and (index != len(packets)-1 or packet['result'] != 'succ')
                    or req is not None and 'req' in packet and packet['req'] != req
                    or reqid is not None and 'reqid' in packet and packet['reqid'] != reqid):
                _fail('fnos_file_protocol_invalid')
        if packets[-1].get('result') != 'succ':
            _fail('fnos_file_protocol_invalid')
        return packets

    def _proxy(self, req, token, deadline, **fields):
        request_id = secrets.token_hex(8)
        body = _json(dict(req=req, reqid=request_id, **fields))
        raw = self._wire('POST', PROXY, token, deadline, body=body,
                         headers={'Content-Type':'application/json'})
        return self._packets(raw, req=req, reqid=request_id)

    def _size(self, ref, token, deadline):
        seen, selected = set(), None
        packets = self._proxy('file.ls', token, deadline, path=self._directory)
        for packet in packets:
            entries = packet.get('files', [])
            if not isinstance(entries, list):
                _fail('fnos_file_protocol_invalid')
            for entry in entries:
                if not isinstance(entry, dict):
                    _fail('fnos_file_protocol_invalid')
                name = entry.get('name')
                if (not isinstance(name, str) or not name or len(name.encode('utf-8')) > 255
                        or name in seen or name in {'.', '..'} or '/' in name or '\\' in name
                        or any(ord(c) < 32 or ord(c) == 127 for c in name)):
                    _fail('fnos_file_protocol_invalid')
                seen.add(name)
                if len(seen) > self._max_entries:
                    _fail('fnos_file_listing_limit')
                if name == ref:
                    selected = entry.get('size')
                    if (type(selected) is not int or not 1 <= selected <= self._maxbytes
                            or 'dir' in entry and entry['dir'] not in {0, False}):
                        _fail('fnos_file_object_invalid')
        self._remaining(deadline)
        return selected

    def _download_token(self, raw):
        document = _decode(raw)
        # Exact documented top-level token only, never a recursive token search.
        if (not isinstance(document, dict) or 'token' not in document
                or document.get('result', 'succ') != 'succ'
                or 'errno' in document and (type(document['errno']) is not int or document['errno'] != 0)
                or document.get('errmsg') not in {None, ''}):
            _fail('fnos_file_protocol_invalid')
        value = document['token']
        if (type(value) is not str or not 1 <= len(value) <= 2048
                or any(not 33 <= ord(c) <= 126 for c in value)):
            _fail('fnos_file_protocol_invalid')
        return value

    def get_exact(self, ref, *, deadline):
        target = self._path(ref)
        self._remaining(deadline)
        if not self._lock.acquire(blocking=False):
            _fail('fnos_file_busy')
        try:
            token = self._token(deadline)
            size = self._size(ref, token, deadline)
            if size is None:
                return None
            raw = self._wire('POST', DOWNLOAD, token, deadline,
                body=_json({'accessToken':token, 'paths':[target], 'noZipWhenSingleFile':True}),
                headers={'Content-Type':'application/json'})
            download_token = self._download_token(raw)
            path = DOWNLOAD + '?token=' + quote(download_token, safe='')
            data = self._wire('GET', path, token, deadline, limit=self._maxbytes, binary=True)
            if len(data) != size or not data.startswith(MAGIC):
                _fail('fnos_file_object_invalid')
            self._remaining(deadline)
            return data
        except FilePortError:
            raise
        except BaseException:
            _fail('fnos_file_protocol_invalid')
        finally:
            self._lock.release()

    def _upload_target(self, ref, packets, size):
        fields = {}
        for packet in packets:
            for name in ('uploadName', 'from', 'skip', 'completed'):
                if name in packet:
                    if name in fields and fields[name] != packet[name]:
                        _fail('fnos_file_protocol_invalid')
                    fields[name] = packet[name]
        for name in ('from', 'skip', 'completed'):
            if name in fields and (type(fields[name]) is not int or fields[name] < 0):
                _fail('fnos_file_protocol_invalid')
        if fields.get('skip', 0) not in {0, 1} or fields.get('completed', 0) > size:
            _fail('fnos_file_protocol_invalid')
        if fields.get('from', 0) != 0:
            _fail('fnos_file_resume_forbidden')
        candidate = fields.get('uploadName', ref)
        if not isinstance(candidate, str):
            _fail('fnos_file_upload_path_invalid')
        if candidate.startswith(self._directory + '/'):
            candidate = candidate[len(self._directory)+1:]
        if re.fullmatch(re.escape(ref) + r'(?:\.~#(?:0|[1-9][0-9]{0,5}))?', candidate) is None:
            _fail('fnos_file_upload_path_invalid')
        return self._directory + '/' + candidate, bool(fields.get('skip') or fields.get('completed'))

    def _upload_success(self, raw):
        if raw == b'':
            # Only _wire's exact upload endpoint / HTTP200 / CL0 contract can
            # reach this branch. This is not a replica durability receipt.
            return
        document = _decode(raw)
        if isinstance(document, dict):
            document = [document]
            raw = _json(document)
        self._packets(raw)

    def upload_once(self, ref, envelope, *, deadline):
        target = self._path(ref)
        if type(envelope) is not bytes or not 1 <= len(envelope) <= self._maxbytes or not envelope.startswith(MAGIC):
            _fail('fnos_file_object_invalid')
        self._remaining(deadline)
        if not self._lock.acquire(blocking=False):
            _fail('fnos_file_busy')
        try:
            token = self._token(deadline)
            packets = self._proxy('file.checkUpload', token, deadline,
                                  path=target, size=len(envelope), overwrite=0)
            upload_path, existing = self._upload_target(ref, packets, len(envelope))
            if existing:
                self._remaining(deadline)
                return UploadResult.EXISTING
            boundary = 'codex-fnos-' + secrets.token_hex(24)
            prefix = ('--'+boundary+'\r\nContent-Disposition: form-data; name="trim-upload-file"; '
                      'filename="'+ref+'"\r\nContent-Type: application/octet-stream\r\n\r\n').encode('ascii')
            body = prefix + envelope + ('\r\n--'+boundary+'--\r\n').encode('ascii')
            raw = self._wire('POST', UPLOAD, token, deadline, body=body,
                headers={'Content-Type':'multipart/form-data; boundary='+boundary,
                    'Trim-Path':quote(upload_path, safe=''), 'Trim-From':'0',
                    'Trim-Overwrite':'0', 'Trim-Mtim':str(int(time.time()))})
            self._upload_success(raw)
            self._remaining(deadline)
            return UploadResult.CREATED
        except FilePortError:
            raise
        except BaseException:
            _fail('fnos_file_protocol_invalid')
        finally:
            self._lock.release()
