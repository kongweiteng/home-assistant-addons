"""Restricted Home Assistant tools for the Hermes HA add-on.

This module replaces Hermes Agent's upstream ``tools/homeassistant_tool.py``
at add-on startup.  Home Assistant does not provide service-level access
control for access tokens, so write safety must be enforced here.

The public tool names intentionally match upstream Hermes:

* ``ha_list_entities``
* ``ha_get_state``
* ``ha_list_services``
* ``ha_call_service``

Read access is limited to ordinary device/status domains.  Write access is
limited by both a domain policy and an optional exact entity allowlist, always
requires one explicit entity, rejects target expansion, and verifies the real
entity state after the service call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)

_HASS_URL: str = ""
_HASS_TOKEN: str = ""

_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Security-sensitive state domains are deliberately absent.  In particular,
# do not expose lock, alarm_control_panel, camera, person, device_tracker,
# zone, automation, script, scene, button, number, select, text, or update.
_READABLE_DOMAINS = frozenset(
    {
        "binary_sensor",
        "climate",
        "cover",
        "fan",
        "humidifier",
        "input_boolean",
        "light",
        "media_player",
        "sensor",
        "switch",
        "vacuum",
        "weather",
    }
)

# Only deterministic on/off services are supported.  Domains must also be
# enabled through HASS_ALLOWED_DOMAINS.  The add-on default is ``light``.
_CONTROL_SERVICE_POLICY = {
    "light": {
        "turn_on": frozenset({"brightness_pct"}),
        "turn_off": frozenset(),
    },
    "switch": {
        "turn_on": frozenset(),
        "turn_off": frozenset(),
    },
}

_FORBIDDEN_TARGET_KEYS = frozenset(
    {"area_id", "device_id", "entity_id", "floor_id", "label_id", "target"}
)
_SENSITIVE_ATTRIBUTE_NAMES = frozenset(
    {
        "access_token",
        "entity_picture",
        "latitude",
        "longitude",
        "media_content_id",
        "password",
        "secret",
        "token",
    }
)


def _get_config() -> tuple[str, str]:
    """Return the runtime HA URL and token without logging either secret."""
    return (
        (_HASS_URL or os.getenv("HASS_URL", "http://homeassistant.local:8123")).rstrip("/"),
        _HASS_TOKEN or os.getenv("HASS_TOKEN", ""),
    )


def _get_headers(token: str = "") -> Dict[str, str]:
    if not token:
        _, token = _get_config()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _csv_env(name: str, *, default: Optional[str] = None) -> frozenset[str]:
    raw = os.getenv(name)
    if raw is None:
        raw = default or ""
    return frozenset(value.strip() for value in raw.split(",") if value.strip())


def _allowed_control_domains() -> frozenset[str]:
    configured = _csv_env("HASS_ALLOWED_DOMAINS", default="light")
    return frozenset(domain for domain in configured if domain in _CONTROL_SERVICE_POLICY)


def _allowed_control_entities() -> frozenset[str]:
    return _csv_env("HASS_ALLOWED_ENTITIES")


def _entity_domain(entity_id: str) -> str:
    return entity_id.partition(".")[0]


def _safe_attributes(attributes: Any) -> Dict[str, Any]:
    if not isinstance(attributes, dict):
        return {}
    return {
        key: value
        for key, value in attributes.items()
        if key.lower() not in _SENSITIVE_ATTRIBUTE_NAMES
        and not any(part in key.lower() for part in ("password", "secret", "token"))
    }


def _filter_and_summarize(
    states: list,
    domain: Optional[str] = None,
    area: Optional[str] = None,
) -> Dict[str, Any]:
    states = [
        state
        for state in states
        if _entity_domain(state.get("entity_id", "")) in _READABLE_DOMAINS
    ]

    if domain:
        states = [
            state
            for state in states
            if state.get("entity_id", "").startswith(f"{domain}.")
        ]

    if area:
        area_lower = area.lower()
        states = [
            state
            for state in states
            if area_lower
            in (state.get("attributes", {}).get("friendly_name", "") or "").lower()
            or area_lower
            in (state.get("attributes", {}).get("area", "") or "").lower()
        ]

    entities = [
        {
            "entity_id": state.get("entity_id", ""),
            "state": state.get("state", "unknown"),
            "friendly_name": state.get("attributes", {}).get("friendly_name", ""),
        }
        for state in states
    ]
    return {"count": len(entities), "entities": entities}


async def _async_list_entities(
    domain: Optional[str] = None,
    area: Optional[str] = None,
) -> Dict[str, Any]:
    import aiohttp

    hass_url, hass_token = _get_config()
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{hass_url}/api/states",
            headers=_get_headers(hass_token),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            response.raise_for_status()
            states = await response.json()
    return _filter_and_summarize(states, domain, area)


async def _async_get_state(entity_id: str) -> Dict[str, Any]:
    import aiohttp

    hass_url, hass_token = _get_config()
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{hass_url}/api/states/{entity_id}",
            headers=_get_headers(hass_token),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            response.raise_for_status()
            state = await response.json()
    return {
        "entity_id": state["entity_id"],
        "state": state["state"],
        "attributes": _safe_attributes(state.get("attributes", {})),
        "last_changed": state.get("last_changed"),
        "last_updated": state.get("last_updated"),
    }


def _build_service_payload(
    entity_id: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = dict(data or {})
    if entity_id:
        payload["entity_id"] = entity_id
    return payload


def _parse_service_response(
    domain: str,
    service: str,
    result: Any,
    *,
    actual_state: Optional[str] = None,
) -> Dict[str, Any]:
    affected = []
    if isinstance(result, list):
        for state in result:
            if isinstance(state, dict):
                affected.append(
                    {
                        "entity_id": state.get("entity_id", ""),
                        "state": state.get("state", ""),
                    }
                )

    expected_state = "on" if service == "turn_on" else "off"
    verified = actual_state == expected_state
    response = {
        "success": verified,
        "service": f"{domain}.{service}",
        "affected_entities": affected,
        "verification": {
            "expected_state": expected_state,
            "actual_state": actual_state,
            "verified": verified,
        },
    }
    if not verified:
        response["error"] = "Home Assistant accepted the service call but the target state was not verified"
    return response


def _validate_service_data(
    domain: str,
    service: str,
    data: Any,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    if data is None or data == "":
        parsed: Dict[str, Any] = {}
    elif isinstance(data, str):
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as exc:
            return None, f"Invalid JSON string in 'data' parameter: {exc}"
    elif isinstance(data, dict):
        parsed = dict(data)
    else:
        return None, "The 'data' parameter must be a JSON object"

    if not isinstance(parsed, dict):
        return None, "The 'data' parameter must be a JSON object"

    forbidden = sorted(_FORBIDDEN_TARGET_KEYS.intersection(parsed))
    if forbidden:
        return None, f"Target expansion keys are forbidden in 'data': {', '.join(forbidden)}"

    allowed_keys = _CONTROL_SERVICE_POLICY[domain][service]
    unexpected = sorted(set(parsed) - allowed_keys)
    if unexpected:
        return None, f"Unsupported service data keys: {', '.join(unexpected)}"

    if "brightness_pct" in parsed:
        brightness = parsed["brightness_pct"]
        if isinstance(brightness, bool) or not isinstance(brightness, (int, float)):
            return None, "brightness_pct must be a number from 0 to 100"
        if not 0 <= brightness <= 100:
            return None, "brightness_pct must be a number from 0 to 100"

    return parsed, None


async def _async_call_service(
    domain: str,
    service: str,
    entity_id: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    import aiohttp

    hass_url, hass_token = _get_config()
    payload = _build_service_payload(entity_id, data)
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{hass_url}/api/services/{domain}/{service}",
            headers=_get_headers(hass_token),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            response.raise_for_status()
            result = await response.json()

        # The service response is not proof that the physical/logical entity
        # reached the requested state.  Re-read the authoritative HA state.
        await asyncio.sleep(0.35)
        async with session.get(
            f"{hass_url}/api/states/{entity_id}",
            headers=_get_headers(hass_token),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            response.raise_for_status()
            state = await response.json()

    return _parse_service_response(
        domain,
        service,
        result,
        actual_state=state.get("state"),
    )


async def _async_list_services(domain: Optional[str] = None) -> Dict[str, Any]:
    import aiohttp

    allowed_domains = _allowed_control_domains()
    hass_url, hass_token = _get_config()
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{hass_url}/api/services",
            headers=_get_headers(hass_token),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            response.raise_for_status()
            services = await response.json()

    domains = []
    for service_domain in services:
        service_domain_name = service_domain.get("domain", "")
        if service_domain_name not in allowed_domains:
            continue
        if domain and service_domain_name != domain:
            continue
        allowed_services = _CONTROL_SERVICE_POLICY[service_domain_name]
        available = service_domain.get("services", {})
        domain_services = {}
        for service_name, allowed_fields in allowed_services.items():
            if service_name not in available:
                continue
            service_info = available[service_name]
            fields = service_info.get("fields", {})
            domain_services[service_name] = {
                "description": service_info.get("description", ""),
                "fields": {
                    key: value.get("description", "")
                    for key, value in fields.items()
                    if key in allowed_fields and isinstance(value, dict)
                },
            }
        domains.append({"domain": service_domain_name, "services": domain_services})
    return {"count": len(domains), "domains": domains}


def _run_async(coro: Any) -> Any:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=30)
    return asyncio.run(coro)


def _handle_list_entities(args: dict, **kw: Any) -> str:
    domain = args.get("domain")
    area = args.get("area")
    if domain and domain not in _READABLE_DOMAINS:
        return tool_error(f"Entity domain '{domain}' is not available to Hermes")
    try:
        return json.dumps(
            {"result": _run_async(_async_list_entities(domain=domain, area=area))}
        )
    except Exception as exc:
        logger.error("ha_list_entities error: %s", exc)
        return tool_error(f"Failed to list entities: {exc}")


def _handle_get_state(args: dict, **kw: Any) -> str:
    entity_id = args.get("entity_id", "")
    if not entity_id:
        return tool_error("Missing required parameter: entity_id")
    if not _ENTITY_ID_RE.match(entity_id):
        return tool_error(f"Invalid entity_id format: {entity_id}")
    if _entity_domain(entity_id) not in _READABLE_DOMAINS:
        return tool_error(f"Entity domain '{_entity_domain(entity_id)}' is not available to Hermes")
    try:
        return json.dumps({"result": _run_async(_async_get_state(entity_id))})
    except Exception as exc:
        logger.error("ha_get_state error for %s: %s", entity_id, exc)
        return tool_error(f"Failed to get state for {entity_id}: {exc}")


def _handle_list_services(args: dict, **kw: Any) -> str:
    domain = args.get("domain")
    if domain and domain not in _allowed_control_domains():
        return tool_error(f"Service domain '{domain}' is not enabled for control")
    try:
        return json.dumps(
            {"result": _run_async(_async_list_services(domain=domain))}
        )
    except Exception as exc:
        logger.error("ha_list_services error: %s", exc)
        return tool_error(f"Failed to list services: {exc}")


def _handle_call_service(args: dict, **kw: Any) -> str:
    domain = args.get("domain", "")
    service = args.get("service", "")
    entity_id = args.get("entity_id", "")
    if not domain or not service or not entity_id:
        return tool_error("Missing required parameters: domain, service, and entity_id")
    if not _SERVICE_NAME_RE.match(domain) or not _SERVICE_NAME_RE.match(service):
        return tool_error("Invalid domain or service format")
    if not _ENTITY_ID_RE.match(entity_id):
        return tool_error(f"Invalid entity_id format: {entity_id}")
    if _entity_domain(entity_id) != domain:
        return tool_error("The entity_id domain must match the service domain")

    allowed_domains = _allowed_control_domains()
    if domain not in allowed_domains:
        return tool_error(f"Service domain '{domain}' is not enabled for control")
    if service not in _CONTROL_SERVICE_POLICY[domain]:
        return tool_error(f"Service '{domain}.{service}' is not enabled for control")

    allowed_entities = _allowed_control_entities()
    if domain == "switch" and not allowed_entities:
        return tool_error("The switch domain requires an exact entity allowlist")
    if allowed_entities and entity_id not in allowed_entities:
        return tool_error(f"Entity '{entity_id}' is not in the control allowlist")

    data, data_error = _validate_service_data(domain, service, args.get("data"))
    if data_error:
        return tool_error(data_error)

    logger.info("HA control request: %s.%s -> %s", domain, service, entity_id)
    try:
        result = _run_async(_async_call_service(domain, service, entity_id, data))
        if result.get("success"):
            logger.info("HA control verified: %s.%s -> %s", domain, service, entity_id)
        else:
            logger.warning("HA control not verified: %s.%s -> %s", domain, service, entity_id)
        return json.dumps({"result": result})
    except Exception as exc:
        logger.error("HA control failed: %s.%s -> %s: %s", domain, service, entity_id, exc)
        return tool_error(f"Failed to call {domain}.{service} for {entity_id}: {exc}")


def _check_ha_available() -> bool:
    return bool(os.getenv("HASS_TOKEN"))


HA_LIST_ENTITIES_SCHEMA = {
    "name": "ha_list_entities",
    "description": (
        "List ordinary Home Assistant device and sensor entities. Security-sensitive "
        "domains such as locks, alarms, cameras, people, and device trackers are excluded."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Optional safe entity domain."},
            "area": {"type": "string", "description": "Optional room/name filter."},
        },
        "required": [],
    },
}

HA_GET_STATE_SCHEMA = {
    "name": "ha_get_state",
    "description": "Get the current state of one ordinary Home Assistant entity.",
    "parameters": {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": "Exact Home Assistant entity ID."}
        },
        "required": ["entity_id"],
    },
}

HA_LIST_SERVICES_SCHEMA = {
    "name": "ha_list_services",
    "description": "List only the Home Assistant control services enabled by the add-on allowlist.",
    "parameters": {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Optional enabled control domain."}
        },
        "required": [],
    },
}

HA_CALL_SERVICE_SCHEMA = {
    "name": "ha_call_service",
    "description": (
        "Control exactly one allowlisted Home Assistant entity with a restricted "
        "on/off service. The tool verifies the resulting HA state before reporting success."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Allowlisted service domain."},
            "service": {"type": "string", "description": "Restricted service name."},
            "entity_id": {"type": "string", "description": "One exact target entity ID."},
            "data": {
                "type": "string",
                "description": "Optional restricted JSON data; target expansion is forbidden.",
            },
        },
        "required": ["domain", "service", "entity_id"],
    },
}


from tools.registry import registry, tool_error


registry.register(
    name="ha_list_entities",
    toolset="homeassistant",
    schema=HA_LIST_ENTITIES_SCHEMA,
    handler=_handle_list_entities,
    check_fn=_check_ha_available,
    emoji="🏠",
)
registry.register(
    name="ha_get_state",
    toolset="homeassistant",
    schema=HA_GET_STATE_SCHEMA,
    handler=_handle_get_state,
    check_fn=_check_ha_available,
    emoji="🏠",
)
registry.register(
    name="ha_list_services",
    toolset="homeassistant",
    schema=HA_LIST_SERVICES_SCHEMA,
    handler=_handle_list_services,
    check_fn=_check_ha_available,
    emoji="🏠",
)
registry.register(
    name="ha_call_service",
    toolset="homeassistant",
    schema=HA_CALL_SERVICE_SCHEMA,
    handler=_handle_call_service,
    check_fn=_check_ha_available,
    emoji="🏠",
)
