from __future__ import annotations

import json
import os
from pathlib import Path
import unittest


class AppServerSchemaCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        schema_value = os.environ.get("CODEX_SCHEMA_DIR", "")
        if not schema_value:
            self.skipTest("CODEX_SCHEMA_DIR 未配置")
        self.schema_dir = Path(schema_value).resolve(strict=True)

    def load(self, relative: str) -> dict:
        document = json.loads((self.schema_dir / relative).read_text(encoding="utf-8"))
        self.assertIsInstance(document, dict)
        return document

    def test_controller_methods_and_fields_match_official_0146_schema(self) -> None:
        initialize = self.load("v1/InitializeParams.json")
        self.assertIn("clientInfo", initialize["required"])

        login = self.load("v2/LoginAccountParams.json")
        login_types = {
            variant["properties"]["type"]["enum"][0]
            for variant in login["oneOf"]
            if variant.get("properties", {}).get("type", {}).get("enum")
        }
        self.assertIn("chatgptDeviceCode", login_types)
        self.assertIn("apiKey", login_types)
        api_key_login = next(
            variant for variant in login["oneOf"]
            if variant.get("properties", {}).get("type", {}).get("enum") == ["apiKey"]
        )
        self.assertIn("apiKey", api_key_login["required"])

        login_response = self.load("v2/LoginAccountResponse.json")
        response_types = {
            variant["properties"]["type"]["enum"][0]
            for variant in login_response["oneOf"]
            if variant.get("properties", {}).get("type", {}).get("enum")
        }
        self.assertIn("apiKey", response_types)

        account = self.load("v2/GetAccountResponse.json")
        account_types = {
            variant["properties"]["type"]["enum"][0]
            for variant in account["definitions"]["Account"]["oneOf"]
        }
        self.assertIn("chatgpt", account_types)
        self.assertIn("apiKey", account_types)

        thread_start_document = self.load("v2/ThreadStartParams.json")
        thread_start = thread_start_document["properties"]
        for field in ("cwd", "sandbox", "approvalPolicy", "developerInstructions"):
            self.assertIn(field, thread_start)
        self.assertIn("read-only", thread_start_document["definitions"]["SandboxMode"]["enum"])

        thread_resume_document = self.load("v2/ThreadResumeParams.json")
        self.assertIn("threadId", thread_resume_document["required"])
        thread_resume = thread_resume_document["properties"]
        for field in ("cwd", "sandbox", "approvalPolicy", "developerInstructions"):
            self.assertIn(field, thread_resume)
        self.assertIn("read-only", thread_resume_document["definitions"]["SandboxMode"]["enum"])

        turn_start = self.load("v2/TurnStartParams.json")
        self.assertEqual(set(turn_start["required"]), {"input", "threadId"})
        for field in ("clientUserMessageId", "approvalPolicy"):
            self.assertIn(field, turn_start["properties"])
        turn_start_serialized = json.dumps(turn_start, ensure_ascii=False)
        for field in ('"localImage"', '"path"', '"detail"'):
            self.assertIn(field, turn_start_serialized)

        item_completed = self.load("v2/ItemCompletedNotification.json")
        self.assertTrue({"threadId", "turnId", "item"}.issubset(item_completed["required"]))
        turn_completed = self.load("v2/TurnCompletedNotification.json")
        self.assertTrue({"threadId", "turn"}.issubset(turn_completed["required"]))


if __name__ == "__main__":
    unittest.main()
