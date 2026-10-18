from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import xml.etree.ElementTree as ET

from codex_controller.extension_attachments import MAX_FILE_BYTES, encode_file, decode_files
from codex_controller.ledger_workbook import ledger_workbook, XLSX_MIME
from codex_controller.store import ControllerStore, StoreError
from codex_controller.extensions import ExtensionRegistry
from codex_controller.tool_proxy import ToolRouter, ToolProxyError
from tests.test_codex_controller import fixture_job
from codex_mail.codex_mail.config import MailConfig
from codex_mail.codex_mail.service import MailService
from codex_mail.codex_mail.store import MailStore


def snapshot() -> bytes:
    text = io.StringIO()
    fields = ['id', 'kind', 'status', 'amount_cents', 'date', 'vendor', 'description', 'tags_json']
    writer = csv.DictWriter(text, fieldnames=fields)
    writer.writeheader()
    for ident, kind, status, cents in [('1', 'payment', 'active', '12345'),
                                      ('2', 'refund', 'active', '2345'),
                                      ('3', 'payment', 'void', '99999')]:
        writer.writerow(dict(id=ident, kind=kind, status=status, amount_cents=cents, date='2026-10-18',
                             vendor='商家', description='=HYPERLINK("https://example.test")', tags_json='["材料"]'))
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w') as archive:
        archive.writestr('transactions.csv', text.getvalue())
    return target.getvalue()


class AttachmentTests(unittest.TestCase):
    def test_workbook_has_complete_typed_rows_and_reconciled_totals_without_formulas(self):
        result = ledger_workbook(snapshot())
        ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            summary = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            details = ET.fromstring(archive.read('xl/worksheets/sheet2.xml'))
            self.assertEqual(summary.find(".//s:c[@r='B2']/s:v", ns).text, '2')
            self.assertEqual(summary.find(".//s:c[@r='B5']/s:v", ns).text, '100')
            self.assertEqual(len(details.findall('.//s:row', ns)), 4)
            self.assertEqual(details.find(".//s:c[@r='D2']/s:v", ns).text, '123.45')
            self.assertEqual(details.find(".//s:c[@r='E3']/s:v", ns).text, '-23.45')
            self.assertEqual(details.find(".//s:c[@r='E4']/s:v", ns).text, '0')
            self.assertEqual(details.find(".//s:c[@r='G2']", ns).attrib['t'], 'inlineStr')
            self.assertEqual(details.findall('.//s:f', ns), [])
            self.assertIsNotNone(details.find('s:autoFilter', ns))
            self.assertIsNotNone(details.find('.//s:pane', ns))

    def test_limits_and_digest_before_transport(self):
        with self.assertRaises(ValueError):
            encode_file({'filename': 'big.txt', 'mime_type': 'text/plain'}, b'a' * (MAX_FILE_BYTES + 1))
        with self.assertRaises(ValueError):
            decode_files([{}] * 11)

    def test_upload_uses_nonconsuming_stream_only_after_job_scope_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ControllerStore(Path(temporary) / 'controller.sqlite3')
            reference = 'r' * 43
            content = b'%PDF-1.7\nprivate-content'
            digest = 'sha256:' + hashlib.sha256(content).hexdigest()
            payload = {**fixture_job('mail-upload'), 'capability_profile': 'owner', 'attachments': [
                {'attachment_ref': reference, 'media_type': 'file', 'size_bytes': len(content), 'sha256': digest}]}
            job = store.create_job(payload)
            store.claim_next()
            router = ToolRouter(store=store, gateway_base_url='http://local-gateway:8103', gateway_token='g' * 48)
            context = {'job_id': job['job_id'], 'conversation_key': payload['conversation_key']}
            meta = {'original_filename': 'file.pdf', 'mime_type': 'application/pdf', 'size_bytes': len(content), 'sha256': digest}
            with patch('codex_controller.tool_proxy._request_bytes', return_value=(meta, content)) as request:
                result = router._resolve_extension_attachments([{'source': 'upload', 'reference': reference}], context)
                self.assertEqual(decode_files(result)[0][1], content)
                self.assertTrue(request.call_args.args[1].endswith('/stream'))
                request.reset_mock()
                with self.assertRaises(ToolProxyError):
                    router._resolve_extension_attachments([{'source': 'upload', 'reference': 'z' * 43}], context)
                request.assert_not_called()

    def test_scoped_artifact_resolves_to_real_xlsx_and_rejects_other_conversation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ControllerStore(Path(temporary) / 'controller.sqlite3')
            payload = {**fixture_job('mail-artifact'), 'capability_profile': 'owner'}
            job = store.create_job(payload)
            store.claim_next()
            content = snapshot()
            meta = {'type': 'file', 'filename': 'ledger.zip', 'mime_type': 'application/zip',
                    'size_bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest(),
                    'summary': {}, 'result_summary': '完整快照'}
            artifact = store.capture_artifact(job['job_id'], meta, content)
            router = ToolRouter(store=store)
            context = {'job_id': job['job_id'], 'conversation_key': payload['conversation_key']}
            refs = [{'source': 'artifact', 'reference': artifact['artifact_id'], 'format': 'ledger_xlsx'}]
            files = decode_files(router._resolve_extension_attachments(refs, context))
            self.assertEqual(files[0][0]['mime_type'], XLSX_MIME)
            self.assertEqual(files[0][0]['filename'], '装修全部账单.xlsx')
            self.assertTrue(zipfile.is_zipfile(io.BytesIO(files[0][1])))
            with self.assertRaises(ToolProxyError):
                router._resolve_extension_attachments(refs, {**context, 'conversation_key': 'sha256:' + 'f' * 64})
            with self.assertRaises(ToolProxyError):
                router._resolve_extension_attachments(refs * 2, context)
            with self.assertRaises(StoreError):
                store.job_attachment(job['job_id'], 'unrelated-reference')

    def test_owner_tool_to_provider_delivers_workbook_once_with_validated_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ControllerStore(root / 'controller.sqlite3')
            payload = {**fixture_job('mail-complete-route'), 'capability_profile': 'owner'}
            job = store.create_job(payload)
            store.claim_next()
            store.assign_thread(job['job_id'], 'thread-mail-route')
            store.assign_turn(job['job_id'], 'turn-mail-route')
            content = snapshot()
            artifact = store.capture_artifact(job['job_id'], {
                'type': 'file', 'filename': 'ledger.zip', 'mime_type': 'application/zip',
                'size_bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest(),
                'summary': {}, 'result_summary': '完整快照'}, content)
            ext = root / 'extensions'
            secrets = root / 'secrets'
            (ext / 'mail').mkdir(parents=True)
            secrets.mkdir(mode=0o700)
            manifest_path = Path(__file__).resolve().parents[1] / 'codex_controller/examples/extensions/mail/manifest.json'
            manifest = manifest_path.read_bytes()
            (ext / 'mail/manifest.json').write_bytes(manifest)
            (ext / 'registry.json').write_text(json.dumps({'version': 1, 'extensions': [{
                'id': 'mail', 'enabled': True, 'allow_write': True, 'base_url': 'http://local-mail:8129',
                'manifest_sha256': 'sha256:' + hashlib.sha256(manifest).hexdigest()}]}))
            (secrets / 'mail.json').write_text(json.dumps({'bearer': 'b' * 48}))
            (secrets / 'mail.json').chmod(0o600)
            sent = []
            provider = MailService(MailConfig('sender@qq.com', 'fixture-code', '助手', frozenset({'owner@example.com'}), 'b' * 48),
                                   MailStore(root / 'mail.sqlite3'), lambda _config, message: sent.append(message))
            packets = []
            def request(_url, _token, packet):
                packets.append(packet)
                return provider.send(packet['arguments'], packet['context'], packet.get('attachments'))
            registry = ExtensionRegistry(ext, secrets, request=request)
            router = ToolRouter(store=store, extension_registry=registry)
            router.update_tool_policy('ext_mail_send', enabled=True, revision=store.tool_catalog_revision(), request_id='enable-mail-route')
            router.begin_job(job['job_id'], payload['message_id'], 'owner', conversation_key=payload['conversation_key'])
            router.bind_turn(job['job_id'], 'turn-mail-route')
            args = {'to': ['owner@example.com'], 'subject': '装修全部账单', 'text_body': '完整 Excel 见附件。',
                    'attachments': [{'source': 'artifact', 'reference': artifact['artifact_id'], 'format': 'ledger_xlsx'}]}
            first = router.call('ext_mail_send', args)
            self.assertEqual(first['status'], 'sent')
            self.assertEqual(first['attachments'][0]['filename'], '装修全部账单.xlsx')
            self.assertEqual(router.call('ext_mail_send', args), first)
            self.assertEqual(len(sent), 1)
            self.assertEqual(len(packets), 1)
            self.assertNotIn('content_base64', packets[0]['arguments']['attachments'][0])
            attached = list(sent[0].iter_attachments())[0]
            self.assertEqual(attached.get_content_type(), XLSX_MIME)
            self.assertTrue(zipfile.is_zipfile(io.BytesIO(attached.get_payload(decode=True))))
            self.assertNotIn('ext_mail_send', router.available_tools('member_read_only'))
            self.assertNotIn('ext_mail_send', router.available_tools('owner_legacy'))


if __name__ == '__main__':
    unittest.main()
