"""Fixed Controller transport for trusted workers; no redirect or write retry.

Ticket material lives only in this process. Unknown writes require status
reconciliation; neither an HTTP timeout nor an accepted event means sent/read.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import base64
import hashlib
import http.client
import json
import os
import re
from pathlib import Path
import socket
import ssl
import stat
import struct
import threading
import unicodedata
from typing import Any, Iterator
from urllib.parse import urlsplit

from .store import DeliveryError, SHANGHAI, canonical, reference
from .work import OUTPUT_NAME

INPUT_MIME_SUFFIXES = {
    'application/pdf': {'.pdf'}, 'application/zip': {'.zip'},
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': {'.xlsx'},
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': {'.docx'},
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': {'.pptx'},
    'text/plain': {'.txt'}, 'text/csv': {'.csv'}, 'image/png': {'.png'},
    'image/jpeg': {'.jpg', '.jpeg'}, 'image/webp': {'.webp'},
}


class CallbackError(DeliveryError):
    pass


class CallbackUnknown(CallbackError):
    """A write might have reached Controller; do not automatically repeat it."""


class ControllerCallback:
    def __init__(self, base_url: str, bearer: str, *, timeout: int = 10,
                 max_file_bytes: int = 5 * 1024 * 1024):
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except (ValueError, TypeError):
            raise CallbackError('callback_config_invalid') from None
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
                or not isinstance(bearer, str) or not 32 <= len(bearer) <= 4096
                or any(not 33 <= ord(ch) <= 126 for ch in bearer)
                or type(timeout) is not int or not 1 <= timeout <= 30
                or type(max_file_bytes) is not int or not 1 <= max_file_bytes <= 20 * 1024 * 1024):
            raise CallbackError('callback_config_invalid')
        self._scheme, self._host, self._port = parsed.scheme, parsed.hostname, port
        self._bearer = bearer
        self.timeout, self.max_file_bytes = timeout, max_file_bytes

    def _request(self, method: str, path: str, *, body: Any = None,
                 token: str | None = None, length: int | None = None,
                 mime: str = 'application/json', max_response_bytes: int = 65536) -> dict[str, Any]:
        response_limit = (14 * 1024 * 1024 if (method, path) == ('POST', '/internal/v1/work-attachments/resolve')
                          else 7 * 1024 * 1024 if (method, path) == ('POST', '/internal/v1/work-inputs/resolve') else 65536)
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= response_limit:
            raise CallbackError('callback_config_invalid')
        connection = (http.client.HTTPSConnection(self._host, self._port, timeout=self.timeout,
                      context=ssl.create_default_context()) if self._scheme == 'https'
                      else http.client.HTTPConnection(self._host, self._port, timeout=self.timeout))
        wire = canonical(body).encode() if isinstance(body, dict) else body
        headers = {'Authorization': 'Bearer ' + (token or self._bearer), 'Content-Type': mime}
        if length is not None:
            headers['Content-Length'] = str(length)
        elif wire is not None:
            headers['Content-Length'] = str(len(wire))
        expired = threading.Event()
        active_socket: list[Any] = []
        def abort() -> None:
            expired.set()
            active = active_socket[0] if active_socket else connection.sock
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
        deadline = threading.Timer(self.timeout, abort)
        deadline.daemon = True
        deadline.start()
        response = None
        try:
            # http.client deliberately does not follow redirects.
            connection.request(method, path, body=wire, headers=headers)
            if connection.sock is not None:
                active_socket.append(connection.sock)
            response = connection.getresponse()
            if response.status == 404:
                raise CallbackError('callback_not_found')
            if response.status in {401, 403}:
                raise CallbackError('callback_not_authorized')
            if response.status == 409:
                raise CallbackError('callback_conflict')
            if response.status >= 500:
                raise CallbackUnknown('callback_outcome_unknown')
            if not 200 <= response.status < 300:
                raise CallbackError('callback_rejected')
            content = response.read(max_response_bytes + 1)
            if expired.is_set():
                raise TimeoutError('callback_deadline')
            if len(content) > max_response_bytes:
                raise (CallbackUnknown if method in {'POST', 'PUT'} else CallbackError)('callback_response_invalid')
            result = json.loads(content)
            result = result.get('result', result) if isinstance(result, dict) else None
            if not isinstance(result, dict) or result.get('version') != 1:
                raise (CallbackUnknown if method in {'POST', 'PUT'} else CallbackError)('callback_response_invalid')
            return result
        except CallbackError:
            raise
        except Exception:
            if method in {'POST', 'PUT'}:
                raise CallbackUnknown('callback_outcome_unknown') from None
            raise CallbackError('callback_unavailable') from None
        finally:
            deadline.cancel()
            if response is not None:
                response.close()
            connection.close()

    @staticmethod
    def _scope(task: dict[str, Any]) -> dict[str, Any]:
        for field in ('owner_ref', 'conversation_ref', 'task_id'):
            reference(task[field])
        source = task.get('current_source_event_ref') or task['source_event_ref']
        reference(source)
        if type(task['input_revision']) is not int or task['input_revision'] < 1:
            raise CallbackError('callback_scope_invalid')
        return {'source_event_ref': source, 'actor_ref': task['owner_ref'],
                'conversation_ref': task['conversation_ref'], 'task_ref': task['task_id'],
                'input_revision': task['input_revision']}

    @contextmanager
    def _file(self, workspace: Path, filename: str) -> Iterator[Any]:
        if (not isinstance(filename, str) or not OUTPUT_NAME.fullmatch(filename)
                or filename in {'.', '..'}):
            raise CallbackError('artifact_path_invalid')
        directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 1 <= info.st_size <= self.max_file_bytes:
                    raise CallbackError('artifact_file_invalid')
                with os.fdopen(fd, 'rb', closefd=False) as source:
                    yield source
            finally:
                os.close(fd)
        finally:
            os.close(directory)

    def upload_file(self, task: dict[str, Any], workspace: Path, *, filename: str,
                    mime_type: str, expected_sha256: str) -> str:
        """Read a frozen trusted checkpoint, never the live mutable sandbox."""
        try:
            with self._file(workspace, filename) as source:
                before = os.fstat(source.fileno())
                sha256 = 'sha256:' + hashlib.file_digest(source, 'sha256').hexdigest()
                if sha256 != expected_sha256:
                    raise CallbackError('artifact_digest_invalid')
                source.seek(0)
                metadata = {'version': 1, **self._scope(task), 'filename': filename,
                            'mime_type': mime_type, 'size_bytes': before.st_size, 'sha256': sha256}
                if mime_type == 'image/png':
                    header = source.read(24)
                    source.seek(0)
                    if len(header) != 24 or header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR':
                        raise CallbackError('artifact_type_invalid')
                    width, height = struct.unpack('>II', header[16:24])
                    metadata.update(width=width, height=height)
                ticket = self._request('POST', '/internal/v1/work-uploads', body=metadata)
                upload = reference(ticket.get('upload_id'))
                if ticket.get('status') == 'stored':
                    return upload
                token = ticket.get('upload_token')
                if (not isinstance(token, str) or not 32 <= len(token) <= 4096
                        or any(not 33 <= ord(c) <= 126 for c in token)):
                    raise CallbackError('callback_response_invalid')
                result = self._request('PUT', '/internal/v1/work-uploads/' + upload,
                    body=source, token=token, length=before.st_size, mime=mime_type)
                after = os.fstat(source.fileno())
                if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (before.st_size, before.st_mtime_ns, before.st_ctime_ns):
                    raise CallbackError('artifact_checkpoint_changed')
                if result.get('upload_id') != upload or result.get('status') != 'stored':
                    raise CallbackUnknown('callback_outcome_unknown')
                return upload
        except CallbackError:
            raise
        except (OSError, ValueError):
            raise CallbackError('artifact_file_unavailable') from None

    def upload_status(self, upload_id: str) -> dict[str, Any]:
        return self._request('GET', '/internal/v1/work-uploads/' + reference(upload_id))

    def resolve_browser_grant(self, task: dict[str, Any], urls: list[str]) -> dict[str, Any]:
        """Reacquire an exact human-source grant before navigation; never cache."""
        if (not isinstance(urls, list) or not 1 <= len(urls) <= 64
                or any(not isinstance(url, str) or not 1 <= len(url) <= 4096
                    or any(not 33 <= ord(character) <= 126 for character in url) for url in urls)
                or len(set(urls)) != len(urls) or sum(map(len, urls)) > 32768):
            raise CallbackError('work_browser_request_invalid')
        try:
            scope = self._scope(task)
        except (KeyError, TypeError, DeliveryError):
            raise CallbackError('callback_scope_invalid') from None
        result = self._request('POST', '/internal/v1/work-browser-grants/resolve',
            body={'version': 1, **scope, 'urls': urls})
        if (set(result) != {'version', 'scope', 'urls', 'input_digest', 'grant_ref', 'expires_at'}
                or type(result['version']) is not int or result['version'] != 1
                or not isinstance(result['scope'], dict) or result['scope'] != scope
                or type(result['scope'].get('input_revision')) is not int
                or not isinstance(result['urls'], list) or result['urls'] != urls
                or not isinstance(result['input_digest'], str)
                or re.fullmatch(r'sha256:[a-f0-9]{64}', result['input_digest']) is None
                or not isinstance(result['grant_ref'], str) or re.fullmatch(r'BG-[a-f0-9]{32}', result['grant_ref']) is None
                or not isinstance(result['expires_at'], str)):
            raise CallbackError('work_browser_grant_invalid')
        try:
            expires = datetime.fromisoformat(result['expires_at'])
            instant = datetime.now(SHANGHAI)
            if (expires.utcoffset() != timedelta(hours=8) or expires.isoformat() != result['expires_at']
                    or not instant < expires <= instant + timedelta(seconds=300)):
                raise ValueError('grant_expiry_invalid')
        except (ValueError, TypeError, OverflowError):
            raise CallbackError('work_browser_grant_invalid') from None
        return result

    def resolve_inputs(self, task: dict[str, Any], refs: list[str]) -> dict[str, bytes]:
        """Resolve scoped JSON/upload namespaces, then return all inputs or fail."""
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 10
                or any(not isinstance(ref, str) or re.fullmatch(r'[A-Za-z0-9_-]{32,128}', ref) is None for ref in refs)
                or len(set(refs)) != len(refs)):
            raise CallbackError('work_input_request_invalid')
        snapshots = [ref for ref in refs if re.fullmatch(r'WI-[0-9a-f]{32}', ref)]
        attachments = [ref for ref in refs if ref not in snapshots]
        groups = []
        if snapshots:
            groups.append(self._resolve_json_inputs(task, snapshots))
        if attachments:
            groups.append(self._resolve_attachment_inputs(task, attachments))
        result, seen, total = {}, set(), 0
        for files in groups:
            for filename, content in files.items():
                key = unicodedata.normalize('NFC', filename).casefold()
                if key in seen:
                    raise CallbackError('work_input_filename_conflict')
                seen.add(key)
                total += len(content)
                if total > 10 * 1024 * 1024:
                    raise CallbackError('work_input_response_invalid')
                result[filename] = content
        if len(result) != len(refs):
            raise CallbackError('work_input_response_invalid')
        return result

    def _resolve_json_inputs(self, task: dict[str, Any], refs: list[str]) -> dict[str, bytes]:
        """Resolve bounded trusted snapshots, never arbitrary files or URLs."""
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 10
                or any(not isinstance(ref, str) or re.fullmatch(r'WI-[0-9a-f]{32}', ref) is None for ref in refs)
                or len(set(refs)) != len(refs)):
            raise CallbackError('work_input_request_invalid')
        try:
            scope = self._scope(task)
        except (KeyError, TypeError, DeliveryError):
            raise CallbackError('callback_scope_invalid') from None
        result = self._request('POST', '/internal/v1/work-inputs/resolve',
            body={'version': 1, **scope, 'input_refs': refs}, max_response_bytes=7 * 1024 * 1024)
        if (set(result) != {'version', 'inputs'} or type(result['version']) is not int or result['version'] != 1
                or not isinstance(result['inputs'], list) or len(result['inputs']) != len(refs)):
            raise CallbackError('work_input_response_invalid')
        files, seen, total = {}, set(), 0
        for item in result['inputs']:
            if (not isinstance(item, dict) or set(item) != {'ref', 'filename', 'mime_type', 'size_bytes', 'sha256', 'content_base64'}
                    or not isinstance(item['ref'], str) or item['ref'] not in refs or item['ref'] in seen
                    or item['filename'] != 'snapshot-' + item['ref'][3:] + '.json'
                    or item['mime_type'] != 'application/json' or type(item['size_bytes']) is not int
                    or not 1 <= item['size_bytes'] <= 512 * 1024 or not isinstance(item['sha256'], str)
                    or re.fullmatch(r'sha256:[a-f0-9]{64}', item['sha256']) is None
                    or not isinstance(item['content_base64'], str) or len(item['content_base64']) > 4 * ((512 * 1024 + 2) // 3)):
                raise CallbackError('work_input_response_invalid')
            try:
                raw = base64.b64decode(item['content_base64'], validate=True)
                if base64.b64encode(raw).decode('ascii') != item['content_base64']:
                    raise ValueError('noncanonical_base64')
                value = json.loads(raw)
                if not isinstance(value, dict) or canonical(value).encode() != raw:
                    raise ValueError('noncanonical_json')
                pending, count = [(value, 0)], 0
                while pending:
                    child, depth = pending.pop()
                    count += 1
                    if depth > 24 or count > 32768:
                        raise ValueError('bounded_json')
                    if isinstance(child, dict):
                        if any(re.search(r'password|passphrase|secret|token|authorization|cookie|private.?key|api.?key|bearer|credential|recovery.?code', key, re.I) for key in child):
                            raise ValueError('unsafe_json')
                        pending.extend((entry, depth + 1) for entry in child.values())
                    elif isinstance(child, list):
                        pending.extend((entry, depth + 1) for entry in child)
                    elif isinstance(child, str) and (self._bearer in child or re.search(r'bearer\s+\S+|basic\s+[A-Za-z0-9+/=]{16,}|-----BEGIN [^-]*PRIVATE KEY-----|[?&](?:token|secret|api[_-]?key|password)=', child, re.I)):
                        raise ValueError('unsafe_json')
            except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
                raise CallbackError('work_input_response_invalid') from None
            if len(raw) != item['size_bytes'] or 'sha256:' + hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise CallbackError('work_input_response_invalid')
            total += len(raw)
            if total > 5 * 1024 * 1024:
                raise CallbackError('work_input_response_invalid')
            seen.add(item['ref'])
            files[item['filename']] = raw
        if seen != set(refs):
            raise CallbackError('work_input_response_invalid')
        return files

    def _resolve_attachment_inputs(self, task: dict[str, Any], refs: list[str]) -> dict[str, bytes]:
        try:
            scope = self._scope(task)
        except (KeyError, TypeError, DeliveryError):
            raise CallbackError('callback_scope_invalid') from None
        result = self._request('POST', '/internal/v1/work-attachments/resolve',
            body={'version': 1, **scope, 'attachment_refs': refs}, max_response_bytes=14 * 1024 * 1024)
        if (set(result) != {'version', 'attachments'} or type(result['version']) is not int or result['version'] != 1
                or not isinstance(result['attachments'], list) or len(result['attachments']) != len(refs)):
            raise CallbackError('work_attachment_response_invalid')
        files, seen, names, total = {}, set(), set(), 0
        for item in result['attachments']:
            if (not isinstance(item, dict) or set(item) != {'ref', 'filename', 'mime_type', 'size_bytes', 'sha256', 'content_base64'}
                    or not isinstance(item['ref'], str) or item['ref'] not in refs or item['ref'] in seen
                    or not isinstance(item['filename'], str) or not item['filename'] or item['filename'] in {'.', '..'}
                    or '/' in item['filename'] or '\\' in item['filename']
                    or any(ord(character) < 32 or ord(character) == 127 for character in item['filename'])
                    or not isinstance(item['mime_type'], str) or item['mime_type'] not in INPUT_MIME_SUFFIXES or type(item['size_bytes']) is not int
                    or not 1 <= item['size_bytes'] <= 5 * 1024 * 1024 or not isinstance(item['sha256'], str)
                    or re.fullmatch(r'sha256:[a-f0-9]{64}', item['sha256']) is None
                    or not isinstance(item['content_base64'], str) or len(item['content_base64']) > 4 * ((5 * 1024 * 1024 + 2) // 3)):
                raise CallbackError('work_attachment_response_invalid')
            try:
                name = item['filename']
                if len(name.encode()) > 255 or Path(name).suffix.lower() not in INPUT_MIME_SUFFIXES[item['mime_type']]:
                    raise ValueError('filename_invalid')
                raw = base64.b64decode(item['content_base64'], validate=True)
                if base64.b64encode(raw).decode('ascii') != item['content_base64']:
                    raise ValueError('base64_invalid')
                prefixes = {'application/pdf': (b'%PDF-',), 'application/zip': (b'PK\x03\x04', b'PK\x05\x06'),
                    'image/png': (b'\x89PNG\r\n\x1a\n',), 'image/jpeg': (b'\xff\xd8',), 'image/webp': (b'RIFF',)}
                expected = prefixes.get(item['mime_type'], (b'PK\x03\x04',) if 'openxmlformats' in item['mime_type'] else ())
                if expected and not raw.startswith(expected):
                    raise ValueError('content_invalid')
                key = unicodedata.normalize('NFC', name).casefold()
                if key in names:
                    raise CallbackError('work_input_filename_conflict')
                names.add(key)
            except (ValueError, TypeError, UnicodeError):
                raise CallbackError('work_attachment_response_invalid') from None
            if len(raw) != item['size_bytes'] or 'sha256:' + hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise CallbackError('work_attachment_response_invalid')
            seen.add(item['ref'])
            files[name] = raw
            total += len(raw)
            if total > 10 * 1024 * 1024:
                raise CallbackError('work_attachment_response_invalid')
        if seen != set(refs):
            raise CallbackError('work_attachment_response_invalid')
        return files

    def event_status(self, event_id: str) -> dict[str, Any]:
        return self._request('GET', '/internal/v1/work-events/' + reference(event_id))

    def cancel_event(self, event_id: str) -> dict[str, Any]:
        """A confirmed suppression precedes the Gateway's send claim.

        Cancellation after the claim cannot promise that a message was withheld.
        An interrupted response is reconciled with event_status, never treated
        as a confirmed cancellation.
        """
        reference(event_id)
        response = self._request('POST', '/internal/v1/work-events/' + event_id + '/cancel', body={})
        if (response.get('event_id') != event_id
                or response.get('status') not in {'suppressed', 'unknown'}
                or response.get('state') not in {'suppressed', 'delivering', 'sent', 'unknown'}):
            raise CallbackUnknown('callback_outcome_unknown')
        return response

    def complete(self, task: dict[str, Any], *, event_id: str, result: str,
                 upload_ids: list[str]) -> dict[str, Any]:
        reference(event_id)
        if not isinstance(result, str) or not 1 <= len(result) <= 12000 or '\x00' in result:
            raise CallbackError('callback_result_invalid')
        if not isinstance(upload_ids, list) or len(upload_ids) > 4 or len(set(upload_ids)) != len(upload_ids):
            raise CallbackError('callback_result_invalid')
        for upload in upload_ids:
            reference(upload)
        response = self._request('POST', '/internal/v1/work-events', body={
            'version': 1, 'event_id': event_id, **self._scope(task), 'result': result, 'upload_ids': upload_ids})
        if response.get('event_id') != event_id or response.get('status') not in {'accepted', 'duplicate'}:
            raise CallbackUnknown('callback_outcome_unknown')
        return response
