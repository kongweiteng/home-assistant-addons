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


UTC = timezone.utc
NOW = datetime(2026, 7, 31, 13, 0, tzinfo=UTC)
OWNER_HASH = sha256_text("weixin:owner-example")
ENROLLMENT_TOKEN = "enrollment-example-" + "x" * 32
INGRESS_ORIGIN = "https://ha.example.invalid"


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
            self.request_json("/v1/authorization/requests", make_envelope())
        self.assertEqual(context.exception.code, 401)
        created = self.request_json(
            "/v1/authorization/requests",
            make_envelope(),
            headers={"Authorization": "Bearer " + "a" * 32},
        )
        self.assertTrue(created["approval_id"].startswith("AUTH-"))
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
