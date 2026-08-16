"""Tests for the independent read-only HA Operations Broker canary."""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = pathlib.Path(__file__).resolve().parents[1]
ADDON = ROOT / "ha_operations_broker"
sys.path.insert(0, str(ADDON))

from operations_broker.api import create_server
from operations_broker.authorization import AuthorizationError
from operations_broker.contract import (
    PROPOSAL_HASH_FIELDS,
    canonical_json,
    sha256_text,
)
from operations_broker.manager_executor import ManagerExecutorClient, ManagerExecutorError
from operations_broker.service import preflight
from operations_broker.supervisor import SupervisorClient, SupervisorError


UTC = timezone.utc
NOW = datetime(2026, 7, 31, 12, 2, tzinfo=UTC)
OWNER_HASH = sha256_text("weixin:owner-example")


def make_envelope(
    action_type: str = "restart_addon",
    target: str = "example_addon",
    *,
    owner_hash: str = OWNER_HASH,
    risk_level: str | None = None,
    requires_backup: bool | None = None,
) -> dict:
    created = NOW - timedelta(minutes=2)
    expires = NOW + timedelta(minutes=8)
    risk = risk_level or ("L1" if action_type == "check_ha_config" else "L3")
    backup = requires_backup if requires_backup is not None else risk == "L3"
    proposal = {
        "version": 1,
        "action_id": "OPS-20260731-A1B2C3D4E5F6",
        "action_type": action_type,
        "target": target,
        "parameter_summary": {"version": "1.2.3"},
        "risk_level": risk,
        "requires_backup": backup,
        "expected_change": "Preflight the exact Home Assistant operation.",
        "validation_plan": ["Read current state", "Verify health after execution"],
        "rollback_plan": ["Stop before execution", "Keep the current version"],
        "created_at": created.isoformat(),
        "expires_at": expires.isoformat(),
        "state": "awaiting_approval",
    }
    parameter_hash = sha256_text(canonical_json(proposal["parameter_summary"]))
    proposal_hash = sha256_text(
        canonical_json({field: proposal[field] for field in PROPOSAL_HASH_FIELDS})
    )
    proposal["parameter_summary_hash"] = f"sha256:{parameter_hash}"
    proposal["proposal_hash"] = f"sha256:{proposal_hash}"
    return {
        "version": 1,
        "proposal": proposal,
        "approval": {
            "version": 1,
            "action_id": proposal["action_id"],
            "proposal_hash": proposal["proposal_hash"],
            "state": "approved",
            "approved_by_hash": owner_hash,
            "approved_at": (NOW - timedelta(minutes=1)).isoformat(),
            "expires_at": expires.isoformat(),
        },
    }


class FakeSupervisor:
    def __init__(self, *, postflight_state: str = "started") -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.postflight_state = postflight_state

    def addon_info(self, slug: str) -> dict:
        self.calls.append(("addon", slug))
        return {
            "slug": slug,
            "name": "Example",
            "state": self.postflight_state,
            "version": "1.2.3",
            "update_available": False,
        }

    def restart_addon(self, slug: str) -> None:
        self.calls.append(("restart", slug))

    def core_info(self) -> dict:
        self.calls.append(("core", None))
        return {"version": "2026.7.4", "state": "running"}


class FixedClock:
    def __call__(self) -> datetime:
        return NOW


class PackagingTests(unittest.TestCase):
    def test_required_files_and_minimum_permissions(self) -> None:
        for relative in (
            "config.yaml",
            "build.yaml",
            "Dockerfile",
            "run.sh",
            "README.md",
            "DOCS.md",
            "CHANGELOG.md",
            "operations_broker/contract.py",
            "operations_broker/supervisor.py",
            "operations_broker/service.py",
            "operations_broker/api.py",
            "operations_broker/authorization.py",
            "operations_broker/execution.py",
            "operations_broker/manager_executor.py",
            "operations_broker/passkeys.py",
            "operations_broker/ui.py",
            "../tests/fixtures/ha_operations_broker_options.json",
        ):
            self.assertTrue((ADDON / relative).is_file(), relative)
        config = (ADDON / "config.yaml").read_text(encoding="utf-8")
        api = (ADDON / "operations_broker" / "api.py").read_text(encoding="utf-8")
        supervisor = (ADDON / "operations_broker" / "supervisor.py").read_text(encoding="utf-8")
        package = (ADDON / "operations_broker" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('version: "0.5.1"', config)
        self.assertIn('server_version = "HAOperationsBroker/0.5.1"', api)
        self.assertNotIn('ha-operations-broker/0.1"', supervisor)
        self.assertIn('ha-operations-broker/0.5.1"', supervisor)
        self.assertIn('__version__ = "0.5.1"', package)
        self.assertIn("slug: ha_operations_broker", config)
        self.assertIn("hassio_api: true", config)
        self.assertIn("hassio_role: manager", config)
        self.assertIn("boot: manual", config)
        self.assertIn("stage: experimental", config)
        self.assertIn("ingress: true", config)
        self.assertIn("ingress_port: 8098", config)
        self.assertIn("panel_admin: true", config)
        self.assertRegex(config, r"(?ms)ports:\n\s+8098/tcp: null")
        self.assertRegex(config, r"(?m)^\s+execution_enabled: false$")
        self.assertRegex(config, r"(?m)^\s+enabled_actions: \[\]$")
        self.assertRegex(config, r"(?m)^\s+restart_addon_allowlist: \[\]$")
        self.assertIn('recovery_api_token: ""', config)
        self.assertIn('backup_evidence_api_token: ""', config)
        self.assertRegex(config, r"(?m)^\s+manager_shadow_enabled: false$")
        self.assertIn('manager_executor_base_url: ""', config)
        self.assertIn('manager_executor_api_token: ""', config)
        self.assertIn('adapter_version: "manager-restart-v1"', config)
        for forbidden in (
            "homeassistant_api: true",
            "hassio_role: backup",
            "hassio_role: admin",
            "host_network: true",
            "privileged:",
            "full_access: true",
            "map:",
        ):
            self.assertNotIn(forbidden, config)

    def test_runtime_fails_closed_and_does_not_echo_tokens(self) -> None:
        run = (ADDON / "run.sh").read_text(encoding="utf-8")
        self.assertIn('"${#BROKER_API_TOKEN}" -lt 32', run)
        self.assertIn('"${#BROKER_RECOVERY_API_TOKEN}" -lt 32', run)
        self.assertIn('"${#BROKER_BACKUP_EVIDENCE_API_TOKEN}" -lt 32', run)
        self.assertIn('"$BROKER_API_TOKEN" = "$BROKER_RECOVERY_API_TOKEN"', run)
        self.assertIn('"$BROKER_RECOVERY_API_TOKEN" = "$BROKER_BACKUP_EVIDENCE_API_TOKEN"', run)
        self.assertIn('BROKER_MANAGER_SHADOW_ENABLED', run)
        self.assertIn('BROKER_SUPERVISOR_BASE_URL="http://supervisor"', run)
        self.assertIn("unset SUPERVISOR_TOKEN", run)
        self.assertNotIn("set -x", run)
        self.assertNotIn('echo "$BROKER_API_TOKEN', run)
        self.assertNotIn('echo "$BROKER_RECOVERY_API_TOKEN', run)
        self.assertNotIn('echo "$BROKER_BACKUP_EVIDENCE_API_TOKEN', run)
        self.assertNotIn('echo "$SUPERVISOR_TOKEN', run)
        self.assertNotIn('echo "$BROKER_PASSKEY_ENROLLMENT_TOKEN', run)

        dockerfile = (ADDON / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('FIDO2_VERSION="2.2.1"', dockerfile)
        self.assertIn(
            'FIDO2_SHA256="ed397da981b9ab133da6ead7309e41f924b566b749956129efe286fae097749f"',
            dockerfile,
        )
        self.assertIn("pip install --no-cache-dir --no-deps", dockerfile)

    def test_source_has_only_fixed_execution_and_no_arbitrary_process_capability(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ADDON / "operations_broker").glob("*.py")
        ).lower()
        for forbidden in (
            "subprocess",
            "os.system",
            "shell=true",
            "docker.sock",
            "method=\"put\"",
            "method=\"delete\"",
            "execute_shell",
            "run_command",
            "delete_path",
            "write_file",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertIn('method="get"', combined)
        supervisor = (ADDON / "operations_broker/supervisor.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('method="POST"', supervisor)
        self.assertIn('/addons/{slug}/restart', supervisor)

    def test_broker_recovery_resolution_is_not_exposed_as_controller_mcp(self) -> None:
        controller = ROOT / "codex_controller" / "codex_controller"
        for relative in ("mcp_proxy.py", "tool_proxy.py"):
            source = (controller / relative).read_text(encoding="utf-8")
            self.assertNotIn("recovery-resolution", source)
            self.assertNotIn("confirmed_healthy", source)
            self.assertNotIn("compensated", source)
            self.assertNotIn("ha_operations_recovery", source)


class PreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.supervisor = FakeSupervisor()
        self.owners = frozenset({OWNER_HASH})

    def run_preflight(self, envelope: dict) -> dict:
        return preflight(
            envelope,
            trusted_owner_hashes=self.owners,
            supervisor=self.supervisor,
            clock=FixedClock(),
        )

    def test_valid_addon_preflight_observes_without_execution(self) -> None:
        result = self.run_preflight(make_envelope())
        self.assertEqual(result["decision"], "preflight_observed")
        self.assertEqual(result["authorization_assurance"], "structural_only")
        self.assertFalse(result["execution_allowed"])
        self.assertEqual(result["observation"]["kind"], "addon_info")
        self.assertEqual(self.supervisor.calls, [("addon", "example_addon")])
        rendered = json.dumps(result)
        self.assertNotIn("parameter_summary", rendered)
        self.assertNotIn("approved_by_hash", rendered)

    def test_valid_core_preflight_requires_exact_target(self) -> None:
        result = self.run_preflight(
            make_envelope("check_ha_config", "home-assistant-core")
        )
        self.assertEqual(result["decision"], "preflight_observed")
        self.assertEqual(self.supervisor.calls, [("core", None)])
        blocked = self.run_preflight(make_envelope("check_ha_config", "wrong-core"))
        self.assertEqual(blocked["issues"][0]["code"], "target_mismatch")

    def test_hash_tampering_and_risk_downgrade_are_blocked(self) -> None:
        tampered = make_envelope()
        tampered["proposal"]["target"] = "other_addon"
        result = self.run_preflight(tampered)
        self.assertEqual(result["issues"][0]["code"], "proposal_hash_mismatch")

        downgraded = make_envelope(risk_level="L1", requires_backup=False)
        result = self.run_preflight(downgraded)
        self.assertEqual(result["issues"][0]["code"], "risk_mismatch")
        self.assertFalse(result["execution_allowed"])

    def test_untrusted_owner_expiry_and_unapproved_receipt_are_blocked(self) -> None:
        untrusted = self.run_preflight(make_envelope(owner_hash="b" * 64))
        self.assertEqual(untrusted["issues"][0]["code"], "owner_not_trusted")

        expired = make_envelope()
        expired["proposal"]["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
        expired["approval"]["expires_at"] = expired["proposal"]["expires_at"]
        proposal = expired["proposal"]
        proposal["proposal_hash"] = "sha256:" + sha256_text(
            canonical_json({field: proposal[field] for field in PROPOSAL_HASH_FIELDS})
        )
        expired["approval"]["proposal_hash"] = proposal["proposal_hash"]
        result = self.run_preflight(expired)
        self.assertEqual(result["issues"][0]["code"], "approval_expired")

        unapproved = make_envelope()
        unapproved["approval"]["state"] = "awaiting_confirmation"
        result = self.run_preflight(unapproved)
        self.assertEqual(result["issues"][0]["code"], "not_approved")

    def test_secret_parameters_and_path_targets_are_blocked(self) -> None:
        secret = make_envelope()
        secret["proposal"]["parameter_summary"] = {"api_token": "hidden"}
        result = self.run_preflight(secret)
        self.assertEqual(result["issues"][0]["code"], "sensitive_parameter")

        path_target = make_envelope(target="../../config")
        result = self.run_preflight(path_target)
        self.assertEqual(result["issues"][0]["code"], "invalid_target")

    def test_backup_and_hacs_actions_are_blocked_without_supervisor_calls(self) -> None:
        backup = self.run_preflight(make_envelope("create_backup", "home-assistant"))
        self.assertEqual(backup["issues"][0]["code"], "permission_not_granted")
        self.assertEqual(backup["future_required_role"], "backup")
        hacs = self.run_preflight(make_envelope("install_hacs", "example_repo"))
        self.assertEqual(hacs["issues"][0]["code"], "unsupported_canary")
        self.assertEqual(self.supervisor.calls, [])


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.payload


class SupervisorClientTests(unittest.TestCase):
    def test_client_uses_exact_get_and_redacts_options(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse(
                {
                    "result": "ok",
                    "data": {
                        "slug": "example_addon",
                        "name": "Example",
                        "state": "started",
                        "version": "1.2.3",
                        "options": {"password": "must-not-return"},
                        "network": {"8090/tcp": 8090},
                    },
                }
            )

        client = SupervisorClient(
            "supervisor-test-token",
            base_url="http://supervisor",
            timeout_seconds=7,
            opener=opener,
        )
        result = client.addon_info("example_addon")
        request, timeout = requests[0]
        self.assertEqual(request.full_url, "http://supervisor/addons/example_addon/info")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 7)
        self.assertNotIn("options", result)
        self.assertNotIn("network", result)
        self.assertNotIn("must-not-return", json.dumps(result))
        self.assertIs(result["installed"], True)

    def test_client_preserves_explicit_installed_false(self) -> None:
        client = SupervisorClient(
            "supervisor-test-token",
            opener=lambda *_args, **_kwargs: FakeResponse(
                {
                    "result": "ok",
                    "data": {
                        "slug": "example_addon",
                        "state": "stopped",
                        "version": "1.2.3",
                        "installed": False,
                    },
                }
            ),
        )
        self.assertIs(client.addon_info("example_addon")["installed"], False)

    def test_client_rejects_slug_before_network(self) -> None:
        client = SupervisorClient(
            "supervisor-test-token",
            opener=lambda *_args, **_kwargs: self.fail("network must not be called"),
        )
        with self.assertRaisesRegex(SupervisorError, "exact slug"):
            client.addon_info("../bad")

    def test_restart_uses_only_exact_post_endpoint(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse({"result": "ok", "data": {}})

        client = SupervisorClient(
            "supervisor-test-token",
            base_url="http://supervisor",
            timeout_seconds=7,
            opener=opener,
        )
        client.restart_addon("example_addon")
        request, timeout = requests[0]
        self.assertEqual(request.full_url, "http://supervisor/addons/example_addon/restart")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"{}")
        self.assertEqual(timeout, 7)

        with self.assertRaisesRegex(SupervisorError, "exact slug"):
            client.restart_addon("../bad")


class ManagerExecutorClientTests(unittest.TestCase):
    def proposal(self) -> dict:
        return {
            "action_id": "OPS-20260805-A1B2C3D4E5F6",
            "proposal_hash": "sha256:" + "a" * 64,
            "target": "example_addon",
            "adapter_version": "manager-restart-v1",
            "adapter_schema_version": 1,
            "baseline_etag": "sha256:" + "b" * 64,
        }

    def test_fixed_internal_shadow_request_and_response(self) -> None:
        seen = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                proposal = self_outer.proposal()
                return json.dumps(
                    {
                        "version": 1,
                        "mode": "shadow",
                        "action_id": proposal["action_id"],
                        "proposal_hash": proposal["proposal_hash"],
                        "action_type": "restart_addon",
                        "target": proposal["target"],
                        "adapter_version": proposal["adapter_version"],
                        "adapter_schema_version": proposal["adapter_schema_version"],
                        "baseline_etag": proposal["baseline_etag"],
                        "execution_allowed": False,
                        "observation": {
                            "slug": "example_addon",
                            "state": "started",
                            "version": "1.2.3",
                            "installed": True,
                        },
                    }
                ).encode()

        self_outer = self

        def opener(request, timeout):
            seen["method"] = request.get_method()
            seen["url"] = request.full_url
            seen["authorization"] = request.headers["Authorization"]
            seen["timeout"] = timeout
            return Response()

        client = ManagerExecutorClient(
            "http://ha-manager-executor:8099", "m" * 32, timeout_seconds=7, opener=opener
        )
        result = client.shadow_restart(self.proposal())
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(
            seen["url"],
            "http://ha-manager-executor:8099/internal/v1/shadow/restart-addon",
        )
        self.assertEqual(seen["authorization"], "Bearer " + "m" * 32)
        self.assertEqual(seen["timeout"], 7)

    def test_url_and_response_mismatch_fail_closed(self) -> None:
        for value in (
            "https://ha-manager-executor:8099",
            "http://127.0.0.1:8099",
            "http://ha-manager-executor:8099/path",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ManagerExecutorError):
                    ManagerExecutorClient(value, "m" * 32)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return json.dumps({"mode": "shadow", "execution_allowed": True}).encode()

        client = ManagerExecutorClient(
            "http://ha-manager-executor:8099",
            "m" * 32,
            opener=lambda *_args, **_kwargs: Response(),
        )
        with self.assertRaises(ManagerExecutorError) as raised:
            client.shadow_restart(self.proposal())
        self.assertEqual(raised.exception.code, "manager_shadow_mismatch")


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recovery_calls: list[tuple[str, dict]] = []

        def resolve_recovery(action_id: str, payload: dict) -> dict:
            if payload.get("resolution") == "compensated":
                raise AuthorizationError(
                    "recovery_already_resolved", "Execution recovery is already resolved"
                )
            self.recovery_calls.append((action_id, payload))
            return {
                "action_id": action_id,
                "state": "recovery_required",
                "recovery": {
                    "resolved": True,
                    "resolution": payload.get("resolution"),
                    "evidence_hash": payload.get("evidence_hash"),
                    "resolved_at": "2026-08-04T00:00:00+00:00",
                },
            }

        self.server = create_server(
            "127.0.0.1",
            0,
            api_token="a" * 32,
            recovery_api_token="r" * 32,
            backup_evidence_api_token="b" * 32,
            max_request_bytes=4096,
            preflight_handler=lambda payload: {
                "received_version": payload.get("version"),
                "execution_allowed": False,
            },
            execution_handler=lambda payload: {
                "action_id": payload.get("action_id"),
                "state": "succeeded",
            },
            execution_status_handler=lambda action_id: {
                "action_id": action_id,
                "state": "recovery_required",
                "recovery": {
                    "resolved": False,
                    "resolution": None,
                    "evidence_hash": None,
                    "resolved_at": None,
                },
            },
            recovery_resolution_handler=resolve_recovery,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_health_is_minimal_and_execution_disabled(self) -> None:
        with urlopen(self.base + "/healthz") as response:
            payload = json.loads(response.read())
        self.assertEqual(
            payload,
            {
                "status": "ok",
                "version": 6,
                "execution_enabled": False,
                "enabled_actions": [],
            },
        )

    def test_preflight_requires_bearer_and_json(self) -> None:
        unauthorized = Request(
            self.base + "/v1/preflight",
            data=b'{"version":1}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(unauthorized)
        self.assertEqual(context.exception.code, 401)

        authorized = Request(
            self.base + "/v1/preflight",
            data=b'{"version":1}',
            headers={
                "Authorization": "Bearer " + "a" * 32,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(authorized) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["received_version"], 1)
        self.assertFalse(payload["execution_allowed"])

    def test_execution_routes_require_bearer(self) -> None:
        payload = {
            "version": 1,
            "receipt_id": "RCPT-" + "A" * 32,
            "action_id": "OPS-20260731-A1B2C3D4E5F6",
            "proposal_hash": "sha256:" + "a" * 64,
            "idempotency_key": "sha256:" + "b" * 64,
        }
        unauthorized = Request(
            self.base + "/v1/executions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(unauthorized)
        self.assertEqual(context.exception.code, 401)

        authorized = Request(
            self.base + "/v1/executions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + "a" * 32,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(authorized) as response:
            result = json.loads(response.read())
        self.assertEqual(result["state"], "succeeded")

        status = Request(
            self.base + "/v1/executions/OPS-20260731-A1B2C3D4E5F6",
            headers={"Authorization": "Bearer " + "a" * 32},
        )
        with urlopen(status) as response:
            result = json.loads(response.read())
        self.assertEqual(result["action_id"], payload["action_id"])
        self.assertFalse(result["recovery"]["resolved"])

    def test_recovery_resolution_requires_bearer_and_forwards_exact_action(self) -> None:
        action_id = "OPS-20260731-A1B2C3D4E5F6"
        payload = {
            "version": 1,
            "resolution": "confirmed_healthy",
            "evidence_hash": "sha256:" + "c" * 64,
        }
        path = f"/v1/executions/{action_id}/recovery-resolution"
        unauthorized = Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(unauthorized)
        self.assertEqual(context.exception.code, 401)
        self.assertEqual(self.recovery_calls, [])

        ordinary_bearer = Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + "a" * 32,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(ordinary_bearer)
        self.assertEqual(context.exception.code, 401)
        self.assertEqual(self.recovery_calls, [])

        authorized = Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + "r" * 32,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(authorized) as response:
            result = json.loads(response.read())
        self.assertEqual(self.recovery_calls, [(action_id, payload)])
        self.assertEqual(result["state"], "recovery_required")
        self.assertTrue(result["recovery"]["resolved"])

    def test_recovery_resolution_conflict_maps_to_409(self) -> None:
        action_id = "OPS-20260731-A1B2C3D4E5F6"
        request = Request(
            self.base + f"/v1/executions/{action_id}/recovery-resolution",
            data=json.dumps(
                {
                    "version": 1,
                    "resolution": "compensated",
                    "evidence_hash": "sha256:" + "d" * 64,
                }
            ).encode(),
            headers={
                "Authorization": "Bearer " + "r" * 32,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request)
        self.assertEqual(context.exception.code, 409)
        error = json.loads(context.exception.read())
        self.assertEqual(error["error"]["code"], "recovery_already_resolved")


if __name__ == "__main__":
    unittest.main()
