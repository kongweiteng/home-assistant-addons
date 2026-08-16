"""Tests for the independent read-only HA Manager Executor shadow."""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = pathlib.Path(__file__).resolve().parents[1]
ADDON = ROOT / "ha_manager_executor"
sys.path.insert(0, str(ADDON))

from ha_manager_executor.api import create_server
from ha_manager_executor.contract import addon_baseline_etag
from ha_manager_executor.service import ShadowError, ShadowManager
from ha_manager_executor.supervisor import SupervisorClient, SupervisorError


TOKEN = "m" * 32
ACTION_ID = "OPS-20260805-A1B2C3D4E5F6"
PROPOSAL_HASH = "sha256:" + "a" * 64
OBSERVATION = {
    "slug": "example_addon",
    "state": "started",
    "version": "1.2.3",
    "version_latest": "1.2.3",
    "update_available": False,
    "available": True,
    "installed": True,
    "protected": False,
    "rating": 6,
    "hassio_role": "default",
    "hassio_api": False,
    "homeassistant_api": False,
    "host_network": False,
    "full_access": False,
}


def request_payload(**updates):
    value = {
        "version": 1,
        "action_id": ACTION_ID,
        "proposal_hash": PROPOSAL_HASH,
        "action_type": "restart_addon",
        "target": "example_addon",
        "adapter_version": "manager-restart-v1",
        "adapter_schema_version": 1,
        "baseline_etag": addon_baseline_etag(OBSERVATION),
    }
    value.update(updates)
    return value


class FakeSupervisor:
    def __init__(self, observation=None):
        self.observation = dict(observation or OBSERVATION)
        self.calls = []

    def addon_info(self, slug):
        self.calls.append(("GET", slug))
        return dict(self.observation)


class PackagingTests(unittest.TestCase):
    def test_metadata_and_files_are_fail_closed(self):
        for relative in (
            "config.yaml", "build.yaml", "Dockerfile", "run.sh", "README.md", "DOCS.md", "CHANGELOG.md",
            "ha_manager_executor/__init__.py", "ha_manager_executor/contract.py", "ha_manager_executor/supervisor.py",
            "ha_manager_executor/service.py", "ha_manager_executor/api.py", "ha_manager_executor/main.py",
        ):
            self.assertTrue((ADDON / relative).is_file(), relative)
        config = (ADDON / "config.yaml").read_text(encoding="utf-8")
        self.assertIn('version: "0.1.1"', config)
        self.assertIn("hassio_api: true", config)
        self.assertIn("hassio_role: manager", config)
        self.assertRegex(config, r"(?ms)ports:\n\s+8099/tcp: null")
        for forbidden in ("ingress: true", "host_network: true", "privileged:", "full_access: true", "homeassistant_api: true"):
            self.assertNotIn(forbidden, config)
        run = (ADDON / "run.sh").read_text(encoding="utf-8")
        self.assertIn('"${#MANAGER_API_TOKEN}" -lt 32', run)
        self.assertIn('MANAGER_SUPERVISOR_BASE_URL="http://supervisor"', run)
        self.assertIn("unset SUPERVISOR_TOKEN", run)
        self.assertNotIn("set -x", run)

    def test_source_has_no_supervisor_write_method(self):
        supervisor = (ADDON / "ha_manager_executor/supervisor.py").read_text(encoding="utf-8")
        combined = "\n".join(path.read_text(encoding="utf-8") for path in (ADDON / "ha_manager_executor").glob("*.py"))
        self.assertIn('method="GET"', supervisor)
        for forbidden in ('method="POST"', 'method="PUT"', 'method="PATCH"', 'method="DELETE"', "/addons/{slug}/restart"):
            self.assertNotIn(forbidden, supervisor)
        for forbidden in ("subprocess", "os.system", "shell=True"):
            self.assertNotIn(forbidden, combined)

    def test_chinese_documentation_exists(self):
        for relative in ("README.md", "DOCS.md", "CHANGELOG.md"):
            text = (ADDON / relative).read_text(encoding="utf-8")
            self.assertRegex(text, r"[\u4e00-\u9fff]")


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.supervisor = FakeSupervisor()
        self.manager = ShadowManager(supervisor=self.supervisor, restart_addon_allowlist=frozenset({"example_addon"}))

    def test_shadow_returns_equivalent_observation_without_write(self):
        result = self.manager.restart_addon(request_payload())
        self.assertEqual(result["mode"], "shadow")
        self.assertFalse(result["execution_allowed"])
        self.assertEqual(result["baseline_etag"], addon_baseline_etag(OBSERVATION))
        self.assertEqual(self.supervisor.calls, [("GET", "example_addon")])

    def test_additional_field_is_rejected_before_supervisor(self):
        with self.assertRaisesRegex(ShadowError, "fields") as raised:
            self.manager.restart_addon(request_payload(url="http://supervisor"))
        self.assertEqual(raised.exception.code, "invalid_fields")
        self.assertEqual(self.supervisor.calls, [])

    def test_target_and_adapter_are_exact(self):
        for payload, code in (
            (request_payload(target="other_addon"), "target_not_allowlisted"),
            (request_payload(adapter_version="free-form"), "adapter_mismatch"),
            (request_payload(action_type="start_addon"), "unsupported_action"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ShadowError) as raised:
                    self.manager.restart_addon(payload)
                self.assertEqual(raised.exception.code, code)

    def test_baseline_drift_and_invalid_state_fail_closed(self):
        with self.assertRaises(ShadowError) as raised:
            self.manager.restart_addon(request_payload(baseline_etag="sha256:" + "b" * 64))
        self.assertEqual(raised.exception.code, "baseline_drift")
        stopped = FakeSupervisor({**OBSERVATION, "state": "stopped"})
        manager = ShadowManager(supervisor=stopped, restart_addon_allowlist=frozenset({"example_addon"}))
        with self.assertRaises(ShadowError) as raised:
            manager.restart_addon(request_payload(baseline_etag=addon_baseline_etag({**OBSERVATION, "state": "stopped"})))
        self.assertEqual(raised.exception.code, "preflight_state_invalid")


class SupervisorClientTests(unittest.TestCase):
    def test_fixed_get_and_response_filtering(self):
        seen = {}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self, _limit): return json.dumps({"result": "ok", "data": {**OBSERVATION, "secret": "drop"}}).encode()
        def opener(request, timeout):
            seen["method"] = request.get_method()
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            return Response()
        result = SupervisorClient("s" * 32, opener=opener).addon_info("example_addon")
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["url"], "http://supervisor/addons/example_addon/info")
        self.assertNotIn("secret", result)

    def test_missing_installed_is_normalized_for_installed_info_endpoint(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self, _limit):
                return json.dumps(
                    {
                        "result": "ok",
                        "data": {
                            key: value
                            for key, value in OBSERVATION.items()
                            if key != "installed"
                        },
                    }
                ).encode()

        result = SupervisorClient(
            "s" * 32, opener=lambda *_args, **_kwargs: Response()
        ).addon_info("example_addon")
        self.assertIs(result["installed"], True)
        self.assertEqual(addon_baseline_etag(result), addon_baseline_etag(OBSERVATION))

    def test_timeout_and_large_response_have_stable_errors(self):
        def timeout_opener(*_args, **_kwargs): raise URLError("down")
        with self.assertRaises(SupervisorError) as raised:
            SupervisorClient("s" * 32, opener=timeout_opener).addon_info("example_addon")
        self.assertEqual(raised.exception.code, "supervisor_unavailable")
        class LargeResponse:
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self, _limit): return b"x" * (1024 * 1024 + 1)
        with self.assertRaises(SupervisorError) as raised:
            SupervisorClient("s" * 32, opener=lambda *_a, **_k: LargeResponse()).addon_info("example_addon")
        self.assertEqual(raised.exception.code, "supervisor_response_too_large")


class ApiTests(unittest.TestCase):
    def setUp(self):
        manager = ShadowManager(supervisor=FakeSupervisor(), restart_addon_allowlist=frozenset({"example_addon"}))
        self.server = create_server("127.0.0.1", 0, api_token=TOKEN, max_request_bytes=32768, restart_shadow_handler=manager.restart_addon, allowlist_count=1)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, payload, *, token=TOKEN):
        request = Request(self.base + "/internal/v1/shadow/restart-addon", data=json.dumps(payload).encode(), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_auth_success_and_health_are_minimal(self):
        status, result = self.request(request_payload())
        self.assertEqual(status, 200)
        self.assertFalse(result["execution_allowed"])
        status, result = self.request(request_payload(), token="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(result, {"error": {"code": "not_authorized"}})
        with urlopen(self.base + "/healthz", timeout=2) as response:
            health = json.loads(response.read())
        self.assertEqual(health, {"allowlist_count": 1, "mode": "shadow", "status": "ok", "version": 1, "write_enabled": False})

    def test_api_rejects_extra_field_and_drift(self):
        status, result = self.request(request_payload(path="/data"))
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "invalid_fields")
        status, result = self.request(request_payload(baseline_etag="sha256:" + "b" * 64))
        self.assertEqual(status, 409)
        self.assertEqual(result["error"]["code"], "baseline_drift")


if __name__ == "__main__":
    unittest.main()
