"""Tests for the non-executing Hermes HA operations approval protocol."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "hermes_agent"
PLUGIN_DIR = ADDON / "bundled_plugins" / "ha-operations-approval"
INIT_PATH = PLUGIN_DIR / "__init__.py"
LEDGER_PATH = PLUGIN_DIR / "ledger.py"
MANAGED_SHELL = ADDON / "managed-plugins.sh"
RUN_PATH = ADDON / "run.sh"
PROFILE_INIT_PATH = ADDON / "profile-init.sh"
CONFIG_PATH = ADDON / "config.yaml"
DOCKERFILE_PATH = ADDON / "Dockerfile"


def _load_plugin():
    package_name = "ha_operations_approval_test_plugin"
    for name in (package_name, f"{package_name}.ledger"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        package_name,
        INIT_PATH,
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load HA operations approval plugin")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


PLUGIN = _load_plugin()
LEDGER = sys.modules[f"{PLUGIN.__name__}.ledger"]


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def proposal_args(action_type: str = "check_ha_config") -> dict:
    return {
        "action_type": action_type,
        "target": "home-assistant-core" if action_type == "check_ha_config" else "example-addon",
        "parameter_summary": json.dumps({"version": "1.2.3"}),
        "requires_backup": action_type != "check_ha_config",
        "expected_change": "Validate the requested Home Assistant operation.",
        "validation_plan": json.dumps(["Check configuration", "Verify health"]),
        "rollback_plan": json.dumps(["Stop before execution", "Keep current state"]),
    }


class ApprovalLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.clock = MutableClock()
        self.db_path = Path(self.tempdir.name) / "state" / "approval.sqlite3"
        self.ledger = LEDGER.ApprovalLedger(
            self.db_path,
            ttl_seconds=600,
            max_pending=3,
            clock=self.clock,
        )
        self.owner_a = LEDGER.identity_hash("weixin", "owner-a")
        self.owner_b = LEDGER.identity_hash("weixin", "owner-b")

    def test_action_risk_is_code_owned(self):
        low = LEDGER.normalize_proposal_request(proposal_args())
        high = LEDGER.normalize_proposal_request(proposal_args("install_addon"))
        self.assertEqual(low["risk_level"], "L1")
        self.assertEqual(high["risk_level"], "L3")

    def test_l3_proposal_requires_backup(self):
        args = proposal_args("install_addon")
        args["requires_backup"] = False
        with self.assertRaisesRegex(LEDGER.ProposalError, "must require a backup"):
            LEDGER.normalize_proposal_request(args)

    def test_unknown_action_and_path_target_are_rejected(self):
        args = proposal_args()
        args["action_type"] = "shell_exec"
        with self.assertRaises(LEDGER.ProposalError):
            LEDGER.normalize_proposal_request(args)
        args = proposal_args()
        args["target"] = "../../config"
        with self.assertRaises(LEDGER.ProposalError):
            LEDGER.normalize_proposal_request(args)

    def test_sensitive_parameter_keys_and_values_are_rejected(self):
        for parameters in (
            {"api_token": "redacted"},
            {"note": "Bearer abcdef"},
            {"note": "sk-abcdefghijklmnopqrstuvwxyz"},
            {"source": "https://user:pass@example.invalid/repo"},
            {"source": "https://example.invalid/repo?token=hidden"},
        ):
            args = proposal_args()
            args["parameter_summary"] = json.dumps(parameters)
            with self.subTest(parameters=parameters):
                with self.assertRaises(LEDGER.ProposalError):
                    LEDGER.normalize_proposal_request(args)

    def test_safe_nested_parameter_summary_is_canonical(self):
        first = LEDGER.sanitize_parameter_summary(
            {"slug": "example", "settings": {"mode": "safe", "ports": [1, 2]}}
        )
        second = LEDGER.sanitize_parameter_summary(
            {"settings": {"ports": [1, 2], "mode": "safe"}, "slug": "example"}
        )
        self.assertEqual(LEDGER.canonical_json(first), LEDGER.canonical_json(second))

    def test_create_persists_only_bounded_audit_fields(self):
        proposal = self.ledger.create(proposal_args())
        self.assertRegex(proposal["action_id"], LEDGER.ACTION_ID_RE)
        self.assertRegex(proposal["proposal_hash"], r"^sha256:[a-f0-9]{64}$")
        self.assertEqual(proposal["state"], "awaiting_approval")
        self.assertEqual(stat.S_IMODE(self.db_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.db_path.parent.stat().st_mode), 0o700)
        connection = sqlite3.connect(self.db_path)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(operations)").fetchall()
        }
        connection.close()
        self.assertNotIn("parameter_summary", columns)
        self.assertNotIn("user_id", columns)
        self.assertNotIn("notification_body", columns)

    def test_l1_approval_is_immediate_and_idempotent(self):
        proposal = self.ledger.create(proposal_args())
        first = self.ledger.approve(proposal["action_id"], self.owner_a)
        second = self.ledger.approve(proposal["action_id"], self.owner_a)
        self.assertEqual(first["state"], "approved")
        self.assertEqual(second["state"], "approved")
        self.assertEqual(first["approved_at"], second["approved_at"])

    def test_l3_requires_same_owner_challenge_confirmation(self):
        proposal = self.ledger.create(proposal_args("install_addon"))
        first = self.ledger.approve(proposal["action_id"], self.owner_a)
        challenge = first["confirmation_challenge"]
        self.assertEqual(first["state"], "awaiting_confirmation")
        with self.assertRaises(LEDGER.ProposalError):
            self.ledger.confirm(proposal["action_id"], self.owner_b, challenge)
        with self.assertRaises(LEDGER.ProposalError):
            self.ledger.confirm(proposal["action_id"], self.owner_a, "DEADBEEF")
        result = self.ledger.confirm(proposal["action_id"], self.owner_a, challenge)
        self.assertEqual(result["state"], "approved")
        self.assertNotIn("confirmation_challenge", self.ledger.get(proposal["action_id"]))

    def test_l3_duplicate_first_approval_does_not_issue_new_challenge(self):
        proposal = self.ledger.create(proposal_args("restart_core"))
        first = self.ledger.approve(proposal["action_id"], self.owner_a)
        second = self.ledger.approve(proposal["action_id"], self.owner_a)
        self.assertIn("confirmation_challenge", first)
        self.assertTrue(second["confirmation_required"])
        self.assertNotIn("confirmation_challenge", second)

    def test_cancel_is_terminal_and_idempotent(self):
        proposal = self.ledger.create(proposal_args())
        first = self.ledger.cancel(proposal["action_id"], self.owner_a)
        second = self.ledger.cancel(proposal["action_id"], self.owner_a)
        self.assertEqual(first["state"], "cancelled")
        self.assertEqual(second["state"], "cancelled")

    def test_confirmation_can_only_be_cancelled_by_same_owner(self):
        proposal = self.ledger.create(proposal_args("install_addon"))
        self.ledger.approve(proposal["action_id"], self.owner_a)
        with self.assertRaises(LEDGER.ProposalError):
            self.ledger.cancel(proposal["action_id"], self.owner_b)
        result = self.ledger.cancel(proposal["action_id"], self.owner_a)
        self.assertEqual(result["state"], "cancelled")

    def test_expired_id_cannot_be_approved(self):
        proposal = self.ledger.create(proposal_args())
        self.clock.value += timedelta(seconds=601)
        result = self.ledger.approve(proposal["action_id"], self.owner_a)
        self.assertEqual(result["state"], "expired")
        self.assertEqual(result["error_code"], "approval_ttl_expired")

    def test_pending_limit_is_enforced(self):
        for _ in range(3):
            self.ledger.create(proposal_args())
        with self.assertRaisesRegex(LEDGER.ProposalError, "Too many pending"):
            self.ledger.create(proposal_args())

    def test_raw_owner_identity_is_not_stored(self):
        proposal = self.ledger.create(proposal_args())
        self.ledger.approve(proposal["action_id"], self.owner_a)
        self.assertNotIn(b"owner-a", self.db_path.read_bytes())


class FakeContext:
    def __init__(self) -> None:
        self.hooks = {}
        self.commands = {}
        self.tools = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, **metadata):
        self.commands[name] = {"handler": handler, **metadata}

    def register_tool(self, name, handler, **metadata):
        self.tools[name] = {"handler": handler, **metadata}


class PluginBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.owner_id = "opaque-weixin-owner"
        self.owner_hash = LEDGER.identity_hash("weixin", self.owner_id)
        self.env = patch.dict(
            os.environ,
            {
                "HA_OPERATIONS_APPROVAL_ENABLED": "true",
                "HA_OPERATIONS_OWNER_IDENTITY_HASHES": self.owner_hash,
                "HA_OPERATIONS_LEDGER_PATH": str(Path(self.tempdir.name) / "ledger.sqlite3"),
                "HA_OPERATIONS_PROPOSAL_TTL_SECONDS": "600",
                "HA_OPERATIONS_MAX_PENDING": "20",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        PLUGIN._CURRENT_ACTOR.set(None)

    @staticmethod
    def event(*, platform="weixin", chat_type="dm", user_id="user", text=""):
        source = types.SimpleNamespace(
            platform=types.SimpleNamespace(value=platform),
            chat_type=chat_type,
            user_id=user_id,
        )
        return types.SimpleNamespace(source=source, internal=False, text=text)

    def test_registers_only_model_free_commands_and_nonexecuting_tools(self):
        context = FakeContext()
        PLUGIN.register(context)
        self.assertEqual(
            set(context.commands), {"ha-approve", "ha-confirm", "ha-cancel"}
        )
        self.assertEqual(
            set(context.tools),
            {"ha_create_operation_proposal", "ha_get_operation_proposal_status"},
        )
        self.assertEqual(set(context.hooks), {"pre_gateway_dispatch"})
        source = INIT_PATH.read_text()
        for forbidden in ("aiohttp", "requests", "subprocess", "supervisor", "hassio"):
            self.assertNotIn(forbidden, source.lower())

    def test_enabled_without_owner_fails_closed(self):
        with patch.dict(os.environ, {"HA_OPERATIONS_OWNER_IDENTITY_HASHES": ""}):
            with self.assertRaisesRegex(RuntimeError, "without an owner"):
                PLUGIN.register(FakeContext())

    def test_owner_dm_can_approve_l1_before_model_dispatch(self):
        proposal = LEDGER.ApprovalLedger.from_env().create(proposal_args())
        event = self.event(user_id=self.owner_id, text=f"/ha-approve {proposal['action_id']}")
        self.assertIsNone(PLUGIN._capture_gateway_actor(event=event))
        reply = PLUGIN._approve_command(proposal["action_id"])
        self.assertIn("state: approved", reply)

    def test_group_other_platform_and_other_user_are_rejected(self):
        proposal = LEDGER.ApprovalLedger.from_env().create(proposal_args())
        for event in (
            self.event(user_id=self.owner_id, chat_type="group"),
            self.event(user_id=self.owner_id, platform="telegram"),
            self.event(user_id="other-user"),
        ):
            PLUGIN._capture_gateway_actor(event=event)
            with self.subTest(event=event):
                self.assertEqual(
                    PLUGIN._approve_command(proposal["action_id"]),
                    "Approval command unavailable or not authorized.",
                )

    def test_natural_language_does_not_change_state(self):
        proposal = LEDGER.ApprovalLedger.from_env().create(proposal_args())
        event = self.event(user_id=self.owner_id, text=f"I approve {proposal['action_id']}")
        self.assertIsNone(PLUGIN._capture_gateway_actor(event=event))
        self.assertEqual(
            LEDGER.ApprovalLedger.from_env().get(proposal["action_id"])["state"],
            "awaiting_approval",
        )

    def test_l3_command_requires_challenge(self):
        proposal = LEDGER.ApprovalLedger.from_env().create(proposal_args("install_addon"))
        PLUGIN._capture_gateway_actor(event=self.event(user_id=self.owner_id))
        reply = PLUGIN._approve_command(proposal["action_id"])
        self.assertIn("/ha-confirm", reply)
        challenge = reply.split(" /ha-confirm ", 1)[1].split()[1]
        confirmed = PLUGIN._confirm_command(f"{proposal['action_id']} {challenge}")
        self.assertIn("state: approved", confirmed)

    def test_tool_rejects_secret_without_echoing_it(self):
        args = proposal_args()
        args["parameter_summary"] = json.dumps({"api_token": "sk-super-secret-value"})
        result = json.loads(PLUGIN._create_proposal(args))
        rendered = json.dumps(result)
        self.assertEqual(result["error"]["code"], "sensitive_parameter")
        self.assertNotIn("sk-super-secret-value", rendered)


class AddonIntegrationTests(unittest.TestCase):
    def test_addon_metadata_and_runtime_contract(self):
        config = CONFIG_PATH.read_text()
        run = RUN_PATH.read_text()
        profile_init = PROFILE_INIT_PATH.read_text()
        dockerfile = DOCKERFILE_PATH.read_text()
        self.assertIn('version: "1.8.0"', config)
        self.assertIn("ha_operations_approval_enabled: false", config)
        self.assertIn("match(^[a-f0-9]{64}$)", config)
        self.assertIn("managed_plugins_install", run)
        self.assertIn("primary profile only", run)
        self.assertIn("HA_OPERATIONS_LEDGER_PATH", run)
        self.assertIn("HA_OPERATIONS_OWNER_IDENTITY_HASHES", profile_init)
        self.assertIn("COPY bundled_plugins", dockerfile)
        self.assertIn("COPY managed-plugins.sh", dockerfile)

    def test_managed_plugin_installs_every_profile_but_enables_only_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "primary"
            secondary = root / "secondary"
            primary.mkdir()
            secondary.mkdir()
            log = root / "python.log"
            stub = root / "python-stub"
            stub.write_text(
                "#!/bin/sh\nprintf '%s|%s\\n' \"$HERMES_HOME\" \"$*\" >> \"$PLUGIN_TEST_LOG\"\n"
            )
            stub.chmod(0o755)
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                PROFILE_HOMES=({primary!s} {secondary!s})
                PROFILE_NAMES=(primary secondary)
                HA_OPERATIONS_APPROVAL_ENABLED=true
                HA_OPERATIONS_OWNER_IDENTITY_HASHES={'a' * 64}
                export PLUGIN_TEST_LOG={log!s}
                source {MANAGED_SHELL!s}
                managed_plugins_install {stub!s}
                """
            )
            result = subprocess.run(
                ["/bin/bash", "-c", script],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for home in (primary, secondary):
                target = home / "plugins" / "ha-operations-approval"
                self.assertTrue((target / ".managed-by-hermes-addon").exists())
                self.assertTrue((target / "plugin.yaml").exists())
                self.assertEqual(stat.S_IMODE((target / "ledger.py").stat().st_mode), 0o444)
            calls = log.read_text().splitlines()
            self.assertEqual(len(calls), 2)
            self.assertIn("cmd_enable", calls[0])
            self.assertIn("allow_tool_override=False", calls[0])
            self.assertIn("cmd_disable", calls[1])

    def test_unmanaged_reserved_plugin_conflict_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conflict = root / "plugins" / "ha-operations-approval"
            conflict.mkdir(parents=True)
            (conflict / "user-file.txt").write_text("preserve")
            stub = root / "python-stub"
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                PROFILE_HOMES=({root!s})
                PROFILE_NAMES=(primary)
                HA_OPERATIONS_APPROVAL_ENABLED=false
                HA_OPERATIONS_OWNER_IDENTITY_HASHES=
                source {MANAGED_SHELL!s}
                managed_plugins_install {stub!s}
                """
            )
            result = subprocess.run(
                ["/bin/bash", "-c", script],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unmanaged plugin conflicts", result.stdout)
            self.assertEqual((conflict / "user-file.txt").read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
