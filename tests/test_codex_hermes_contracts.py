from __future__ import annotations

import ast
import base64
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex_hermes_migration"


class CodexHermesContractTests(unittest.TestCase):
    def test_job_contract_has_stable_idempotency_and_privacy_fields(self) -> None:
        schema = json.loads((CONTRACTS / "codex_weixin_job_v1.schema.json").read_text())
        self.assertEqual(schema["properties"]["version"]["const"], 1)
        self.assertIn("message_id", schema["required"])
        self.assertIn("conversation_key", schema["required"])
        self.assertNotIn("capability_profile", schema["required"])
        self.assertEqual(
            schema["properties"]["capability_profile"]["enum"],
            ["owner", "member_read_only"],
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("owner_legacy", json.dumps(schema))
        serialized = json.dumps(schema).lower()
        for forbidden in ("token", "cookie", "password", "user_id"):
            self.assertNotIn(forbidden, serialized)

    def test_ledger_catalog_keeps_single_versioned_tool_surface(self) -> None:
        catalog = json.loads((CONTRACTS / "renovation_ledger_tools_v1.json").read_text())
        self.assertEqual(catalog["format_id"], "kanhuwan-renovation-ledger@1")
        names = [item["name"] for item in catalog["tools"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("ledger_add_payment", names)
        self.assertIn("ledger_add_refund", names)
        self.assertIn("ledger_verify_export", names)
        for tool in catalog["tools"]:
            if tool["write"]:
                self.assertIn("idempotency_key", tool["required"])

    def test_v2_ledger_catalog_is_indexed_and_receipt_schema_matches_runtime(self) -> None:
        contracts_readme = (CONTRACTS / "README.md").read_text(encoding="utf-8")
        self.assertIn("renovation_ledger_tools_v2.json", contracts_readme)

        catalog = json.loads((CONTRACTS / "renovation_ledger_tools_v2.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["version"], 2)
        self.assertEqual(catalog["format_id"], "kanhuwan-renovation-ledger")
        self.assertEqual(catalog["format_version"], 2)

        receipt_schema = json.loads(
            (CONTRACTS / "ha_operations_receipt_v1.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            receipt_schema["properties"]["receipt_id"],
            {"type": "string", "pattern": r"^RCPT-[A-F0-9]{32}$"},
        )

    def test_hub_catalog_keeps_legacy_contract_and_idempotent_writes(self) -> None:
        catalog = json.loads((CONTRACTS / "renovation_hub_tools_v1.json").read_text())
        self.assertEqual(catalog["version"], 1)
        self.assertEqual(catalog["legacy_contract"], "renovation_ledger_tools_v1.json")
        names = {item["name"] for item in catalog["tools"]}
        self.assertIn("renovation_dashboard", names)
        self.assertIn("renovation_event_create", names)
        for tool in catalog["tools"]:
            if tool["write"]:
                self.assertIn("idempotency_key", tool["required"])

    def test_app_server_fixture_preserves_explicit_device_auth_without_mixing_modes(self) -> None:
        messages = [json.loads(line) for line in (FIXTURES / "app_server_transcript.jsonl").read_text().splitlines()]
        methods = [message.get("method") for message in messages]
        self.assertIn("initialize", methods)
        self.assertIn("account/login/start", methods)
        login = next(message for message in messages if message.get("method") == "account/login/start")
        self.assertEqual(login["params"], {"type": "chatgptDeviceCode"})
        self.assertNotIn("apiKey", json.dumps(messages))

    def test_all_fixtures_are_synthetic(self) -> None:
        for path in FIXTURES.iterdir():
            text = path.read_text()
            self.assertNotIn("sk-", text)
            self.assertNotIn("Bearer ", text)
            self.assertNotIn("/Users/", text)
            self.assertNotRegex(
                text,
                r"(?<!\d)(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
                r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?!\d)",
            )

    def test_codex_linux_platform_checksums_match_official_npm_lock(self) -> None:
        lock = json.loads((ROOT / "codex_controller" / "package-lock.json").read_text(encoding="utf-8"))
        dockerfile = (ROOT / "codex_controller" / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("registry.npmmirror.com", json.dumps(lock))
        for arch in ("x64", "arm64"):
            package = lock["packages"][f"node_modules/@openai/codex-linux-{arch}"]
            self.assertTrue(package["resolved"].startswith("https://registry.npmjs.org/@openai/codex/"))
            expected = base64.b64decode(package["integrity"].split("-", 1)[1]).hex()
            self.assertRegex(expected, r"^[a-f0-9]{128}$")
            self.assertIn(f'codex_sha512="{expected}"', dockerfile)
        self.assertEqual(len(re.findall(r'codex_sha512="[a-f0-9]{128}"', dockerfile)), 2)

    def test_codex_linux_platform_remote_adds_are_sha256_pinned(self) -> None:
        dockerfile = (ROOT / "codex_controller" / "Dockerfile").read_text(encoding="utf-8")
        expected = {
            "amd64": (
                "91a32565acb7fef5d300294577092a121503406e34ce7e992af8bdf1998b65fb",
                "linux-x64",
            ),
            "arm64": (
                "dee08f72d728189d9ac672356c6704e74e8c82e8c725feb8a0fbd1b6e0516724",
                "linux-arm64",
            ),
        }
        matches = re.findall(
            r"FROM scratch AS codex-(amd64|arm64)\n"
            r"ARG CODEX_VERSION\n"
            r"ADD --checksum=sha256:([a-f0-9]{64}) \\\n"
            r"\s+https://registry\.npmjs\.org/@openai/codex/-/"
            r"codex-\$\{CODEX_VERSION\}-(linux-(?:x64|arm64))\.tgz",
            dockerfile,
        )
        self.assertEqual({arch: (digest, platform) for arch, digest, platform in matches}, expected)
        self.assertEqual(dockerfile.count("ADD --checksum=sha256:"), 2)

    def test_gateway_cdn_boundary_matches_pinned_hermes_evidence(self) -> None:
        protocol_path = ROOT / "weixin_gateway" / "weixin_gateway" / "protocol.py"
        module = ast.parse(protocol_path.read_text(encoding="utf-8"))
        allowlist = None
        default_cdn = None
        for node in module.body:
            if not isinstance(node, ast.Assign):
                continue
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "WEIXIN_CDN_ALLOWLIST" in names:
                allowlist = set(ast.literal_eval(node.value))
            if "WEIXIN_CDN_BASE_URL" in names:
                default_cdn = ast.literal_eval(node.value)

        expected_allowlist = {
            "novac2c.cdn.weixin.qq.com",
            "ilinkai.weixin.qq.com",
            "wx.qlogo.cn",
            "thirdwx.qlogo.cn",
            "res.wx.qq.com",
            "mmbiz.qpic.cn",
            "mmbiz.qlogo.cn",
        }
        self.assertEqual(default_cdn, "https://novac2c.cdn.weixin.qq.com/c2c")
        self.assertEqual(allowlist, expected_allowlist)

        notice = (ROOT / "weixin_gateway" / "NOTICE.md").read_text(encoding="utf-8")
        self.assertIn("d0b87dad77944c669b453385bb797d53fa33c4f7", notice)
        self.assertIn("upload_full_url", notice)
        self.assertIn("upload_param", notice)
        self.assertIn("只接受 HTTPS", notice)
        for host in expected_allowlist:
            self.assertIn(host, notice)


if __name__ == "__main__":
    unittest.main()
