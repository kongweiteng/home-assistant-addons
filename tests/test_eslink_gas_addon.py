from datetime import datetime, timedelta, timezone
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen


ROOT = pathlib.Path(__file__).resolve().parents[1]
ADDON = ROOT / "eslink_gas"
sys.path.insert(0, str(ADDON))

from eslink_gas.api import create_server
from eslink_gas.cache import StateCache
from eslink_gas.client import (
    AuthRequiredError,
    CHROMEDRIVER_BINARY,
    ContractError,
    DESKTOP_WECHAT_USER_AGENT,
    EslinkBrowserClient,
    FetchResult,
    USER_INFO_PATH,
    blocked_third_party_urls,
    build_iot_url,
    chrome_arguments,
    parse_fetch_response,
)
from eslink_gas.config import AccountConfig, AppConfig
from eslink_gas.mqtt import discovery_messages, snapshot_messages
from eslink_gas.normalize import normalize_account
from eslink_gas.runtime import GasMonitor, RuntimeState


UTC = timezone.utc


def options_payload(**overrides) -> dict:
    test_token = "test" * 8
    payload = {
        "accounts": [
            {"id": "home", "user_no": "100000000001", "user_name": "示例用户"}
        ],
        "portal_url": (
            "http://cloudselfhelp-mobile.eslink.cc/#/index?"
            f"token={test_token}&opid=example"
        ),
        "allow_insecure_http": True,
        "poll_interval_minutes": 30,
        "page_timeout_seconds": 25,
        "stale_after_minutes": 180,
        "include_personal_details": False,
    }
    payload.update(overrides)
    return payload


def load_config(path: pathlib.Path, **overrides) -> AppConfig:
    path.write_text(json.dumps(options_payload(**overrides)), encoding="utf-8")
    return AppConfig.load(path)


def upstream_payload(user_no: str = "100000000001") -> dict:
    return {
        "success": True,
        "userNo": user_no,
        "custName": "示例用户",
        "addrDesc": "示例市示例区示例路一号",
        "custMobile": "13800000000",
        "acctOrg": "示例燃气",
        "addrStatus": "正常",
        "custClass": "居民",
        "servicePointRuler": "后付费",
        "meterList": [
            {
                "acctBalance": "123.4500",
                "acctBalanceDesc": "表上余额",
                "meterNo": "900000000001",
                "meterStatus": "在用",
                "meterStatusId": "1",
                "meterType": "NBIOT",
                "meterClass": "物联网表",
                "priceName": "居民阶梯",
                "purchCommandStateDes": "无待执行指令",
            }
        ],
    }


class FakeClient:
    def __init__(self, results):
        self.results = tuple(results)

    def fetch_accounts(self):
        return self.results


class FakePublisher:
    def __init__(self):
        self.snapshots = []

    def publish_snapshot(self, account_id, snapshot):
        self.snapshots.append((account_id, dict(snapshot)))


class FakePortalDriver:
    def __init__(self, cookie_values, *, ready=True):
        self.cookie_values = list(cookie_values)
        self.ready = ready
        self.title = "东新燃气微信服务大厅" if ready else ""
        self.visited = []

    def get(self, url):
        self.visited.append(url)

    def get_cookies(self):
        value = self.cookie_values.pop(0) if len(self.cookie_values) > 1 else self.cookie_values[0]
        return [{"name": "SESSION", "value": value}] if value else []

    def execute_script(self, script):
        if "document.body" in script:
            return "物联表缴费 物联表使用" if self.ready else ""
        return None


class FakeIotDriver:
    title = "物联网表充值"

    def __init__(self, payload):
        self.payload = payload
        self.visited = []
        self.manual_query_called = False
        self.performance_entries = [
            {
                "message": json.dumps(
                    {
                        "message": {
                            "method": "Network.responseReceived",
                            "params": {
                                "requestId": "request-1",
                                "response": {
                                    "url": (
                                        "http://utilityserve-mobile.eslink.cc"
                                        "/api/usmart/v1.0/iot/userInfoQuery"
                                    ),
                                    "status": 200,
                                },
                            },
                        }
                    }
                )
            }
        ]

    def get(self, url):
        self.visited.append(url)

    def get_log(self, kind):
        self.log_kind = kind
        entries, self.performance_entries = self.performance_entries, []
        return entries

    def execute_cdp_cmd(self, method, params):
        if method != "Network.getResponseBody" or params != {"requestId": "request-1"}:
            raise AssertionError("unexpected CDP request")
        return {
            "body": json.dumps(self.payload),
            "base64Encoded": False,
        }

    def execute_async_script(self, script, *args):
        self.manual_query_called = True
        raise AssertionError("the observed page response should be preferred")


class EslinkGasAddonTests(unittest.TestCase):
    def test_required_files_and_minimum_permissions(self) -> None:
        for relative in (
            "config.yaml",
            "build.yaml",
            "Dockerfile",
            "run.sh",
            "README.md",
            "DOCS.md",
            "CHANGELOG.md",
        ):
            self.assertTrue((ADDON / relative).is_file(), relative)
        config = (ADDON / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("slug: eslink_gas", config)
        self.assertIn("homeassistant_api: true", config)
        self.assertIn("ingress: true", config)
        self.assertIn("  - mqtt:want", config)
        self.assertNotIn("host_network", config)
        self.assertNotIn("privileged", config)
        self.assertNotIn("ports:", config)
        self.assertNotIn("hassio_api", config)
        self.assertIn('version: "0.1.4"', config)

        build = (ADDON / "build.yaml").read_text(encoding="utf-8")
        dockerfile = (ADDON / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("DEBIAN_MIRROR", build)
        self.assertIn("mirrors.tuna.tsinghua.edu.cn/debian", build)
        self.assertIn("ARG DEBIAN_MIRROR=https://deb.debian.org/debian", dockerfile)
        self.assertIn("deb.debian.org/debian", dockerfile)

    def test_configuration_is_fixed_host_and_fails_closed_on_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "options.json"
            config = load_config(path)
            self.assertEqual(config.accounts[0].id, "home")
            path.write_text(
                json.dumps(options_payload(allow_insecure_http=False)),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                AppConfig.load(path)
            invalid_host_url = options_payload()["portal_url"].replace(
                "cloudselfhelp-mobile.eslink.cc", "example.com"
            )
            path.write_text(
                json.dumps(
                    options_payload(portal_url=invalid_host_url)
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                AppConfig.load(path)

    def test_duplicate_accounts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "options.json"
            accounts = [
                {"id": "home", "user_no": "100000000001", "user_name": "A"},
                {"id": "home", "user_no": "100000000002", "user_name": "B"},
            ]
            path.write_text(
                json.dumps(options_payload(accounts=accounts)), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                AppConfig.load(path)

    def test_client_only_builds_the_observed_read_only_path(self) -> None:
        account = AccountConfig("home", "100000000001", "示例 用户")
        url = build_iot_url(account)
        self.assertTrue(url.startswith("http://utilityserve-mobile.eslink.cc/"))
        self.assertIn("userNo=100000000001", url)
        self.assertIn("userName=%E7%A4%BA%E4%BE%8B%20%E7%94%A8%E6%88%B7", url)
        self.assertEqual(USER_INFO_PATH, "/api/usmart/v1.0/iot/userInfoQuery")
        source = (ADDON / "eslink_gas" / "client.py").read_text(encoding="utf-8")
        self.assertIn("goog:loggingPrefs", source)
        self.assertIn("Network.getResponseBody", source)
        self.assertNotIn("wechatPay", source)
        self.assertNotIn("/payment", source.lower())
        self.assertNotIn("/bind", source.lower())

    def test_browser_profile_and_third_party_blocking_are_explicit(self) -> None:
        args = chrome_arguments(pathlib.Path("/data/chromium-profile"))
        self.assertIn("--headless=new", args)
        self.assertIn("--disable-dev-shm-usage", args)
        self.assertTrue(any(arg.startswith("--user-data-dir=") for arg in args))
        self.assertEqual(CHROMEDRIVER_BINARY, "/usr/bin/chromedriver")
        self.assertIn("MicroMessenger", DESKTOP_WECHAT_USER_AGENT)
        self.assertIn(
            f"--user-agent={DESKTOP_WECHAT_USER_AGENT}",
            args,
        )
        blocked = "\n".join(blocked_third_party_urls())
        self.assertIn("amap.com", blocked)
        self.assertIn("cnzz.com", blocked)
        self.assertNotIn("eslink.cc", blocked)

    def test_authentication_waits_for_a_fresh_session_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(pathlib.Path(tmp) / "options.json")
            ticks = iter((0.0, 0.0, 0.25, 0.25))
            client = EslinkBrowserClient(
                config,
                pathlib.Path(tmp) / "profile",
                "/usr/bin/chromium",
                clock=lambda: next(ticks),
            )
            driver = FakePortalDriver(("expired", "expired", "renewed"), ready=False)
            client._authenticate(driver)
            self.assertEqual(driver.visited, [config.portal_url])

    def test_authentication_does_not_accept_an_unrendered_stale_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(
                pathlib.Path(tmp) / "options.json", page_timeout_seconds=10
            )
            ticks = iter((0.0, 0.0, 11.0))
            client = EslinkBrowserClient(
                config,
                pathlib.Path(tmp) / "profile",
                "/usr/bin/chromium",
                clock=lambda: next(ticks),
            )
            driver = FakePortalDriver(("expired",), ready=False)
            with self.assertRaises(AuthRequiredError):
                client._authenticate(driver)

    def test_authentication_accepts_a_settled_desktop_wechat_portal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(
                pathlib.Path(tmp) / "options.json", page_timeout_seconds=10
            )
            ticks = iter((0.0, 0.0, 0.0, 2.25, 2.25))
            client = EslinkBrowserClient(
                config,
                pathlib.Path(tmp) / "profile",
                "/usr/bin/chromium",
                clock=lambda: next(ticks),
            )
            driver = FakePortalDriver(("existing",), ready=True)
            client._authenticate(driver)
            self.assertEqual(driver.visited, [config.portal_url])

    def test_fetch_response_preserves_auth_and_contract_failures(self) -> None:
        payload = upstream_payload()
        self.assertEqual(
            parse_fetch_response(
                {"status": 200, "text": json.dumps(payload), "truncated": False}
            ),
            payload,
        )
        with self.assertRaises(AuthRequiredError):
            parse_fetch_response(
                {
                    "status": 200,
                    "text": json.dumps({"success": False, "echoCode": "910000"}),
                    "truncated": False,
                }
            )
        with self.assertRaises(ContractError):
            parse_fetch_response({"status": 200, "text": "{}", "truncated": True})

    def test_meter_page_observed_response_precedes_manual_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(pathlib.Path(tmp) / "options.json")
            client = EslinkBrowserClient(
                config,
                pathlib.Path(tmp) / "profile",
                "/usr/bin/chromium",
            )
            driver = FakeIotDriver(upstream_payload())
            result = client._fetch_account(driver, config.accounts[0])
            self.assertTrue(result["success"])
            self.assertEqual(driver.log_kind, "performance")
            self.assertFalse(driver.manual_query_called)

    def test_normalization_masks_personal_data_and_keeps_decimal_text(self) -> None:
        account = AccountConfig("home", "100000000001", "示例用户")
        snapshot = normalize_account(
            upstream_payload(),
            account,
            include_personal_details=False,
            fetched_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        self.assertEqual(snapshot["balance"], "123.4500")
        self.assertEqual(snapshot["meter_count"], 1)
        self.assertNotEqual(snapshot["customer_name"], "示例用户")
        self.assertNotEqual(snapshot["customer_address"], "示例市示例区示例路一号")
        self.assertTrue(snapshot["user_no_masked"].endswith("0001"))

    def test_normalization_can_show_personal_details_only_in_private_state(self) -> None:
        account = AccountConfig("home", "100000000001", "示例用户")
        snapshot = normalize_account(
            upstream_payload(), account, include_personal_details=True
        )
        self.assertEqual(snapshot["customer_name"], "示例用户")
        self.assertEqual(snapshot["customer_mobile"], "13800000000")
        combined = json.dumps(discovery_messages(("home",)), ensure_ascii=False)
        combined += json.dumps(snapshot_messages("home", snapshot), ensure_ascii=False)
        self.assertNotIn("示例用户", combined)
        self.assertNotIn("13800000000", combined)
        self.assertNotIn("100000000001", combined)

    def test_cache_is_atomic_private_and_contains_no_runtime_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "state.json"
            cache = StateCache(path)
            snapshot = {
                "home": {
                    "account_id": "home",
                    "user_no_masked": "********0001",
                    "balance": "1.2",
                }
            }
            cache.save(snapshot)
            self.assertEqual(cache.load(("home",)), snapshot)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            text_value = path.read_text(encoding="utf-8")
            self.assertNotIn("portal_url", text_value)
            self.assertNotIn("token=", text_value)
            self.assertNotIn("100000000001", text_value)

    def test_partial_failure_preserves_recent_cache_as_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            options = pathlib.Path(tmp) / "options.json"
            config = load_config(options)
            cache = StateCache(pathlib.Path(tmp) / "state.json")
            publisher = FakePublisher()
            now = datetime(2026, 8, 1, 1, 0, tzinfo=UTC)
            monitor = GasMonitor(
                config,
                FakeClient((FetchResult("home", payload=upstream_payload()),)),
                cache,
                publisher,
                RuntimeState(),
            )
            first = monitor.run_once(now)["home"]
            self.assertEqual(first["status"], "ok")
            monitor.client = FakeClient(
                (FetchResult("home", error=RuntimeError("temporary")),)
            )
            second = monitor.run_once(now + timedelta(minutes=5))["home"]
            self.assertEqual(second["status"], "degraded")
            self.assertTrue(second["available"])
            self.assertEqual(second["balance"], "123.4500")

    def test_auth_failure_is_explicit_and_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(pathlib.Path(tmp) / "options.json")
            monitor = GasMonitor(
                config,
                FakeClient((FetchResult("home", error=AuthRequiredError("expired")),)),
                StateCache(pathlib.Path(tmp) / "state.json"),
                FakePublisher(),
                RuntimeState(),
            )
            snapshot = monitor.run_once(datetime(2026, 8, 1, tzinfo=UTC))["home"]
            self.assertEqual(snapshot["status"], "auth_required")
            self.assertIsNone(snapshot["balance"])
            self.assertFalse(snapshot["available"])

    def test_ingress_is_read_only_and_health_contains_no_personal_data(self) -> None:
        runtime = RuntimeState(
            {
                "home": {
                    "account_id": "home",
                    "available": True,
                    "status": "ok",
                    "customer_name": "示例用户",
                }
            }
        )
        server = create_server("127.0.0.1", 0, runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/health") as response:
                health = response.read().decode()
            self.assertNotIn("示例用户", health)
            request = Request(base + "/api/v1/status", data=b"{}", method="POST")
            with self.assertRaises(Exception):
                urlopen(request)
            html = (ADDON / "eslink_gas" / "api.py").read_text(encoding="utf-8")
            self.assertIn("不会执行充值", html)
            self.assertNotIn("我要充值", html)
        finally:
            server.shutdown()
            server.server_close()

    def test_runtime_script_does_not_echo_secrets(self) -> None:
        script = (ADDON / "run.sh").read_text(encoding="utf-8")
        self.assertNotIn("set -x", script)
        self.assertNotIn("echo \"$SUPERVISOR_TOKEN", script)
        self.assertNotIn("echo \"$ESLINK_MQTT_PASSWORD", script)
        dockerfile = (ADDON / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("selenium==4.35.0", dockerfile)
        self.assertIn("paho-mqtt==2.1.0", dockerfile)


if __name__ == "__main__":
    unittest.main()
