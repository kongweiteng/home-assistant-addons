"""Regression tests for the restricted Hermes Home Assistant tool."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "hermes_agent" / "homeassistant_tool.py"
HEALTH_PATH = ROOT / "hermes_agent" / "homeassistant_health.py"
CONFIG_PATH = ROOT / "hermes_agent" / "config.yaml"
RUN_PATH = ROOT / "hermes_agent" / "run.sh"
PROFILE_INIT_PATH = ROOT / "hermes_agent" / "profile-init.sh"
DOCKERFILE_PATH = ROOT / "hermes_agent" / "Dockerfile"


class _Registry:
    def __init__(self) -> None:
        self.names = []

    def register(self, **kwargs) -> None:
        self.names.append(kwargs["name"])


def _load_tool_module():
    tools_module = types.ModuleType("tools")
    tools_module.__path__ = []
    registry_module = types.ModuleType("tools.registry")
    registry_module.registry = _Registry()
    registry_module.tool_error = lambda message: json.dumps({"error": message})

    old_tools = sys.modules.get("tools")
    old_registry = sys.modules.get("tools.registry")
    old_health = sys.modules.get("tools.homeassistant_health")
    sys.modules["tools"] = tools_module
    sys.modules["tools.registry"] = registry_module
    try:
        health_spec = importlib.util.spec_from_file_location(
            "tools.homeassistant_health", HEALTH_PATH
        )
        if health_spec is None or health_spec.loader is None:
            raise RuntimeError("Unable to load Home Assistant health helper")
        health_module = importlib.util.module_from_spec(health_spec)
        sys.modules["tools.homeassistant_health"] = health_module
        health_spec.loader.exec_module(health_module)
        spec = importlib.util.spec_from_file_location(
            "hermes_addon_homeassistant_tool", TOOL_PATH
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("Unable to load restricted Home Assistant tool")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, registry_module.registry
    finally:
        if old_tools is None:
            sys.modules.pop("tools", None)
        else:
            sys.modules["tools"] = old_tools
        if old_registry is None:
            sys.modules.pop("tools.registry", None)
        else:
            sys.modules["tools.registry"] = old_registry
        if old_health is None:
            sys.modules.pop("tools.homeassistant_health", None)
        else:
            sys.modules["tools.homeassistant_health"] = old_health


TOOL, REGISTRY = _load_tool_module()


class AddonContractTests(unittest.TestCase):
    def test_addon_uses_supervisor_core_api_permission(self):
        config = CONFIG_PATH.read_text()
        self.assertIn("homeassistant_api: true", config)
        self.assertNotIn("hassio_api: true", config)
        self.assertIn('version: "1.7.0"', config)
        self.assertRegex(
            config,
            r'(?ms)ha_control_allowed_domains:\n\s+- "light"\n\s+ha_control_allowed_entities: \[\]',
        )

    def test_runtime_prefers_supervisor_token_when_no_explicit_token(self):
        run = RUN_PATH.read_text()
        self.assertIn('HASS_TOKEN="$SUPERVISOR_TOKEN"', run)
        self.assertIn('HASS_URL="http://supervisor/core"', run)
        self.assertIn('export HASS_ALLOWED_DOMAINS HASS_ALLOWED_ENTITIES', run)
        self.assertIn("HASS_HEALTH_CONFIG_B64", run)
        self.assertIn("chmod 600 /config/.hermes_profile", run)

    def test_restricted_tool_is_installed_after_upstream_setup(self):
        dockerfile = DOCKERFILE_PATH.read_text()
        run = RUN_PATH.read_text()
        self.assertIn(
            "COPY homeassistant_tool.py /usr/local/share/hermes-addon/homeassistant_tool.py",
            dockerfile,
        )
        self.assertIn(
            "COPY homeassistant_health.py /usr/local/share/hermes-addon/homeassistant_health.py",
            dockerfile,
        )
        self.assertGreater(run.index("HASS_TOOL_OVERRIDE="), run.index("install_hermes_core\n"))
        self.assertIn(
            'install -m 0644 "$HASS_TOOL_OVERRIDE" "$SRC_DIR/tools/homeassistant_tool.py"',
            run,
        )
        self.assertIn(
            'install -m 0644 "$HASS_HEALTH_OVERRIDE" "$SRC_DIR/tools/homeassistant_health.py"',
            run,
        )

    def test_security_policy_environment_cannot_be_overridden_by_env_vars(self):
        profile_init = PROFILE_INIT_PATH.read_text()
        self.assertIn("HASS_ALLOWED_DOMAINS", profile_init)
        self.assertIn("HASS_ALLOWED_ENTITIES", profile_init)
        self.assertIn("HASS_HEALTH_CONFIG_B64", profile_init)


class ToolPolicyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "HASS_ALLOWED_DOMAINS": "light",
                "HASS_ALLOWED_ENTITIES": "",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_expected_tools_are_registered(self):
        self.assertEqual(
            set(REGISTRY.names),
            {
                "ha_list_entities",
                "ha_get_state",
                "ha_health_snapshot",
                "ha_list_services",
                "ha_call_service",
            },
        )

    def test_default_write_domain_is_only_light(self):
        self.assertEqual(TOOL._allowed_control_domains(), frozenset({"light"}))

    def test_explicit_empty_domain_list_disables_all_writes(self):
        with patch.dict(os.environ, {"HASS_ALLOWED_DOMAINS": ""}, clear=False):
            self.assertEqual(TOOL._allowed_control_domains(), frozenset())

    def test_switch_is_rejected_until_explicitly_enabled(self):
        result = json.loads(
            TOOL._handle_call_service(
                {
                    "domain": "switch",
                    "service": "turn_on",
                    "entity_id": "switch.test",
                }
            )
        )
        self.assertIn("not enabled", result["error"])

    def test_switch_domain_requires_an_exact_entity_allowlist(self):
        with patch.dict(
            os.environ,
            {"HASS_ALLOWED_DOMAINS": "light,switch", "HASS_ALLOWED_ENTITIES": ""},
            clear=False,
        ):
            result = json.loads(
                TOOL._handle_call_service(
                    {
                        "domain": "switch",
                        "service": "turn_on",
                        "entity_id": "switch.test",
                    }
                )
            )
        self.assertIn("requires an exact entity allowlist", result["error"])

    def test_entity_is_mandatory_and_must_match_domain(self):
        missing = json.loads(
            TOOL._handle_call_service({"domain": "light", "service": "turn_on"})
        )
        self.assertIn("entity_id", missing["error"])

        mismatch = json.loads(
            TOOL._handle_call_service(
                {
                    "domain": "light",
                    "service": "turn_on",
                    "entity_id": "switch.test",
                }
            )
        )
        self.assertIn("must match", mismatch["error"])

    def test_target_expansion_and_unknown_parameters_are_rejected(self):
        target = json.loads(
            TOOL._handle_call_service(
                {
                    "domain": "light",
                    "service": "turn_on",
                    "entity_id": "light.test",
                    "data": '{"area_id":"living_room"}',
                }
            )
        )
        self.assertIn("Target expansion", target["error"])

        unknown = json.loads(
            TOOL._handle_call_service(
                {
                    "domain": "light",
                    "service": "turn_on",
                    "entity_id": "light.test",
                    "data": '{"rgb_color":[255,0,0]}',
                }
            )
        )
        self.assertIn("Unsupported", unknown["error"])

    def test_entity_allowlist_is_exact(self):
        with patch.dict(
            os.environ,
            {"HASS_ALLOWED_ENTITIES": "light.allowed"},
            clear=False,
        ):
            result = json.loads(
                TOOL._handle_call_service(
                    {
                        "domain": "light",
                        "service": "turn_on",
                        "entity_id": "light.other",
                    }
                )
            )
        self.assertIn("not in the control allowlist", result["error"])

    def test_allowed_light_call_returns_verified_result(self):
        def fake_run(coro):
            coro.close()
            return {
                "success": True,
                "service": "light.turn_on",
                "affected_entities": [{"entity_id": "light.test", "state": "on"}],
                "verification": {
                    "expected_state": "on",
                    "actual_state": "on",
                    "verified": True,
                },
            }

        with patch.object(TOOL, "_run_async", side_effect=fake_run):
            result = json.loads(
                TOOL._handle_call_service(
                    {
                        "domain": "light",
                        "service": "turn_on",
                        "entity_id": "light.test",
                        "data": '{"brightness_pct":50}',
                    }
                )
            )
        self.assertTrue(result["result"]["success"])
        self.assertTrue(result["result"]["verification"]["verified"])

    def test_security_sensitive_read_domains_are_rejected(self):
        for entity_id in ("lock.front_door", "camera.front_door", "person.owner"):
            with self.subTest(entity_id=entity_id):
                result = json.loads(TOOL._handle_get_state({"entity_id": entity_id}))
                self.assertIn("not available", result["error"])

    def test_sensitive_attributes_are_removed(self):
        attributes = TOOL._safe_attributes(
            {
                "friendly_name": "Living Room",
                "access_token": "secret",
                "entity_picture": "/api/camera_proxy/camera.test?token=secret",
                "custom_secret_value": "secret",
            }
        )
        self.assertEqual(attributes, {"friendly_name": "Living Room"})

    def test_health_snapshot_handler_returns_versioned_result(self):
        def fake_run(coro):
            coro.close()
            return {
                "version": 1,
                "status": "unavailable",
                "disk": {"total_bytes": None},
            }

        with patch.object(TOOL, "_run_async", side_effect=fake_run):
            result = json.loads(TOOL._handle_health_snapshot({}))
        self.assertEqual(result["result"]["version"], 1)
        self.assertEqual(result["result"]["status"], "unavailable")

    def test_service_response_requires_verified_state(self):
        ok = TOOL._parse_service_response(
            "light", "turn_on", [], actual_state="on"
        )
        failed = TOOL._parse_service_response(
            "light", "turn_on", [], actual_state="off"
        )
        self.assertTrue(ok["success"])
        self.assertFalse(failed["success"])
        self.assertIn("error", failed)

    def test_tool_availability_requires_injected_token(self):
        with patch.dict(os.environ, {"HASS_TOKEN": ""}, clear=False):
            self.assertFalse(TOOL._check_ha_available())
        with patch.dict(os.environ, {"HASS_TOKEN": "present"}, clear=False):
            self.assertTrue(TOOL._check_ha_available())


if __name__ == "__main__":
    unittest.main()
