from __future__ import annotations

from email.message import EmailMessage
import json
from pathlib import Path
import tempfile
import unittest

from codex_mail.codex_mail.config import ConfigError, MailConfig
from codex_mail.codex_mail.service import DeliveryError, MailService
from codex_mail.codex_mail.store import MailStore
from codex_controller.extensions import ExtensionRegistry
from codex_controller.skills import metadata


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "codex_mail"
MANIFEST = ROOT / "codex_controller/examples/extensions/mail/manifest.json"
SKILL = ROOT / "codex_controller/examples/skills/send-email/SKILL.md"


def options(**changes):
    value = {
        "smtp_username": "sender@qq.com",
        "smtp_authorization_code": "authorization-code",
        "sender_name": "家庭微信助手",
        "allowed_recipients": ["owner@example.com", "family@example.com"],
        "extension_bearer": "b" * 48,
    }
    value.update(changes)
    return value


def context(key: str = "a"):
    return {"job_hash": "1" * 64, "turn_hash": "2" * 64, "idempotency_key": "sha256:" + key * 64}


class ConfigTests(unittest.TestCase):
    def load(self, value):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "options.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return MailConfig.load(path)

    def test_loads_fixed_qq_service_and_allowlist(self):
        config = self.load(options())
        self.assertEqual(config.smtp_host, "smtp.qq.com")
        self.assertEqual(config.smtp_port, 465)
        self.assertEqual(config.allowed_recipients, {"owner@example.com", "family@example.com"})

    def test_rejects_non_qq_sender_and_missing_allowlist(self):
        with self.assertRaises(ConfigError):
            self.load(options(smtp_username="sender@example.com"))
        with self.assertRaises(ConfigError):
            self.load(options(allowed_recipients=[]))


class PackagingTests(unittest.TestCase):
    def test_addon_is_internal_only_and_marks_credentials_as_passwords(self):
        config = (ADDON / "config.yaml").read_text(encoding="utf-8")
        self.assertIn('version: "0.1.0"', config)
        self.assertIn("8129/tcp: null", config)
        self.assertNotIn("8129/tcp: 8129", config)
        self.assertIn("smtp_authorization_code: password", config)
        self.assertIn("extension_bearer: password", config)
        self.assertTrue((ADDON / "run.sh").stat().st_mode & 0o111)

    def test_manifest_uses_controller_closed_schemas(self):
        document = json.loads(MANIFEST.read_text(encoding="utf-8"))
        definitions = [
            ExtensionRegistry._tool("mail", "http://local-codex-mail:8129", "sha256:" + "0" * 64, tool, True)
            for tool in document["tools"]
        ]
        self.assertEqual([item.definition.name for item in definitions], ["ext_mail_send", "ext_mail_recent_status"])
        self.assertEqual([item.definition.risk_type for item in definitions], ["write", "read_only"])

    def test_skill_is_discoverable_and_does_not_treat_drafting_as_send(self):
        name, description = metadata(SKILL.read_text(encoding="utf-8"))
        body = SKILL.read_text(encoding="utf-8")
        self.assertEqual(name, "send-email")
        self.assertIn("微信助手", description)
        self.assertIn("不构成发送授权", body)
        self.assertIn("不要自动重发", body)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = MailConfig(
            "sender@qq.com", "authorization-code", "家庭微信助手",
            frozenset({"owner@example.com", "family@example.com"}), "b" * 48,
        )
        self.store = MailStore(Path(self.directory.name) / "mail.sqlite3")
        self.sent: list[EmailMessage] = []

    def service(self, sender=None, checker=None):
        def success(_config, message):
            self.sent.append(message)
        return MailService(self.config, self.store, sender or success, checker or (lambda _c, _m: False))

    def test_sends_once_and_replays_same_result(self):
        service = self.service()
        args = {"to": ["owner@example.com"], "subject": "测试", "text_body": "正文"}
        first = service.send(args, context())
        second = service.send(args, context())
        self.assertEqual(first["status"], "sent")
        self.assertEqual(first, second)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["To"], "owner@example.com")
        self.assertEqual(self.sent[0]["Subject"], "测试")

    def test_rejects_unlisted_recipient_without_smtp(self):
        service = self.service()
        result = service.send({"to": ["other@example.com"], "subject": "测试", "text_body": "正文"}, context())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["verification"], "not_sent")
        self.assertEqual(result["error_code"], "recipient_not_allowed")
        self.assertEqual(self.sent, [])

    def test_definite_failure_is_not_retried(self):
        calls = 0
        def fail(_config, _message):
            nonlocal calls
            calls += 1
            raise DeliveryError("smtp_auth_failed", True)
        service = self.service(sender=fail)
        args = {"to": ["owner@example.com"], "subject": "测试", "text_body": "正文"}
        self.assertEqual(service.send(args, context("b"))["status"], "failed")
        self.assertEqual(service.send(args, context("b"))["status"], "failed")
        self.assertEqual(calls, 1)

    def test_unknown_reconciles_from_sent_folder_without_resend(self):
        calls = 0
        def uncertain(_config, _message):
            nonlocal calls
            calls += 1
            raise DeliveryError("delivery_uncertain", False)
        found = False
        def checker(_config, _message_id):
            return found
        service = self.service(sender=uncertain, checker=checker)
        args = {"to": ["owner@example.com"], "subject": "测试", "text_body": "正文"}
        self.assertEqual(service.send(args, context("c"))["status"], "unknown")
        found = True
        result = service.send(args, context("c"))
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["verification"], "sent_folder_confirmed")
        self.assertEqual(calls, 1)

    def test_recent_status_omits_body_and_can_reconcile(self):
        service = self.service()
        args = {"to": ["owner@example.com"], "cc": ["family@example.com"], "subject": "测试", "text_body": "秘密正文"}
        service.send(args, context("d"))
        item = service.recent_status()["items"][0]
        self.assertEqual(item["recipients"], ["owner@example.com", "family@example.com"])
        self.assertNotIn("text_body", item)
        self.assertNotIn("秘密正文", json.dumps(item, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
