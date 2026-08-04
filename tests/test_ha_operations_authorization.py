"""Tests for the independent Passkey authorization root canary."""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
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
from operations_broker.contract import (
    DEFAULT_ADAPTER_SCHEMA_VERSION,
    DEFAULT_ADAPTER_VERSION,
    DEFAULT_POLICY_EPOCH,
    DEFAULT_POLICY_HASH,
    PROPOSAL_HASH_FIELDS,
    addon_baseline_etag,
    allowlist_fingerprint,
    canonical_json,
    sha256_text,
)
from operations_broker.execution import ExecutionManager
from operations_broker.supervisor import SupervisorError


UTC = timezone.utc
NOW = datetime(2026, 7, 31, 13, 0, tzinfo=UTC)
OWNER_HASH = sha256_text("weixin:owner-example")
ENROLLMENT_TOKEN = "enrollment-example-" + "x" * 32
INGRESS_ORIGIN = "https://ha.example.invalid"
ALLOWED_ADDON = "example_addon"
RECOVERY_EVIDENCE_HASH = "sha256:" + sha256_text("recovery:evidence")
BACKUP_EVIDENCE_ID = "backup-example-20260731"
BASELINE_ETAG = addon_baseline_etag(
    {
        "slug": ALLOWED_ADDON,
        "state": "started",
        "version": "1.2.3",
        "installed": True,
    }
)


def idempotency(label: str) -> str:
    return "sha256:" + sha256_text(f"operations:{label}")


def make_backup_evidence(
    *,
    scope: str = "addon",
    baseline: str = BASELINE_ETAG,
    logical_id: str = BACKUP_EVIDENCE_ID,
    created_at: datetime = NOW - timedelta(hours=1),
    expires_at: datetime = NOW + timedelta(days=1),
    completed: bool = True,
    readable: bool = True,
) -> dict:
    return {
        "version": 1,
        "scope": scope,
        "logical_id": logical_id,
        "completed": completed,
        "created_at": created_at.isoformat(),
        "size": 1024,
        "sha256": "sha256:" + sha256_text("backup:on-device"),
        "off_device_sha256": "sha256:" + sha256_text("backup:off-device"),
        "readable": readable,
        "baseline": baseline,
        "expires_at": expires_at.isoformat(),
    }


def make_intent(
    *,
    target: str = ALLOWED_ADDON,
    key: str = idempotency("restart"),
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
        preflight_version: str = "1.2.3",
        block_restart: bool = False,
    ) -> None:
        self.postflight_state = postflight_state
        self.postflight_version = postflight_version
        self.preflight_version = preflight_version
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
            "state": "started" if self.info_count <= 2 else self.postflight_state,
            "version": self.preflight_version if self.info_count <= 2 else self.postflight_version,
            "installed": True,
        }

    def restart_addon(self, slug: str) -> None:
        self.calls.append(("restart", slug))
        self.restart_started.set()
        if self.block_restart and not self.restart_release.wait(timeout=3):
            raise SupervisorError("test_timeout", "test restart timed out")


class FakeManagerShadow:
    def __init__(self, observation: dict | None = None) -> None:
        self.observation = observation or {
            "slug": ALLOWED_ADDON,
            "state": "started",
            "version": "1.2.3",
            "installed": True,
        }
        self.calls: list[dict] = []

    def shadow_restart(self, proposal: dict) -> dict:
        self.calls.append(dict(proposal))
        return {"observation": dict(self.observation)}


class AuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = pathlib.Path(self.temporary.name) / "private" / "passkeys.sqlite3"
        self.clock = MutableClock()
        self.backend = FakePasskeys()
        self.store = AuthorizationStore(self.database)
        self.store.register_backup_evidence(
            make_backup_evidence(), registered_at=self.clock()
        )
        self.manager = AuthorizationManager(
            store=self.store,
            passkeys=self.backend,
            trusted_owner_hashes=frozenset({OWNER_HASH}),
            enrollment_token=ENROLLMENT_TOKEN,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            baseline_provider=lambda _target: BASELINE_ETAG,
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
        self.assertEqual(first["policy_epoch"], DEFAULT_POLICY_EPOCH)
        self.assertEqual(first["policy_hash"], DEFAULT_POLICY_HASH)
        self.assertEqual(first["baseline_etag"], BASELINE_ETAG)
        self.assertEqual(first["backup_evidence_id"], BACKUP_EVIDENCE_ID)

        with self.assertRaisesRegex(AuthorizationError, "fields are invalid"):
            self.manager.create_proposal(
                {**make_intent(), "baseline_etag": "sha256:" + "c" * 64}
            )
        self.clock.advance(86_401)
        replay = self.manager.create_proposal(make_intent())
        self.assertEqual(replay["action_id"], first["action_id"])
        self.assertEqual(replay["state"], "expired")

    def test_backup_evidence_is_structured_immutable_and_required(self) -> None:
        evidence = make_backup_evidence()
        self.assertEqual(
            self.store.register_backup_evidence(evidence, registered_at=self.clock()),
            evidence,
        )
        with self.assertRaises(AuthorizationError) as raised:
            self.store.register_backup_evidence(
                {**evidence, "size": 2048}, registered_at=self.clock()
            )
        self.assertEqual(raised.exception.code, "backup_evidence_conflict")
        with sqlite3.connect(self.store.path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE backup_evidence SET size = size + 1 WHERE logical_id = ?",
                    (BACKUP_EVIDENCE_ID,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM backup_evidence WHERE logical_id = ?",
                    (BACKUP_EVIDENCE_ID,),
                )

        invalid_values = (
            ({**evidence, "logical_id": "backup-incomplete", "completed": False}, "backup_evidence_incomplete"),
            ({**evidence, "logical_id": "backup-unreadable", "readable": False}, "backup_evidence_unreadable"),
            ({**evidence, "logical_id": "backup-size-zero", "size": 0}, "backup_evidence_size_invalid"),
            ({**evidence, "logical_id": "backup-hash-bad", "sha256": "sha256:bad"}, "backup_evidence_hash_invalid"),
            ({**evidence, "logical_id": "backup-off-device-bad", "off_device_sha256": "sha256:bad"}, "backup_evidence_hash_invalid"),
            ({**evidence, "logical_id": "backup-scope-bad", "scope": "host"}, "backup_evidence_scope_invalid"),
            ({**evidence, "logical_id": "backup-future", "created_at": (NOW + timedelta(seconds=1)).isoformat()}, "backup_evidence_created_in_future"),
            ({**evidence, "logical_id": "backup-expired", "expires_at": NOW.isoformat()}, "backup_evidence_expired"),
            ({**evidence, "logical_id": "backup-size-huge", "size": 9_223_372_036_854_775_808}, "backup_evidence_size_invalid"),
            ({**evidence, "logical_id": "sk-thislookssecret1234567890"}, "backup_evidence_id_invalid"),
            ({**evidence, "logical_id": "backup-extra-field", "path": "/backup/private"}, "backup_evidence_fields_invalid"),
        )
        for invalid, code in invalid_values:
            with self.subTest(code=code):
                with self.assertRaises(AuthorizationError) as raised:
                    self.store.register_backup_evidence(invalid, registered_at=self.clock())
                self.assertEqual(raised.exception.code, code)

        empty_store = AuthorizationStore(
            pathlib.Path(self.temporary.name) / "without-evidence" / "passkeys.sqlite3"
        )
        empty_manager = AuthorizationManager(
            store=empty_store,
            passkeys=FakePasskeys(),
            trusted_owner_hashes=frozenset({OWNER_HASH}),
            enrollment_token=ENROLLMENT_TOKEN,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            baseline_provider=lambda _target: BASELINE_ETAG,
            clock=self.clock,
        )
        with self.assertRaises(AuthorizationError) as raised:
            empty_manager.create_proposal(make_intent(key=idempotency("no-evidence")))
        self.assertEqual(raised.exception.code, "backup_evidence_required")

        mismatch_store = AuthorizationStore(
            pathlib.Path(self.temporary.name) / "mismatch-evidence" / "passkeys.sqlite3"
        )
        mismatch_store.register_backup_evidence(
            make_backup_evidence(
                logical_id="backup-baseline-mismatch",
                baseline="sha256:" + "d" * 64,
            ),
            registered_at=self.clock(),
        )
        mismatch_manager = AuthorizationManager(
            store=mismatch_store,
            passkeys=FakePasskeys(),
            trusted_owner_hashes=frozenset({OWNER_HASH}),
            enrollment_token=ENROLLMENT_TOKEN,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            baseline_provider=lambda _target: BASELINE_ETAG,
            clock=self.clock,
        )
        with self.assertRaises(AuthorizationError) as raised:
            mismatch_manager.create_proposal(make_intent(key=idempotency("mismatch-evidence")))
        self.assertEqual(raised.exception.code, "backup_evidence_required")

        short_store = AuthorizationStore(
            pathlib.Path(self.temporary.name) / "short-evidence" / "passkeys.sqlite3"
        )
        short_store.register_backup_evidence(
            make_backup_evidence(
                logical_id="backup-too-short",
                expires_at=NOW + timedelta(seconds=300),
            ),
            registered_at=self.clock(),
        )
        short_manager = AuthorizationManager(
            store=short_store,
            passkeys=FakePasskeys(),
            trusted_owner_hashes=frozenset({OWNER_HASH}),
            enrollment_token=ENROLLMENT_TOKEN,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            baseline_provider=lambda _target: BASELINE_ETAG,
            clock=self.clock,
        )
        with self.assertRaises(AuthorizationError) as raised:
            short_manager.create_proposal(make_intent(key=idempotency("short-evidence")))
        self.assertEqual(raised.exception.code, "backup_evidence_required")

        full_store = AuthorizationStore(
            pathlib.Path(self.temporary.name) / "full-evidence" / "passkeys.sqlite3"
        )
        full_store.register_backup_evidence(
            make_backup_evidence(scope="full", logical_id="backup-full-eligible"),
            registered_at=self.clock(),
        )
        full_manager = AuthorizationManager(
            store=full_store,
            passkeys=FakePasskeys(),
            trusted_owner_hashes=frozenset({OWNER_HASH}),
            enrollment_token=ENROLLMENT_TOKEN,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            baseline_provider=lambda _target: BASELINE_ETAG,
            clock=self.clock,
        )
        full_proposal = full_manager.create_proposal(
            make_intent(key=idempotency("full-evidence"))
        )
        self.assertEqual(full_proposal["backup_evidence_id"], "backup-full-eligible")

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
        store.register_backup_evidence(make_backup_evidence(), registered_at=NOW)
        manager = AuthorizationManager(
            store=store,
            passkeys=backend,
            trusted_owner_hashes=frozenset({OWNER_HASH}),
            enrollment_token=ENROLLMENT_TOKEN,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            baseline_provider=lambda _target: BASELINE_ETAG,
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
        self.store.register_backup_evidence(
            make_backup_evidence(), registered_at=self.clock()
        )
        self.passkeys = FakePasskeys()
        self.authorization_count = 0
        self.manager = AuthorizationManager(
            store=self.store,
            passkeys=self.passkeys,
            trusted_owner_hashes=frozenset({OWNER_HASH}),
            enrollment_token=ENROLLMENT_TOKEN,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            baseline_provider=lambda _target: BASELINE_ETAG,
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
        self.authorization_count += 1
        self.passkeys.authentication_count = self.authorization_count
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
        manager_shadow: FakeManagerShadow | None = None,
    ) -> ExecutionManager:
        return ExecutionManager(
            store=self.store,
            supervisor=supervisor,
            execution_enabled=enabled,
            enabled_actions=actions,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            manager_shadow=manager_shadow,
            clock=self.clock,
        )

    def claim_direct(
        self,
        payload: dict,
        *,
        store: AuthorizationStore | None = None,
        instance_id: str = "BROKER-" + "A" * 32,
        lease_ttl_seconds: int = 30,
    ) -> tuple[dict, bool]:
        return (store or self.store).claim_execution(
            receipt_id=payload["receipt_id"],
            action_id=payload["action_id"],
            proposal_hash=payload["proposal_hash"],
            idempotency_key=payload["idempotency_key"],
            policy_epoch=DEFAULT_POLICY_EPOCH,
            policy_hash=DEFAULT_POLICY_HASH,
            allowlist_hash=allowlist_fingerprint(frozenset({ALLOWED_ADDON})),
            adapter_version=DEFAULT_ADAPTER_VERSION,
            adapter_schema_version=DEFAULT_ADAPTER_SCHEMA_VERSION,
            baseline_etag=BASELINE_ETAG,
            backup_evidence_id=BACKUP_EVIDENCE_ID,
            instance_id=instance_id,
            lease_ttl_seconds=lease_ttl_seconds,
            claimed_at=self.clock(),
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
        proposal, request, payload = self.authorized_execution()
        supervisor = FakeExecutionSupervisor()
        execution = self.executor(supervisor)
        first = execution.execute(payload)
        second = execution.execute(payload)
        self.assertEqual(first["state"], "succeeded")
        self.assertTrue(second["replayed"])
        self.assertEqual(
            supervisor.calls,
            [
                ("info", ALLOWED_ADDON),
                ("info", ALLOWED_ADDON),
                ("restart", ALLOWED_ADDON),
                ("info", ALLOWED_ADDON),
            ],
        )
        receipt = self.store.receipt_for_request(request["approval_id"])
        self.assertTrue(receipt["consumed"])
        self.assertEqual(proposal["backup_evidence_id"], BACKUP_EVIDENCE_ID)
        self.assertEqual(request["backup_evidence_id"], BACKUP_EVIDENCE_ID)
        self.assertEqual(receipt["backup_evidence_id"], BACKUP_EVIDENCE_ID)
        self.assertEqual(first["backup_evidence_id"], BACKUP_EVIDENCE_ID)

    def test_manager_shadow_matches_before_claim_and_mismatch_consumes_nothing(self) -> None:
        _proposal, request, payload = self.authorized_execution("manager-shadow-ok")
        supervisor = FakeExecutionSupervisor()
        shadow = FakeManagerShadow()
        result = self.executor(supervisor, manager_shadow=shadow).execute(payload)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(len(shadow.calls), 1)

        _proposal, request, payload = self.authorized_execution("manager-shadow-drift")
        supervisor = FakeExecutionSupervisor()
        shadow = FakeManagerShadow({
            "slug": ALLOWED_ADDON,
            "state": "started",
            "version": "9.9.9",
            "installed": True,
        })
        with self.assertRaises(AuthorizationError) as raised:
            self.executor(supervisor, manager_shadow=shadow).execute(payload)
        self.assertEqual(raised.exception.code, "manager_shadow_mismatch")
        self.assertFalse(self.store.receipt_for_request(request["approval_id"])["consumed"])
        self.assertNotIn(("restart", ALLOWED_ADDON), supervisor.calls)

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

    def test_policy_and_baseline_drift_fail_before_receipt_consumption(self) -> None:
        _proposal, request, payload = self.authorized_execution("policy-drift")
        supervisor = FakeExecutionSupervisor()
        execution = ExecutionManager(
            store=self.store,
            supervisor=supervisor,
            execution_enabled=True,
            enabled_actions=frozenset({"restart_addon"}),
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            policy_hash="sha256:" + "d" * 64,
            clock=self.clock,
        )
        with self.assertRaisesRegex(AuthorizationError, "binding changed") as context:
            execution.execute(payload)
        self.assertEqual(context.exception.code, "policy_changed")
        self.assertEqual(supervisor.calls, [])
        self.assertFalse(self.store.receipt_for_request(request["approval_id"])["consumed"])

        _proposal, request, payload = self.authorized_execution("baseline-drift")
        supervisor = FakeExecutionSupervisor(preflight_version="1.2.4")
        with self.assertRaisesRegex(AuthorizationError, "baseline changed") as context:
            self.executor(supervisor).execute(payload)
        self.assertEqual(context.exception.code, "baseline_changed")
        self.assertEqual(supervisor.calls, [("info", ALLOWED_ADDON)])
        self.assertFalse(self.store.receipt_for_request(request["approval_id"])["consumed"])

    def test_backup_evidence_drift_fails_before_receipt_consumption_or_supervisor(self) -> None:
        _proposal, request, payload = self.authorized_execution("backup-evidence-drift")
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("DROP TRIGGER backup_evidence_no_update")
            connection.execute(
                "UPDATE backup_evidence SET baseline = ? WHERE logical_id = ?",
                ("sha256:" + "d" * 64, BACKUP_EVIDENCE_ID),
            )
        supervisor = FakeExecutionSupervisor()
        with self.assertRaises(AuthorizationError) as raised:
            self.executor(supervisor).execute(payload)
        self.assertEqual(raised.exception.code, "backup_evidence_changed")
        self.assertEqual(supervisor.calls, [])
        self.assertFalse(self.store.receipt_for_request(request["approval_id"])["consumed"])

    def test_backup_evidence_expiry_window_fails_before_receipt_consumption(self) -> None:
        _proposal, request, payload = self.authorized_execution("backup-evidence-expiry")
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("DROP TRIGGER backup_evidence_no_update")
            connection.execute(
                "UPDATE backup_evidence SET expires_at = ? WHERE logical_id = ?",
                ((NOW + timedelta(seconds=60)).isoformat(), BACKUP_EVIDENCE_ID),
            )
        supervisor = FakeExecutionSupervisor()
        with self.assertRaises(AuthorizationError) as raised:
            self.executor(supervisor).execute(payload)
        self.assertEqual(raised.exception.code, "backup_evidence_expired")
        self.assertEqual(supervisor.calls, [])
        self.assertFalse(self.store.receipt_for_request(request["approval_id"])["consumed"])

    def test_corrupt_backup_evidence_timestamp_has_stable_error(self) -> None:
        _proposal, request, payload = self.authorized_execution("backup-evidence-corrupt-time")
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("DROP TRIGGER backup_evidence_no_update")
            connection.execute(
                "UPDATE backup_evidence SET expires_at = 'not-a-time' WHERE logical_id = ?",
                (BACKUP_EVIDENCE_ID,),
            )
        supervisor = FakeExecutionSupervisor()
        with self.assertRaises(AuthorizationError) as raised:
            self.executor(supervisor).execute(payload)
        self.assertEqual(raised.exception.code, "backup_evidence_changed")
        self.assertEqual(supervisor.calls, [])
        self.assertFalse(self.store.receipt_for_request(request["approval_id"])["consumed"])

    def test_persistent_lease_blocks_second_store_and_expiry_requires_recovery(self) -> None:
        _proposal, _request, first_payload = self.authorized_execution("lease-first")
        _proposal, second_request, second_payload = self.authorized_execution("lease-second")
        first, replayed = self.claim_direct(first_payload, lease_ttl_seconds=5)
        self.assertFalse(replayed)
        self.assertEqual(first["state"], "authorized")
        second_store = AuthorizationStore(self.store.path)
        with self.assertRaisesRegex(AuthorizationError, "lease is already held") as context:
            self.claim_direct(
                second_payload,
                store=second_store,
                instance_id="BROKER-" + "B" * 32,
                lease_ttl_seconds=5,
            )
        self.assertEqual(context.exception.code, "execution_busy")
        self.assertFalse(self.store.receipt_for_request(second_request["approval_id"])["consumed"])
        self.assertEqual(
            {lease["state"] for lease in self.store.leases_for_action(first_payload["action_id"])},
            {"active"},
        )

        self.clock.advance(6)
        with self.assertRaisesRegex(AuthorizationError, "requires recovery") as context:
            self.claim_direct(
                second_payload,
                store=second_store,
                instance_id="BROKER-" + "B" * 32,
                lease_ttl_seconds=5,
            )
        self.assertEqual(context.exception.code, "lease_recovery_required")
        self.assertEqual(self.store.get_execution(first_payload["action_id"])["state"], "recovery_required")
        self.assertFalse(self.store.receipt_for_request(second_request["approval_id"])["consumed"])

    def test_restart_recovery_marks_claimed_execution_recovery_required(self) -> None:
        _proposal, _request, payload = self.authorized_execution()
        claimed, replayed = self.store.claim_execution(
            receipt_id=payload["receipt_id"],
            action_id=payload["action_id"],
            proposal_hash=payload["proposal_hash"],
            idempotency_key=payload["idempotency_key"],
            policy_epoch=DEFAULT_POLICY_EPOCH,
            policy_hash=DEFAULT_POLICY_HASH,
            allowlist_hash=allowlist_fingerprint(frozenset({ALLOWED_ADDON})),
            adapter_version=DEFAULT_ADAPTER_VERSION,
            adapter_schema_version=DEFAULT_ADAPTER_SCHEMA_VERSION,
            baseline_etag=BASELINE_ETAG,
            backup_evidence_id=BACKUP_EVIDENCE_ID,
            instance_id="BROKER-" + "A" * 32,
            lease_ttl_seconds=30,
            claimed_at=self.clock(),
        )
        self.assertFalse(replayed)
        self.assertEqual(claimed["state"], "authorized")
        execution = self.executor(FakeExecutionSupervisor())
        self.assertEqual(execution.recovered_executions, 1)
        self.assertEqual(execution.status(payload["action_id"])["state"], "recovery_required")

    def test_unresolved_recovery_blocks_new_execution_before_receipt_consumption(self) -> None:
        _proposal, _request, first_payload = self.authorized_execution("first-recovery")
        supervisor = FakeExecutionSupervisor(postflight_version="9.9.9")
        execution = self.executor(supervisor)
        first = execution.execute(first_payload)
        self.assertEqual(first["state"], "recovery_required")
        self.assertFalse(first["recovery"]["resolved"])

        _proposal, second_request, second_payload = self.authorized_execution(
            "blocked-by-recovery"
        )
        second_supervisor = FakeExecutionSupervisor()
        second_execution = self.executor(second_supervisor)
        with self.assertRaisesRegex(AuthorizationError, "requires recovery") as context:
            second_execution.execute(second_payload)
        self.assertEqual(context.exception.code, "unresolved_recovery")
        self.assertEqual(second_supervisor.calls, [])
        self.assertFalse(
            self.store.receipt_for_request(second_request["approval_id"])["consumed"]
        )

        resolved = execution.resolve_recovery(
            first_payload["action_id"],
            {
                "version": 1,
                "resolution": "confirmed_healthy",
                "evidence_hash": RECOVERY_EVIDENCE_HASH,
            },
        )
        self.assertEqual(resolved["state"], "recovery_required")
        self.assertEqual(
            resolved["recovery"],
            {
                "resolved": True,
                "resolution": "confirmed_healthy",
                "evidence_hash": RECOVERY_EVIDENCE_HASH,
                "resolved_at": NOW.isoformat(),
            },
        )

        second = second_execution.execute(second_payload)
        self.assertEqual(second["state"], "succeeded")

    def test_existing_execution_replay_is_returned_while_recovery_is_unresolved(self) -> None:
        _proposal, _request, payload = self.authorized_execution("replay-recovery")
        supervisor = FakeExecutionSupervisor(postflight_version="9.9.9")
        execution = self.executor(supervisor)
        first = execution.execute(payload)
        calls_after_first = list(supervisor.calls)
        replay = execution.execute(payload)
        self.assertEqual(first["state"], "recovery_required")
        self.assertTrue(replay["replayed"])
        self.assertFalse(replay["recovery"]["resolved"])
        self.assertEqual(supervisor.calls, calls_after_first)

    def test_recovery_resolution_contract_and_state_fail_closed_without_supervisor(self) -> None:
        _proposal, _request, payload = self.authorized_execution("resolution-contract")
        supervisor = FakeExecutionSupervisor(postflight_version="9.9.9")
        execution = self.executor(supervisor)
        execution.execute(payload)
        calls_before_resolution = list(supervisor.calls)

        invalid_payloads = (
            {
                "version": 1,
                "resolution": "ignored",
                "evidence_hash": RECOVERY_EVIDENCE_HASH,
            },
            {
                "version": 1,
                "resolution": "compensated",
                "evidence_hash": "sha256:bad",
            },
            {
                "version": 1,
                "resolution": "compensated",
                "evidence_hash": RECOVERY_EVIDENCE_HASH,
                "note": "free text is forbidden",
            },
            {
                "version": 1,
                "resolution": [],
                "evidence_hash": RECOVERY_EVIDENCE_HASH,
            },
        )
        for invalid in invalid_payloads:
            with self.assertRaises(AuthorizationError):
                execution.resolve_recovery(payload["action_id"], invalid)

        resolved = execution.resolve_recovery(
            payload["action_id"],
            {
                "version": 1,
                "resolution": "compensated",
                "evidence_hash": RECOVERY_EVIDENCE_HASH,
            },
        )
        self.assertEqual(resolved["recovery"]["resolution"], "compensated")
        with self.assertRaisesRegex(AuthorizationError, "already resolved"):
            execution.resolve_recovery(
                payload["action_id"],
                {
                    "version": 1,
                    "resolution": "confirmed_healthy",
                    "evidence_hash": RECOVERY_EVIDENCE_HASH,
                },
            )
        self.assertEqual(supervisor.calls, calls_before_resolution)

        _proposal, _request, succeeded_payload = self.authorized_execution(
            "resolution-not-required"
        )
        supervisor.postflight_version = "1.2.3"
        succeeded = execution.execute(succeeded_payload)
        self.assertEqual(succeeded["state"], "succeeded")
        with self.assertRaisesRegex(AuthorizationError, "does not require"):
            execution.resolve_recovery(
                succeeded_payload["action_id"],
                {
                    "version": 1,
                    "resolution": "confirmed_healthy",
                    "evidence_hash": RECOVERY_EVIDENCE_HASH,
                },
            )

    def test_concurrent_recovery_resolution_only_succeeds_once(self) -> None:
        _proposal, _request, payload = self.authorized_execution("resolution-race")
        execution = self.executor(FakeExecutionSupervisor(postflight_version="9.9.9"))
        execution.execute(payload)
        barrier = threading.Barrier(3)
        results: list[dict] = []
        errors: list[str] = []

        def resolve(resolution: str) -> None:
            barrier.wait()
            try:
                results.append(
                    execution.resolve_recovery(
                        payload["action_id"],
                        {
                            "version": 1,
                            "resolution": resolution,
                            "evidence_hash": RECOVERY_EVIDENCE_HASH,
                        },
                    )
                )
            except AuthorizationError as exc:
                errors.append(exc.code)

        workers = [
            threading.Thread(target=resolve, args=("confirmed_healthy",)),
            threading.Thread(target=resolve, args=("compensated",)),
        ]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=3)
        self.assertEqual(len(results), 1)
        self.assertEqual(errors, ["recovery_already_resolved"])

    def test_resolved_recovery_survives_restart_and_keeps_original_state(self) -> None:
        _proposal, _request, payload = self.authorized_execution("resolved-restart")
        claimed, replayed = self.store.claim_execution(
            receipt_id=payload["receipt_id"],
            action_id=payload["action_id"],
            proposal_hash=payload["proposal_hash"],
            idempotency_key=payload["idempotency_key"],
            policy_epoch=DEFAULT_POLICY_EPOCH,
            policy_hash=DEFAULT_POLICY_HASH,
            allowlist_hash=allowlist_fingerprint(frozenset({ALLOWED_ADDON})),
            adapter_version=DEFAULT_ADAPTER_VERSION,
            adapter_schema_version=DEFAULT_ADAPTER_SCHEMA_VERSION,
            baseline_etag=BASELINE_ETAG,
            backup_evidence_id=BACKUP_EVIDENCE_ID,
            instance_id="BROKER-" + "B" * 32,
            lease_ttl_seconds=30,
            claimed_at=self.clock(),
        )
        self.assertFalse(replayed)
        self.assertEqual(claimed["state"], "authorized")
        restarted = self.executor(FakeExecutionSupervisor())
        self.assertEqual(restarted.recovered_executions, 1)
        restarted.resolve_recovery(
            payload["action_id"],
            {
                "version": 1,
                "resolution": "confirmed_healthy",
                "evidence_hash": RECOVERY_EVIDENCE_HASH,
            },
        )

        restarted_again = self.executor(FakeExecutionSupervisor())
        self.assertEqual(restarted_again.recovered_executions, 0)
        status = restarted_again.status(payload["action_id"])
        self.assertEqual(status["state"], "recovery_required")
        self.assertTrue(status["recovery"]["resolved"])

        _proposal, _request, next_payload = self.authorized_execution(
            "after-resolved-restart"
        )
        next_execution = restarted_again.execute(next_payload)
        self.assertEqual(next_execution["state"], "succeeded")


class AuthorizationStoreMigrationTests(unittest.TestCase):
    def test_v4_proposal_receipt_execution_and_recovery_audit_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = pathlib.Path(temporary) / "audit" / "passkeys.sqlite3"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(
                    """
                    CREATE TABLE passkeys (
                        credential_id BLOB PRIMARY KEY,
                        credential_data BLOB NOT NULL,
                        user_id_hash TEXT NOT NULL,
                        sign_count INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        last_used_at TEXT
                    );
                    CREATE TABLE authorization_requests (
                        approval_id TEXT PRIMARY KEY,
                        action_id TEXT NOT NULL UNIQUE,
                        action_type TEXT NOT NULL,
                        target TEXT NOT NULL,
                        proposal_hash TEXT NOT NULL,
                        risk_level TEXT NOT NULL,
                        requires_backup INTEGER NOT NULL,
                        parameter_summary_json TEXT NOT NULL,
                        expected_change TEXT NOT NULL,
                        validation_plan_json TEXT NOT NULL,
                        rollback_plan_json TEXT NOT NULL,
                        structural_owner_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        state TEXT NOT NULL,
                        proposal_origin TEXT NOT NULL DEFAULT 'legacy_envelope'
                    );
                    CREATE TABLE authorization_receipts (
                        receipt_id TEXT PRIMARY KEY,
                        approval_id TEXT NOT NULL UNIQUE,
                        action_id TEXT NOT NULL,
                        proposal_hash TEXT NOT NULL,
                        authorized_user_hash TEXT NOT NULL,
                        credential_id_hash TEXT NOT NULL,
                        authorized_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        assurance TEXT NOT NULL,
                        consumed_at TEXT,
                        FOREIGN KEY(approval_id) REFERENCES authorization_requests(approval_id)
                    );
                    CREATE TABLE operation_proposals (
                        action_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        action_type TEXT NOT NULL,
                        target TEXT NOT NULL,
                        proposal_hash TEXT NOT NULL UNIQUE,
                        risk_level TEXT NOT NULL,
                        requires_backup INTEGER NOT NULL,
                        parameter_summary_json TEXT NOT NULL,
                        expected_change TEXT NOT NULL,
                        validation_plan_json TEXT NOT NULL,
                        rollback_plan_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    );
                    CREATE TABLE operation_executions (
                        action_id TEXT PRIMARY KEY,
                        receipt_id TEXT NOT NULL UNIQUE,
                        proposal_hash TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        action_type TEXT NOT NULL,
                        target TEXT NOT NULL,
                        state TEXT NOT NULL,
                        preflight_json TEXT,
                        postflight_json TEXT,
                        error_code TEXT,
                        recovery_resolution TEXT,
                        recovery_evidence_hash TEXT,
                        recovery_resolved_at TEXT,
                        started_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        finished_at TEXT,
                        FOREIGN KEY(receipt_id) REFERENCES authorization_receipts(receipt_id),
                        FOREIGN KEY(action_id) REFERENCES operation_proposals(action_id)
                    );
                    PRAGMA user_version=4;
                    """
                )
                connection.execute(
                    """
                    INSERT INTO operation_proposals(
                        action_id, idempotency_key, action_type, target, proposal_hash,
                        risk_level, requires_backup, parameter_summary_json,
                        expected_change, validation_plan_json, rollback_plan_json,
                        created_at, expires_at
                    ) VALUES (?, ?, 'restart_addon', 'example_addon', ?, 'L3', 1,
                              '{}', 'historical', '["check"]', '["stop"]', ?, ?)
                    """,
                    (
                        "OPS-20260731-A1B2C3D4E5F6",
                        "sha256:" + "b" * 64,
                        "sha256:" + "a" * 64,
                        "2026-07-31T13:00:00+00:00",
                        "2026-07-31T13:10:00+00:00",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO authorization_requests(
                        approval_id, action_id, action_type, target, proposal_hash,
                        risk_level, requires_backup, parameter_summary_json,
                        expected_change, validation_plan_json, rollback_plan_json,
                        structural_owner_hash, created_at, expires_at, state,
                        proposal_origin
                    ) VALUES ('AUTH-HISTORICAL00000000000000000001', ?, 'restart_addon',
                              'example_addon', ?, 'L3', 1, '{}', 'historical',
                              '["check"]', '["stop"]', ?, ?, ?, 'authorized',
                              'broker_native')
                    """,
                    (
                        "OPS-20260731-A1B2C3D4E5F6",
                        "sha256:" + "a" * 64,
                        OWNER_HASH,
                        "2026-07-31T13:00:00+00:00",
                        "2026-07-31T13:10:00+00:00",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO authorization_receipts(
                        receipt_id, approval_id, action_id, proposal_hash,
                        authorized_user_hash, credential_id_hash, authorized_at,
                        expires_at, assurance, consumed_at
                    ) VALUES ('RCPT-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
                              'AUTH-HISTORICAL00000000000000000001', ?, ?, ?, ?, ?, ?,
                              'passkey_verified', ?)
                    """,
                    (
                        "OPS-20260731-A1B2C3D4E5F6",
                        "sha256:" + "a" * 64,
                        OWNER_HASH,
                        sha256_text("credential"),
                        "2026-07-31T13:01:00+00:00",
                        "2026-07-31T13:10:00+00:00",
                        "2026-07-31T13:02:00+00:00",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO operation_executions(
                        action_id, receipt_id, proposal_hash, idempotency_key,
                        action_type, target, state, error_code,
                        recovery_resolution, recovery_evidence_hash,
                        recovery_resolved_at, started_at, updated_at, finished_at
                    ) VALUES (?, 'RCPT-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', ?, ?,
                              'restart_addon', 'example_addon', 'recovery_required',
                              'broker_restarted', 'confirmed_healthy', ?, ?, ?, ?, ?)
                    """,
                    (
                        "OPS-20260731-A1B2C3D4E5F6",
                        "sha256:" + "a" * 64,
                        "sha256:" + "b" * 64,
                        RECOVERY_EVIDENCE_HASH,
                        "2026-07-31T13:05:00+00:00",
                        "2026-07-31T13:02:00+00:00",
                        "2026-07-31T13:05:00+00:00",
                        "2026-07-31T13:05:00+00:00",
                    ),
                )
                legacy_queries = {
                    "operation_proposals": "SELECT action_id, idempotency_key, action_type, target, proposal_hash, risk_level, requires_backup, parameter_summary_json, expected_change, validation_plan_json, rollback_plan_json, created_at, expires_at FROM operation_proposals",
                    "authorization_requests": "SELECT approval_id, action_id, action_type, target, proposal_hash, risk_level, requires_backup, parameter_summary_json, expected_change, validation_plan_json, rollback_plan_json, structural_owner_hash, created_at, expires_at, state, proposal_origin FROM authorization_requests",
                    "authorization_receipts": "SELECT receipt_id, approval_id, action_id, proposal_hash, authorized_user_hash, credential_id_hash, authorized_at, expires_at, assurance, consumed_at FROM authorization_receipts",
                    "operation_executions": "SELECT action_id, receipt_id, proposal_hash, idempotency_key, action_type, target, state, preflight_json, postflight_json, error_code, recovery_resolution, recovery_evidence_hash, recovery_resolved_at, started_at, updated_at, finished_at FROM operation_executions",
                }
                before = {
                    table: connection.execute(query).fetchall()
                    for table, query in legacy_queries.items()
                }
            first = AuthorizationStore(database)
            with sqlite3.connect(database) as connection:
                after = {
                    table: connection.execute(query).fetchall()
                    for table, query in legacy_queries.items()
                }
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM backup_evidence").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM operation_leases").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT proposal_version, policy_epoch, policy_hash, allowlist_hash, adapter_version, adapter_schema_version, baseline_etag, backup_evidence_id FROM operation_proposals"
                    ).fetchone(),
                    (1, None, None, None, None, None, None, None),
                )
                triggers = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    )
                }
                self.assertTrue(
                    {"backup_evidence_no_update", "backup_evidence_no_delete"}.issubset(
                        triggers
                    )
                )
            self.assertEqual(after, before)
            second = AuthorizationStore(database)
            self.assertEqual(
                second.get_execution("OPS-20260731-A1B2C3D4E5F6")["recovery"],
                first.get_execution("OPS-20260731-A1B2C3D4E5F6")["recovery"],
            )
            with sqlite3.connect(database) as connection:
                repeated = {
                    table: connection.execute(query).fetchall()
                    for table, query in legacy_queries.items()
                }
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(repeated, before)

    def test_empty_database_and_0_3_history_migrate_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            empty_database = pathlib.Path(temporary) / "empty" / "passkeys.sqlite3"
            AuthorizationStore(empty_database)
            with sqlite3.connect(empty_database) as connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(operation_executions)"
                    )
                }
                user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertTrue(
                {
                    "recovery_resolution",
                    "recovery_evidence_hash",
                    "recovery_resolved_at",
                }.issubset(columns)
            )
            self.assertTrue(
                {
                    "proposal_version",
                    "policy_epoch",
                    "policy_hash",
                    "allowlist_hash",
                    "adapter_version",
                    "adapter_schema_version",
                    "baseline_etag",
                    "backup_evidence_id",
                    "lease_instance_id",
                    "lease_epoch",
                }.issubset(columns)
            )
            self.assertEqual(user_version, 6)
            with sqlite3.connect(empty_database) as connection:
                evidence_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(backup_evidence)")
                }
            self.assertTrue(
                {
                    "logical_id",
                    "scope",
                    "completed",
                    "created_at",
                    "size",
                    "sha256",
                    "off_device_sha256",
                    "readable",
                    "baseline",
                    "expires_at",
                    "registered_at",
                }.issubset(evidence_columns)
            )

            old_database = pathlib.Path(temporary) / "old" / "passkeys.sqlite3"
            old_database.parent.mkdir()
            with sqlite3.connect(old_database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE operation_executions (
                        action_id TEXT PRIMARY KEY,
                        receipt_id TEXT NOT NULL UNIQUE,
                        proposal_hash TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        action_type TEXT NOT NULL,
                        target TEXT NOT NULL,
                        state TEXT NOT NULL,
                        preflight_json TEXT,
                        postflight_json TEXT,
                        error_code TEXT,
                        started_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        finished_at TEXT
                    );
                    INSERT INTO operation_executions(
                        action_id, receipt_id, proposal_hash, idempotency_key,
                        action_type, target, state, error_code,
                        started_at, updated_at, finished_at
                    ) VALUES (
                        'OPS-20260731-A1B2C3D4E5F6',
                        'RCPT-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
                        'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                        'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                        'restart_addon', 'example_addon', 'recovery_required',
                        'broker_restarted',
                        '2026-07-31T13:00:00+00:00',
                        '2026-07-31T13:01:00+00:00',
                        '2026-07-31T13:01:00+00:00'
                    );
                    """
                )
            first = AuthorizationStore(old_database)
            historical = first.get_execution("OPS-20260731-A1B2C3D4E5F6")
            self.assertEqual(historical["state"], "recovery_required")
            self.assertEqual(
                historical["recovery"],
                {
                    "resolved": False,
                    "resolution": None,
                    "evidence_hash": None,
                    "resolved_at": None,
                },
            )
            second = AuthorizationStore(old_database)
            self.assertEqual(second.get_execution(historical["action_id"]), historical)
            with sqlite3.connect(old_database) as connection:
                raw = connection.execute(
                    """
                    SELECT state, error_code, started_at, updated_at, finished_at,
                           recovery_resolution, recovery_evidence_hash,
                           recovery_resolved_at
                    FROM operation_executions WHERE action_id = ?
                    """,
                    (historical["action_id"],),
                ).fetchone()
                user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(
                raw,
                (
                    "recovery_required",
                    "broker_restarted",
                    "2026-07-31T13:00:00+00:00",
                    "2026-07-31T13:01:00+00:00",
                    "2026-07-31T13:01:00+00:00",
                    None,
                    None,
                    None,
                ),
            )
            self.assertEqual(user_version, 6)


class AuthorizationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = AuthorizationStore(
            pathlib.Path(self.temporary.name) / "private" / "passkeys.sqlite3"
        )
        self.store.register_backup_evidence(
            make_backup_evidence(), registered_at=NOW
        )
        self.manager = AuthorizationManager(
            store=self.store,
            passkeys=FakePasskeys(),
            trusted_owner_hashes=frozenset({OWNER_HASH}),
            enrollment_token=ENROLLMENT_TOKEN,
            restart_addon_allowlist=frozenset({ALLOWED_ADDON}),
            baseline_provider=lambda _target: BASELINE_ETAG,
            clock=MutableClock(),
        )
        self.server = create_server(
            "127.0.0.1",
            0,
            api_token="a" * 32,
            recovery_api_token="r" * 32,
            backup_evidence_api_token="b" * 32,
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

    def test_backup_evidence_registration_and_query_require_dedicated_bearer(self) -> None:
        evidence = make_backup_evidence(logical_id="backup-api-20260731")
        for token in (None, "a" * 32, "r" * 32):
            headers = {} if token is None else {"Authorization": "Bearer " + token}
            with self.assertRaises(HTTPError) as context:
                self.request_json("/v1/backup-evidence", evidence, headers=headers)
            self.assertEqual(context.exception.code, 401)

            get_headers = {} if token is None else {"Authorization": "Bearer " + token}
            request = Request(
                self.base + "/v1/backup-evidence/" + evidence["logical_id"],
                headers=get_headers,
            )
            with self.assertRaises(HTTPError) as context:
                urlopen(request)
            self.assertEqual(context.exception.code, 401)

        created = self.request_json(
            "/v1/backup-evidence",
            evidence,
            headers={"Authorization": "Bearer " + "b" * 32},
        )
        self.assertEqual(created, evidence)
        request = Request(
            self.base + "/v1/backup-evidence/" + evidence["logical_id"],
            headers={"Authorization": "Bearer " + "b" * 32},
        )
        with urlopen(request) as response:
            self.assertEqual(json.loads(response.read()), evidence)

        self.assertEqual(
            self.request_json(
                "/v1/backup-evidence",
                evidence,
                headers={"Authorization": "Bearer " + "b" * 32},
            ),
            evidence,
        )
        with self.assertRaises(HTTPError) as context:
            self.request_json(
                "/v1/backup-evidence",
                {**evidence, "size": evidence["size"] + 1},
                headers={"Authorization": "Bearer " + "b" * 32},
            )
        self.assertEqual(context.exception.code, 409)

        with self.assertRaises(HTTPError) as context:
            self.request_json(
                "/v1/backup-evidence",
                {**evidence, "path": "/private/backup"},
                headers={"Authorization": "Bearer " + "b" * 32},
            )
        self.assertEqual(context.exception.code, 400)

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
