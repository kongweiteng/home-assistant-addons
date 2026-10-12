from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_controller import fnos_skill as fnos


class FnosSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cli = self.root / "trim-cli"
        self.cli.write_text("fixture", encoding="utf-8")
        self.cli.chmod(0o700)
        self.auth = self.root / "private-auth"
        self.auth.mkdir(mode=0o700)
        self.session = self.auth / ".config/trim-cli/profiles/home.json"
        self.session.parent.mkdir(mode=0o700, parents=True)
        self.session.write_text('{"fixture":true}', encoding="utf-8")
        self.session.chmod(0o600)
        self.paths = [patch.object(fnos, "FNOS_CLI_PATH", self.cli),
                      patch.object(fnos, "FNOS_AUTH_HOME", self.auth)]
        for patcher in self.paths:
            patcher.start()
        self.client = fnos.FnosSkillClient(enabled=True)

    def tearDown(self) -> None:
        for patcher in reversed(self.paths):
            patcher.stop()
        self.temporary.cleanup()

    def assert_error(self, expected: str, function, *args) -> fnos.FnosSkillError:
        with self.assertRaises(fnos.FnosSkillError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, expected)
        return caught.exception

    @staticmethod
    def response(value, *, returncode=0, stderr=b""):
        return subprocess.CompletedProcess([], returncode, json.dumps(value).encode(), stderr)

    def test_catalog_is_closed_and_requires_owner_job_for_controls(self) -> None:
        self.assertEqual(fnos.FNOS_TOOLS, {"fnos_query", "fnos_service_control"})
        for definition in fnos.FNOS_DEFINITIONS:
            self.assertFalse(definition.input_schema["additionalProperties"])
            self.assertTrue(definition.requires_job_context)
        self.assertTrue(fnos.FNOS_DEFINITIONS[1].idempotent_write)
        self.assertIn("owner", fnos.FNOS_DEFINITIONS[1].description)

    def test_disabled_missing_install_and_auth_fail_closed(self) -> None:
        self.assertFalse(fnos.FnosSkillClient().configured())
        self.assert_error("fnos_not_configured", fnos.FnosSkillClient().call, "fnos_query", {"query": "apps"})
        self.cli.unlink()
        self.assertFalse(self.client.configured())

    def test_pending_without_session_and_unsafe_session_metadata_are_not_configured(self) -> None:
        self.session.unlink()
        (self.session.parent / "home.oauth-pending.json").write_text('{"fixture":true}')
        self.assertFalse(self.client.configured())
        self.assert_error("fnos_not_configured", self.client.call, "fnos_query", {"query": "apps"})
        self.session.write_text('{"fixture":true}')
        self.session.chmod(0o644)
        self.assertFalse(self.client.configured())
        self.session.chmod(0o600)
        self.assertTrue(self.client.configured())
        self.session.write_text("")
        self.assertFalse(self.client.configured())

    def test_unknown_queries_arguments_and_command_injection_never_execute(self) -> None:
        invalid = [("raw", {}), ("fnos_query", {"query": "power"}),
                   ("fnos_query", {"query": "apps", "host": "example.invalid"}),
                   ("fnos_query", {"query": ["apps"]}), ("fnos_query", {})]
        with patch.object(fnos.subprocess, "run") as run:
            for name, args in invalid:
                self.assert_error("fnos_invalid_input", self.client.call, name, args)
            run.assert_not_called()

    def test_control_rejects_options_shell_paths_and_missing_job(self) -> None:
        args = {"target_type": "container", "action": "restart", "target": "example-service"}
        with patch.object(fnos.subprocess, "run") as run:
            for target in ("--all", "a;b", "$(id)", "../service", "/service", "a\nb", "*", "a b"):
                self.assert_error("fnos_invalid_input", self.client.call, "fnos_service_control", {**args, "target": target}, "job-1")
            self.assert_error("fnos_job_required", self.client.call, "fnos_service_control", args)
            self.assert_error("fnos_invalid_input", self.client.call, "fnos_service_control", {**args, "action": "rm"}, "job-1")
            run.assert_not_called()

    def test_app_restart_is_rejected_before_execution(self) -> None:
        with patch.object(fnos.subprocess, "run") as run:
            self.assert_error("fnos_unsupported_action", self.client.call, "fnos_service_control",
                              {"target_type": "app", "action": "restart", "target": "trim.example"}, "job-1")
            run.assert_not_called()

    def test_container_query_uses_all_fixed_profile_private_home_and_no_shell(self) -> None:
        value = [{"Id": "ab1234", "Names": ["/example-service"], "State": "running",
                  "Labels": {"secret": "fixture-secret"}, "Env": ["TOKEN=fixture-secret"],
                  "Command": "echo fixture-secret", "access_token": "fixture-secret"}]
        with patch.dict(os.environ, {"PRIVATE_BEARER": "fixture-secret"}), patch.object(fnos.subprocess, "run", return_value=self.response(value)) as run:
            result = self.client.call("fnos_query", {"query": "containers"})
        self.assertEqual(result["data"], [{"Id": "ab1234", "Names": ["example-service"], "State": "running"}])
        self.assertNotIn("fixture-secret", json.dumps(result))
        positional, kwargs = run.call_args
        self.assertEqual(positional[0], [str(self.cli), "--profile", "home", "docker", "container", "ls", "--all"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["env"]["HOME"], str(self.auth))
        self.assertEqual(kwargs["env"]["TRIM_CLI_SESSION_STORAGE"], "file")
        self.assertNotIn("PRIVATE_BEARER", kwargs["env"])

    def test_system_and_list_envelopes_preserve_safe_fields_only(self) -> None:
        values = ["example-nas", {"version": "1.2.0800", "refresh_token": "fixture-secret"},
                  {"data": [{"appName": "trim.example", "version": "1.0", "state": "running",
                              "config": {"password": "fixture-secret"}}]}]
        with patch.object(fnos.subprocess, "run", side_effect=[self.response(value) for value in values]):
            self.assertEqual(self.client.call("fnos_query", {"query": "host_name"})["data"], "example-nas")
            self.assertEqual(self.client.call("fnos_query", {"query": "version"})["data"], {"version": "1.2.0800"})
            result = self.client.call("fnos_query", {"query": "apps"})
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertEqual(result["data"]["data"][0]["appName"], "trim.example")

    def test_missing_list_is_unknown_and_explicit_empty_list_is_known(self) -> None:
        with patch.object(fnos.subprocess, "run", side_effect=[self.response({"result": "succ"}), self.response([])]):
            missing = self.client.call("fnos_query", {"query": "containers"})
            empty = self.client.call("fnos_query", {"query": "containers"})
        self.assertFalse(missing["data_available"])
        self.assertEqual(missing["data"], {})
        self.assertTrue(empty["data_available"])
        self.assertEqual(empty["data"], [])

    def test_docker_container_id_metrics_become_bounded_safe_records(self) -> None:
        container_id = "a" * 64
        value = {container_id: {"cpuUsage": 0.5, "networkRx": 120, "networkTx": 240, "usedMem": 4096,
                                "Env": ["TOKEN=fixture-secret"], "labels": {"token": "fixture-secret"}},
                 "fixture-secret": {"cpuUsage": 99}, "b" * 64: {"cpuUsage": True, "networkRx": -1,
                                                             "networkTx": "fixture-secret", "usedMem": float("inf")}}
        with patch.object(fnos.subprocess, "run", return_value=self.response(value)):
            result = self.client.call("fnos_query", {"query": "docker_stats"})
        self.assertEqual(result["data"], [{"id": container_id, "cpuUsage": 0.5, "networkRx": 120,
                                         "networkTx": 240, "usedMem": 4096}, {"id": "b" * 64}])
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertTrue(result["data_available"])
        oversized = {f"{number:064x}": {"cpuUsage": 1} for number in range(1001)}
        with patch.object(fnos.subprocess, "run", return_value=self.response(oversized)):
            self.assert_error("fnos_invalid_response", self.client.call, "fnos_query", {"query": "docker_stats"})

    def test_storage_array_and_raid_summary_omit_disk_mountpoint_and_comments(self) -> None:
        value = {"array": [{"name": "示例存储池 (1)", "uuid": "example-pool", "storId": 1,
                             "size": 1024, "frsize": 4096, "fssize": 256, "fstype": "btrfs", "level": 1,
                             "comment": "fixture-secret", "mountpoint": "/private/fixture-secret",
                             "md": [{"arrayState": "clean", "bitmapType": "internal", "level": 1,
                                     "name": "example-raid", "raidDisks": 2, "size": 1024,
                                     "syncAction": "idle", "syncCompleted": "100%", "uuid": "example-raid-id",
                                     "disk": [{"serial": "fixture-secret", "device": "fixture-secret"}]}]}],
                 "result": "succ", "access_token": "fixture-secret"}
        with patch.object(fnos.subprocess, "run", return_value=self.response(value)):
            result = self.client.call("fnos_query", {"query": "storage_pools"})
        pool = result["data"]["array"][0]
        self.assertEqual(pool["name"], "示例存储池 (1)")
        self.assertEqual(pool["storId"], 1)
        self.assertEqual(pool["fstype"], "btrfs")
        self.assertEqual(pool["md"][0]["raidDisks"], 2)
        self.assertEqual(pool["md"][0]["syncCompleted"], "100%")
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertNotIn("disk", pool["md"][0])

    def test_human_labels_support_cjk_but_do_not_relax_control_target(self) -> None:
        value = {"list": [{"appName": "trim.example", "name": "示例应用（测试）", "status": 1}], "total": 1}
        with patch.object(fnos.subprocess, "run", return_value=self.response(value)):
            result = self.client.call("fnos_query", {"query": "apps"})
        self.assertEqual(result["data"]["list"][0]["name"], "示例应用（测试）")
        self.assertEqual(result["data"]["total"], 1)
        with patch.object(fnos.subprocess, "run") as run:
            self.assert_error("fnos_invalid_input", self.client.call, "fnos_service_control",
                              {"target_type": "app", "action": "start", "target": "示例应用"}, "job-1")
            run.assert_not_called()

    def test_auth_timeout_and_os_errors_never_expose_stderr_or_exception(self) -> None:
        with patch.object(fnos.subprocess, "run", return_value=self.response({}, returncode=1, stderr=b"401 access_token=fixture-secret")):
            auth = self.assert_error("fnos_auth_required", self.client.call, "fnos_query", {"query": "apps"})
            self.assertNotIn("fixture-secret", str(auth))
        with patch.object(fnos.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture-secret", 30, output=b"fixture-secret")):
            timeout = self.assert_error("fnos_timeout", self.client.call, "fnos_query", {"query": "apps"})
            self.assertNotIn("fixture-secret", str(timeout))
        with patch.object(fnos.subprocess, "run", side_effect=OSError("fixture-secret")):
            unavailable = self.assert_error("fnos_unavailable", self.client.call, "fnos_query", {"query": "apps"})
            self.assertNotIn("fixture-secret", str(unavailable))

    def test_business_errors_and_malformed_json_are_safe(self) -> None:
        values = [{"result": "fail", "errmsg": "fixture-secret"}, {"errno": 401, "errmsg": "fixture-secret"},
                  {"code": 403, "message": "fixture-secret"}]
        with patch.object(fnos.subprocess, "run", side_effect=[self.response(value) for value in values]):
            for _ in values:
                error = self.assert_error("fnos_unavailable", self.client.call, "fnos_query", {"query": "apps"})
                self.assertNotIn("fixture-secret", str(error))
        with patch.object(fnos.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"fixture-secret", b"")):
            self.assert_error("fnos_invalid_response", self.client.call, "fnos_query", {"query": "apps"})

    def test_symlinked_auth_or_ledger_never_executes_or_overwrites_destination(self) -> None:
        import shutil
        shutil.rmtree(self.auth)
        other = self.root / "other"
        other.mkdir()
        self.auth.symlink_to(other)
        with patch.object(fnos.subprocess, "run") as run:
            self.assert_error("fnos_not_configured", self.client.call, "fnos_query", {"query": "apps"})
            run.assert_not_called()
        self.auth.unlink()
        self.auth.mkdir(mode=0o700)
        self.session.parent.mkdir(mode=0o700, parents=True)
        self.session.write_text('{"fixture":true}')
        self.session.chmod(0o600)
        destination = self.root / "untouched"
        destination.write_text("untouched", encoding="utf-8")
        (self.auth / "service-control.json").symlink_to(destination)
        with patch.object(fnos.subprocess, "run") as run:
            self.assert_error("fnos_private_state_invalid", self.client.call, "fnos_service_control",
                              {"target_type": "app", "action": "start", "target": "trim.example"}, "job-1")
            run.assert_not_called()
        self.assertEqual(destination.read_text(), "untouched")

    def test_control_is_durably_deduplicated_and_output_does_not_leak(self) -> None:
        args = {"target_type": "container", "action": "restart", "target": "example-service"}
        with patch.object(fnos.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"fixture-secret", b"fixture-secret")) as run:
            first = self.client.call("fnos_service_control", args, "job-1")
            replay = fnos.FnosSkillClient(enabled=True).call("fnos_service_control", args, "job-1")
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][-4:], ["container", "restart", "example-service", "--yes"])
        self.assertEqual(first["status"], "submitted")
        self.assertEqual(first["verification"], "not_performed")
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertNotIn("fixture-secret", json.dumps(first))
        self.assertEqual(self.auth.stat().st_mode & 0o777, 0o700)
        for name in ("service-control.json", "skill.lock"):
            self.assertEqual((self.auth / name).stat().st_mode & 0o777, 0o600)
        self.assertNotIn("job-1", (self.auth / "service-control.json").read_text())

    def test_unknown_control_cannot_be_replayed_but_different_job_is_distinct(self) -> None:
        args = {"target_type": "container", "action": "restart", "target": "example-service"}
        with patch.object(fnos.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 30)) as run:
            self.assert_error("fnos_control_unknown", self.client.call, "fnos_service_control", args, "job-1")
            self.assert_error("fnos_control_unknown", self.client.call, "fnos_service_control", args, "job-1")
            run.assert_called_once()
        with patch.object(fnos.subprocess, "run", return_value=self.response({"result": "succ"})) as run:
            self.client.call("fnos_service_control", args, "job-2")
            run.assert_called_once()

    def test_preexisting_pending_or_corrupt_ledger_fails_closed(self) -> None:
        args = {"target_type": "app", "action": "stop", "target": "trim.example"}
        key = hashlib.sha256(json.dumps(["job-1", "app", "stop", "trim.example"], separators=(",", ":")).encode()).hexdigest()
        path = self.auth / "service-control.json"
        path.write_text(json.dumps({key: "pending"}), encoding="utf-8")
        with patch.object(fnos.subprocess, "run") as run:
            self.assert_error("fnos_control_unknown", self.client.call, "fnos_service_control", args, "job-1")
            path.write_text("corrupt", encoding="utf-8")
            self.assert_error("fnos_private_state_invalid", self.client.call, "fnos_service_control", args, "job-1")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
