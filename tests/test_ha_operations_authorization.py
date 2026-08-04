"""Tests for the independent Passkey authorization root canary."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = pathlib.Path(__file__).resolve().parents[1]
ADDON = ROOT / "ha_operations_broker"
sys.path.insert(0, str(ADDON))

from operations_broker.api import create_server
from operations_broker.authorization import (
    AuthorizationError,
    AuthorizationManager,
    AuthorizationStore,
)
from operations_broker.contract import PROPOSAL_HASH_FIELDS, canonical_json, sha256_text
from operations_broker.execution import ExecutionManager
from operations_broker.supervisor import SupervisorError


UTC = timezone.utc
NOW = datetime(2026, 7, 31, 13, 0, tzinfo=UTC)
OWNER_HASH = sha256_text("weixin:owner-example")
ENROLLMENT_TOKEN = "enrollment-example-" + "x" * 32
INGRESS_ORIGIN = "https://ha.example.invalid"
ALLOWED_ADDON = "example_addon"


def idempotency(label: str) -> str:
    return "sha256:" + sha256_text(f"operations:{label}")


def make_intent(
    *, target: str = ALLOWED_ADDON, key: str = idempotency("restart")
) -> dict:
    return {
        "version": 1,
        "action_type": "restart_addon",
        "target": target,
        "idempotency_key": key,
    }


def make_envelope(*, target: str = "example_addon", expires_in: int = 600) -> dict:
    created = NOW - timedelta(minutes=2)
    expires = NOW + timedelta(seconds=expires_in)
    proposal = {
        "version": 1,
        "action_id": "OPS-20260731-A1B2C3D4E5F6",
        "action_type": "restart_addon",
        "target": target,
        "parameter_summary": {"version": "1.2.3"},
        "risk_level": "L3",
        "requires_backup": True,
        "expected_change": "Restart the exact Home Assistant add-on.",
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
            "approved_by_hash": OWNER_HASH,
            "approved_at": (NOW - timedelta(minutes=1)).isoformat(),
            "expires_at": expires.isoformat(),
        },
    }


class MutableClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakePasskeys:
    def __init__(self, *, registration_count: int = 0, authentication_count: int = 1):
        self.registration_count = registration_count
        self.authentication_count = authentication_count
        self.registration_states = []
        self.authentication_states = []

    def registration_begin(self, *, user_handle, existing_credentials):
        self.registration_states.append((user_handle, existing_credentials))
        return {
            "publicKey": {
                "challenge": "registration-challenge",
                "user": {"id": "user-handle"},
            }
        }, {"kind": "registration"}

    def registration_complete(self, *, state, response):
        if state != {"kind": "registration"} or response != {"ok": True}:
            raise ValueError("bad registration")
        return {
            "credential_id": b"credential-id-1",
            "credential_data": b"credential-data-1",
            "sign_count": self.registration_count,
        }

    def authentication_begin(self, *, credentials):
        self.authentication_states.append(credentials)
        return {
            "publicKey": {
                "challenge": "authentication-challenge",
                "allowCredentials": [],
            }
        }, {"kind": "authentication"}

    def authentication_complete(self, *, state, credentials, response):
        if state != {"kind": "authentication"} or response != {"ok": True}:
            raise ValueError("bad authentication")
        if credentials != [b"credential-data-1"]:
            raise ValueError("unexpected credential")
        return {
            "credential_id": b"credential-id-1",
            "sign_count": self.authentication_count,
        }


class FakeExecutionSupervisor:
    def __init__(
        self,
        *,
        postflight_state: str = "started",
        postflight_version: str = "1.2.3",
        block_restart: bool = False,
    ) -> None:
        self.postflight_state = postflight_state
        self.postflight_version = postflight_version
        self.block_restart = block_restart
        self.calls: list[tuple[str, str]] = []
        self.restart_started = threading.Event()
        self.restart_release = threading.Event()
        self.info_count = 0

    def addon_info(self, slug: str) -> dict:
        self.calls.append(("info", slug))
        self.info_count += 1
        return {
            "slug": slug,
            "state": "started" if self.info_count == 1 else self.postflight_state,
            "version": "1.2.3" if self.info_count == 1 else self.postflight_version,
            "installed": True,
        }

    def restart_addon(self, slug: str) -> None:
        self.calls.append(("restart", slug))
        self.restart_started.set()
        if self.block_restart and not self.restart_release.wait(timeout=3):
            raise SupervisorError("test_timeout", "test restart timed out")


class AuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = pathlib.Path(self.temporary.name) / "private" / "passkeys.sqlite3"
        self.clock = MutableClock()
        self.backend = FakePasskeys()
        self.store = AuthorizationStore(self.database)
        self.manager = AuthorizationManager(
            store=self.store,
            passkeys=self.backend,
            trusted_owner_hashes=frozenset({OWNER_HASH}),
            enrollment_token=ENROLLMENT_TOKEN,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON, "other_addon"}),
            clock=self.clock,
        )

    def register(self, user: str = "ha-user-example") -> None:
        begin = self.manager.begin_registration(
            remote_user_id=user, enrollment_token=ENROLLMENT_TOKEN
        )
        self.manager.complete_registration(
            remote_user_id=user,
            enrollment_token=ENROLLMENT_TOKEN,
            flow_id=begin["flow_id"],
            response={"ok": True},
        )

    def test_registration_requires_token_and_persists_no_raw_identity_or_token(self) -> None:
        with self.assertRaisesRegex(AuthorizationError, "denied"):
            self.manager.begin_registration(
                remote_user_id="ha-user-example", enrollment_token="wrong"
            )
        self.register()
        self.assertEqual(self.store.credential_count(), 1)
        self.assertEqual(stat.S_IMODE(os.stat(self.database).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.database.parent).st_mode), 0o700)
        database_bytes = self.database.read_bytes()
        self.assertNotIn(b"ha-user-example", database_bytes)
        self.assertNotIn(ENROLLMENT_TOKEN.encode(), database_bytes)

    def test_each_ha_user_gets_at_most_one_passkey(self) -> None:
        self.register()
        with self.assertRaisesRegex(AuthorizationError, "already"):
            self.manager.begin_registration(
                remote_user_id="ha-user-example", enrollment_token=ENROLLMENT_TOKEN
            )

    def test_request_is_idempotent_by_action_and_hash_but_rejects_conflict(self) -> None:
        first = self.manager.create_request(make_envelope())
        second = self.manager.create_request(make_envelope())
        self.assertEqual(first["approval_id"], second["approval_id"])
        conflict = make_envelope(target="other_addon")
        with self.assertRaisesRegex(AuthorizationError, "another proposal hash"):
            self.manager.create_request(conflict)

    def test_broker_native_proposal_is_immutable_and_idempotent(self) -> None:
        first = self.manager.create_proposal(make_intent())
        second = self.manager.create_proposal(make_intent())
        self.assertEqual(first, second)
        self.assertTrue(first["action_id"].startswith("OPS-20260731-"))
        self.assertEqual(first["action_type"], "restart_addon")
        self.assertEqual(first["target"], ALLOWED_ADDON)
        self.assertEqual(first["risk_level"], "L3")
        self.assertTrue(first["requires_backup"])
        self.assertEqual(first["state"], "awaiting_approval")
        self.assertEqual(first["parameter_summary"], {"idempotency_key": idempotency("restart")})
        self.assertFalse(first["execution_allowed"])

        with self.assertRaisesRegex(AuthorizationError, "another operation intent"):
            self.manager.create_proposal(
                make_intent(target="other_addon", key=idempotency("restart"))
            )

    def test_native_proposal_rejects_extra_fields_actions_and_slugs(self) -> None:
        extra = {**make_intent(), "parameters": {"anything": True}}
        with self.assertRaisesRegex(AuthorizationError, "fields are invalid"):
            self.manager.create_proposal(extra)
        unsupported = {**make_intent(), "action_type": "install_addon"}
        with self.assertRaisesRegex(AuthorizationError, "Only restart_addon"):
            self.manager.create_proposal(unsupported)
        with self.assertRaisesRegex(AuthorizationError, "exact slug"):
            self.manager.create_proposal(make_intent(target="../bad"))
        with self.assertRaisesRegex(AuthorizationError, "not allowlisted"):
            self.manager.create_proposal(make_intent(target="not_allowed"))

    def test_native_authorization_request_references_broker_proposal_only(self) -> None:
        proposal = self.manager.create_proposal(make_intent())
        request = self.manager.create_native_request(
            {"version": 1, "action_id": proposal["action_id"]}
        )
        self.assertEqual(request["proposal_hash"], proposal["proposal_hash"])
        self.assertEqual(request["proposal_origin"], "broker_native")
        self.assertEqual(request["state"], "pending")
        with self.assertRaisesRegex(AuthorizationError, "fields are invalid"):
            self.manager.create_native_request(
                {"version": 1, "action_id": proposal["action_id"], "owner_hash": OWNER_HASH}
            )

    def test_passkey_assertion_binds_request_and_never_enables_execution(self) -> None:
        self.register()
        request = self.manager.create_request(make_envelope())
        begin = self.manager.begin_authorization(
            approval_id=request["approval_id"], remote_user_id="ha-user-example"
        )
        result = self.manager.complete_authorization(
            approval_id=request["approval_id"],
            remote_user_id="ha-user-example",
            flow_id=begin["flow_id"],
            response={"ok": True},
        )
        receipt = result["receipt"]
        self.assertEqual(receipt["authorization_assurance"], "passkey_verified")
        self.assertEqual(receipt["proposal_hash"], request["proposal_hash"])
        self.assertFalse(receipt["execution_allowed"])
        self.assertFalse(result["execution_allowed"])
        status = self.manager.internal_status(request["approval_id"])
        self.assertEqual(status["request"]["state"], "authorized")
        self.assertFalse(status["execution_allowed"])

        ingress = self.manager.ingress_context(
            approval_id=request["approval_id"], remote_user_id="ha-user-example"
        )
        self.assertEqual(
            set(ingress["receipt"]),
            {"receipt_id", "authorized_at", "authorization_assurance"},
        )
        self.assertNotIn("authorized_user_hash", json.dumps(ingress))
        self.assertNotIn("credential_id_hash", json.dumps(ingress))
        self.assertNotIn("proposal_hash", ingress["receipt"])
        self.assertFalse(ingress["execution_allowed"])

    def test_challenge_is_single_use_and_bound_to_ha_user(self) -> None:
        self.register()
        request = self.manager.create_request(make_envelope())
        begin = self.manager.begin_authorization(
            approval_id=request["approval_id"], remote_user_id="ha-user-example"
        )
        with self.assertRaisesRegex(AuthorizationError, "does not match"):
            self.manager.complete_authorization(
                approval_id=request["approval_id"],
                remote_user_id="different-ha-user",
                flow_id=begin["flow_id"],
                response={"ok": True},
            )
        with self.assertRaisesRegex(AuthorizationError, "invalid"):
            self.manager.complete_authorization(
                approval_id=request["approval_id"],
                remote_user_id="ha-user-example",
                flow_id=begin["flow_id"],
                response={"ok": True},
            )

    def test_challenge_expiry_and_counter_rollback_fail_closed(self) -> None:
        self.register()
        request = self.manager.create_request(make_envelope())
        begin = self.manager.begin_authorization(
            approval_id=request["approval_id"], remote_user_id="ha-user-example"
        )
        self.clock.advance(181)
        with self.assertRaisesRegex(AuthorizationError, "expired"):
            self.manager.complete_authorization(
                approval_id=request["approval_id"],
                remote_user_id="ha-user-example",
                flow_id=begin["flow_id"],
                response={"ok": True},
            )

        second_database = pathlib.Path(self.temporary.name) / "counter" / "passkeys.sqlite3"
        backend = FakePasskeys(registration_count=5, authentication_count=5)
        store = AuthorizationStore(second_database)
        manager = AuthorizationManager(
            store=store,
            passkeys=backend,
            trusted_owner_hashes=frozenset({OWNER_HASH}),
            enrollment_token=ENROLLMENT_TOKEN,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            clock=MutableClock(),
        )
        registration = manager.begin_registration(
            remote_user_id="counter-user", enrollment_token=ENROLLMENT_TOKEN
        )
        manager.complete_registration(
            remote_user_id="counter-user",
            enrollment_token=ENROLLMENT_TOKEN,
            flow_id=registration["flow_id"],
            response={"ok": True},
        )
        pending = manager.create_request(make_envelope())
        assertion = manager.begin_authorization(
            approval_id=pending["approval_id"], remote_user_id="counter-user"
        )
        with self.assertRaisesRegex(AuthorizationError, "did not advance"):
            manager.complete_authorization(
                approval_id=pending["approval_id"],
                remote_user_id="counter-user",
                flow_id=assertion["flow_id"],
                response={"ok": True},
            )


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = MutableClock()
        self.store = AuthorizationStore(
            pathlib.Path(self.temporary.name) / "private" / "passkeys.sqlite3"
        )
        self.manager = AuthorizationManager(
            store=self.store,
            passkeys=FakePasskeys(),
            trusted_owner_hashes=frozenset({OWNER_HASH}),
            enrollment_token=ENROLLMENT_TOKEN,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            clock=self.clock,
        )
        registration = self.manager.begin_registration(
            remote_user_id="ha-user-example", enrollment_token=ENROLLMENT_TOKEN
        )
        self.manager.complete_registration(
            remote_user_id="ha-user-example",
            enrollment_token=ENROLLMENT_TOKEN,
            flow_id=registration["flow_id"],
            response={"ok": True},
        )

    def authorized_execution(self, label: str = "restart") -> tuple[dict, dict, dict]:
        proposal = self.manager.create_proposal(make_intent(key=idempotency(label)))
        request = self.manager.create_native_request(
            {"version": 1, "action_id": proposal["action_id"]}
        )
        begin = self.manager.begin_authorization(
            approval_id=request["approval_id"], remote_user_id="ha-user-example"
        )
        authorized = self.manager.complete_authorization(
            approval_id=request["approval_id"],
            remote_user_id="ha-user-example",
            flow_id=begin["flow_id"],
            response={"ok": True},
        )
        payload = {
            "version": 1,
            "receipt_id": authorized["receipt"]["receipt_id"],
            "action_id": proposal["action_id"],
            "proposal_hash": proposal["proposal_hash"],
            "idempotency_key": proposal["idempotency_key"],
        }
        return proposal, request, payload

    def executor(
        self,
        supervisor: FakeExecutionSupervisor,
        *,
        enabled: bool = True,
        actions: frozenset[str] = frozenset({"restart_addon"}),
    ) -> ExecutionManager:
        return ExecutionManager(
            store=self.store,
            supervisor=supervisor,
            execution_enabled=enabled,
            enabled_actions=actions,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            clock=self.clock,
        )

    def test_default_disabled_does_not_consume_receipt_or_call_supervisor(self) -> None:
        _proposal, request, payload = self.authorized_execution()
        supervisor = FakeExecutionSupervisor()
        execution = self.executor(supervisor, enabled=False, actions=frozenset())
        with self.assertRaisesRegex(AuthorizationError, "disabled"):
            execution.execute(payload)
        self.assertEqual(supervisor.calls, [])
        self.assertFalse(self.store.receipt_for_request(request["approval_id"])["consumed"])

    def test_single_use_receipt_executes_once_and_replay_returns_same_result(self) -> None:
        _proposal, request, payload = self.authorized_execution()
        supervisor = FakeExecutionSupervisor()
        execution = self.executor(supervisor)
        first = execution.execute(payload)
        second = execution.execute(payload)
        self.assertEqual(first["state"], "succeeded")
        self.assertTrue(second["replayed"])
        self.assertEqual(
            supervisor.calls,
            [("info", ALLOWED_ADDON), ("restart", ALLOWED_ADDON), ("info", ALLOWED_ADDON)],
        )
        self.assertTrue(self.store.receipt_for_request(request["approval_id"])["consumed"])

    def test_arbitrary_execution_fields_and_postflight_mismatch_fail_closed(self) -> None:
        _proposal, _request, payload = self.authorized_execution()
        supervisor = FakeExecutionSupervisor(postflight_version="9.9.9")
        execution = self.executor(supervisor)
        with self.assertRaisesRegex(AuthorizationError, "fields are invalid"):
            execution.execute({**payload, "parameters": {"path": "/config"}})
        result = execution.execute(payload)
        self.assertEqual(result["state"], "recovery_required")
        self.assertEqual(result["error_code"], "postflight_mismatch")

    def test_missing_and_expired_receipts_fail_without_supervisor_write(self) -> None:
        supervisor = FakeExecutionSupervisor()
        execution = self.executor(supervisor)
        missing = {
            "version": 1,
            "receipt_id": "RCPT-" + "A" * 32,
            "action_id": "OPS-20260731-A1B2C3D4E5F6",
            "proposal_hash": "sha256:" + "a" * 64,
            "idempotency_key": "sha256:" + "b" * 64,
        }
        with self.assertRaisesRegex(AuthorizationError, "not found"):
            execution.execute(missing)

        _proposal, request, payload = self.authorized_execution("expired")
        self.clock.advance(601)
        with self.assertRaisesRegex(AuthorizationError, "expired"):
            execution.execute(payload)
        self.assertEqual(supervisor.calls, [])
        self.assertFalse(self.store.receipt_for_request(request["approval_id"])["consumed"])

    def test_concurrent_execution_is_rejected_and_only_one_restart_occurs(self) -> None:
        _proposal, _request, payload = self.authorized_execution()
        supervisor = FakeExecutionSupervisor(block_restart=True)
        execution = self.executor(supervisor)
        result: list[dict] = []
        worker = threading.Thread(target=lambda: result.append(execution.execute(payload)))
        worker.start()
        self.assertTrue(supervisor.restart_started.wait(timeout=2))
        with self.assertRaisesRegex(AuthorizationError, "Another operation"):
            execution.execute(payload)
        supervisor.restart_release.set()
        worker.join(timeout=3)
        self.assertEqual(result[0]["state"], "succeeded")
        self.assertEqual(supervisor.calls.count(("restart", ALLOWED_ADDON)), 1)

    def test_restart_recovery_marks_claimed_execution_recovery_required(self) -> None:
        _proposal, _request, payload = self.authorized_execution()
        claimed, replayed = self.store.claim_execution(
            receipt_id=payload["receipt_id"],
            action_id=payload["action_id"],
            proposal_hash=payload["proposal_hash"],
            idempotency_key=payload["idempotency_key"],
            claimed_at=self.clock(),
        )
        self.assertFalse(replayed)
        self.assertEqual(claimed["state"], "authorized")
        execution = self.executor(FakeExecutionSupervisor())
        self.assertEqual(execution.recovered_executions, 1)
        self.assertEqual(execution.status(payload["action_id"])["state"], "recovery_required")


class AuthorizationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.manager = AuthorizationManager(
            store=AuthorizationStore(
                pathlib.Path(self.temporary.name) / "private" / "passkeys.sqlite3"
            ),
            passkeys=FakePasskeys(),
            trusted_owner_hashes=frozenset({OWNER_HASH}),
            enrollment_token=ENROLLMENT_TOKEN,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            clock=MutableClock(),
        )
        self.server = create_server(
            "127.0.0.1",
            0,
            api_token="a" * 32,
            max_request_bytes=32768,
            preflight_handler=lambda _payload: {"execution_allowed": False},
            authorization_manager=self.manager,
            allowed_ingress_origins=frozenset({INGRESS_ORIGIN}),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def request_json(self, path: str, payload: dict, *, headers=None) -> dict:
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        request = Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers=request_headers,
            method="POST",
        )
        with urlopen(request) as response:
            return json.loads(response.read())

    def test_internal_request_creation_requires_bearer(self) -> None:
        with self.assertRaises(HTTPError) as context:
            self.request_json("/v1/proposals", make_intent())
        self.assertEqual(context.exception.code, 401)
        proposal = self.request_json(
            "/v1/proposals",
            make_intent(),
            headers={"Authorization": "Bearer " + "a" * 32},
        )
        created = self.request_json(
            "/v1/authorization/requests",
            {"version": 1, "action_id": proposal["action_id"]},
            headers={"Authorization": "Bearer " + "a" * 32},
        )
        self.assertTrue(created["approval_id"].startswith("AUTH-"))
        self.assertEqual(created["proposal_origin"], "broker_native")
        self.assertFalse(created["execution_allowed"])

    def test_ingress_requires_authenticated_user_and_exact_origin(self) -> None:
        with self.assertRaises(HTTPError) as context:
            urlopen(self.base + "/api/context")
        self.assertEqual(context.exception.code, 401)

        with self.assertRaises(HTTPError) as context:
            self.request_json(
                "/api/passkeys/register/begin",
                {"enrollment_token": ENROLLMENT_TOKEN},
                headers={
                    "X-Remote-User-Id": "ha-user-example",
                    "Origin": "https://evil.example.invalid",
                },
            )
        self.assertEqual(context.exception.code, 403)

    def test_enrollment_token_is_not_reflected(self) -> None:
        result = self.request_json(
            "/api/passkeys/register/begin",
            {"enrollment_token": ENROLLMENT_TOKEN},
            headers={
                "X-Remote-User-Id": "ha-user-example",
                "Origin": INGRESS_ORIGIN,
            },
        )
        rendered = json.dumps(result)
        self.assertNotIn(ENROLLMENT_TOKEN, rendered)
        self.assertFalse(result["execution_allowed"])


class Fido2DependencySmokeTests(unittest.TestCase):
    def test_real_fido2_backend_generates_uv_required_options(self) -> None:
        try:
            from operations_broker.passkeys import Fido2PasskeyBackend
        except ModuleNotFoundError as exc:
            self.skipTest(f"fido2 dependency is installed in the add-on image: {exc}")
        backend = Fido2PasskeyBackend(
            rp_id="example.invalid",
            allowed_origins=("https://ha.example.invalid",),
        )
        options, state = backend.registration_begin(
            user_handle=b"u" * 32, existing_credentials=[]
        )
        self.assertIn("challenge", options["publicKey"])
        self.assertEqual(
            options["publicKey"]["authenticatorSelection"]["userVerification"],
            "required",
        )
        self.assertIn("challenge", state)


if __name__ == "__main__":
    unittest.main()
