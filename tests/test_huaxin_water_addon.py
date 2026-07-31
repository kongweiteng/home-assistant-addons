from email.message import Message
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen


ROOT = pathlib.Path(__file__).resolve().parents[1]
ADDON = ROOT / "huaxin_water"
sys.path.insert(0, str(ADDON))

from huaxin_water.api import DASHBOARD_HTML, create_server
from huaxin_water.cache import CacheStore
from huaxin_water.client import ENDPOINT_PATHS, HuaxinClient, UpstreamError
from huaxin_water.config import AppConfig
from huaxin_water.normalize import ContractError, normalize_response
from huaxin_water.runtime import WaterService


ACCOUNT_A = "000000000001"
ACCOUNT_B = "000000000002"


def write_options(path: pathlib.Path, **overrides) -> AppConfig:
    payload = {
        "accounts": [
            {"id": "home", "customer_no": ACCOUNT_A},
            {"id": "studio", "customer_no": ACCOUNT_B},
        ],
        "base_url": "http://www.huaxinshuiwu.com/api",
        "allow_insecure_http": True,
        "poll_interval_minutes": 360,
        "request_timeout_seconds": 15,
        "stale_after_minutes": 1440,
        "manual_refresh_cooldown_seconds": 60,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return AppConfig.load(path)


def envelope(result):
    return {"success": True, "resultCode": "0000000", "msg": "ok", "result": result}


def response_for(endpoint: str, marker: str = "A") -> dict:
    if endpoint == "customer_info":
        return envelope(
            {
                "customer": {
                    "customerNo": ACCOUNT_A,
                    "customerName": f"Synthetic {marker}",
                    "customerAddr": f"Synthetic address {marker}",
                    "customerCell": "not-retained",
                    "customerMeterNum": "2",
                },
                "waterInfo": {
                    "remaining": "12.50",
                    "arrears": 0,
                    "meterNumber": "M-1",
                    "customerPopulation": "3",
                    "totalUse": "45.6",
                    "step": 1,
                    "stepName": "Tier 1",
                    "useKindType": "Residential",
                },
                "meterInfos": [
                    {
                        "registNo": "R-1",
                        "meterLocation": "Kitchen",
                        "latestReadingDate": "2026-07-01",
                        "latestReading": "101.25",
                    },
                    {
                        "registNo": "R-2",
                        "meterLocation": "Garden",
                        "latestReadingDate": "2026-07-02",
                        "latestReading": 88,
                    },
                ],
                "waterTrend": {"hasWaterTrend": "true", "x": ["Jun", "Jul"], "y": ["3.2", 4]},
            }
        )
    if endpoint == "water_records":
        return envelope(
            [
                {
                    "registNo": "R-1",
                    "meterLocation": "Kitchen",
                    "calculateMonth": "2026-07",
                    "senseTime": "2026-07-31",
                    "accountAmount": "4.5",
                    "receivableCharge": 12.34,
                }
            ]
        )
    if endpoint == "payment_records":
        return envelope(
            {
                "records": [
                    {
                        "paymentMode": "Synthetic",
                        "chargeTime": "2026-07-15",
                        "paymentMoney": "25.00",
                    }
                ]
            }
        )
    if endpoint == "steps":
        return envelope(
            [
                {
                    "name": "Tier 1",
                    "stepStartValue": "0",
                    "stepEndValue": 120,
                    "used": "45.6",
                    "capacity": "120",
                },
                {
                    "name": "Tier 2",
                    "stepStartValue": 120,
                    "stepEndValue": "180",
                    "used": 0,
                    "capacity": 60,
                },
            ]
        )
    if endpoint == "payment_summary":
        return envelope(
            {
                "customerNo": ACCOUNT_A,
                "customerName": f"Synthetic {marker}",
                "address": f"Synthetic address {marker}",
                "remaining": 12.5,
                "arrears": "0.00",
                "canRecharge": True,
                "minReCharge": "1",
                "maxReCharge": 1000,
            }
        )
    raise AssertionError(endpoint)


class FakeClient:
    def __init__(self) -> None:
        self.failures: set[tuple[str, str]] = set()

    def fetch(self, customer_no: str, endpoint: str) -> dict:
        if (customer_no, endpoint) in self.failures:
            raise UpstreamError("timeout", endpoint)
        marker = "A" if customer_no == ACCOUNT_A else "B"
        return response_for(endpoint, marker)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode()
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int) -> bytes:
        return self.payload[:size]


class HuaxinWaterAddonTests(unittest.TestCase):
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
        self.assertIn("slug: huaxin_water", config)
        self.assertIn("ingress: true", config)
        self.assertIn("backup: cold", config)
        self.assertNotIn("homeassistant_api", config)
        self.assertNotIn("hassio_api", config)
        self.assertNotIn("host_network", config)
        self.assertNotIn("privileged", config)
        self.assertNotIn("ports:", config)
        self.assertNotIn("services:", config)

    def test_client_has_exact_get_allowlist(self) -> None:
        self.assertEqual(
            ENDPOINT_PATHS,
            {
                "customer_info": "/bh/customer/info",
                "water_records": "/bh/customer/queryWaterRecords",
                "payment_records": "/bh/customer/queryPaymentRecords",
                "steps": "/bh/customer/queryStep",
                "payment_summary": "/bh/pay/getUserPaymentInfo",
            },
        )
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse(response_for("water_records"))

        client = HuaxinClient(
            "https://water.example.test/api", 9, opener=opener, sleep=lambda _: None
        )
        client.fetch(ACCOUNT_A, "water_records")
        self.assertEqual(requests[0][0].method, "GET")
        self.assertIn("/api/bh/customer/queryWaterRecords?", requests[0][0].full_url)
        self.assertEqual(requests[0][1], 9)
        with self.assertRaises(ValueError):
            client.fetch(ACCOUNT_A, "not_allowlisted")

    def test_default_client_transport_does_not_follow_redirects(self) -> None:
        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://example.test/outside")
                self.end_headers()

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = HuaxinClient(
                f"http://127.0.0.1:{server.server_port}/api", 3, sleep=lambda _: None
            )
            with self.assertRaises(UpstreamError) as captured:
                client.fetch(ACCOUNT_A, "customer_info")
            self.assertEqual(captured.exception.kind, "http_error")
            self.assertEqual(captured.exception.http_status, 302)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_client_classifies_server_errors_without_response_body(self) -> None:
        attempts = []

        def opener(request, timeout):
            attempts.append(request.method)
            raise HTTPError(request.full_url, 503, "unavailable", {}, None)

        client = HuaxinClient(
            "https://water.example.test/api", 9, opener=opener, sleep=lambda _: None
        )
        with self.assertRaises(UpstreamError) as captured:
            client.fetch(ACCOUNT_A, "customer_info")
        self.assertEqual(captured.exception.kind, "server_error")
        self.assertEqual(captured.exception.http_status, 503)
        self.assertEqual(attempts, ["GET", "GET"])

    def test_client_does_not_retry_bad_requests(self) -> None:
        attempts = []

        def opener(request, timeout):
            attempts.append(request.method)
            raise HTTPError(request.full_url, 400, "bad request", {}, None)

        client = HuaxinClient(
            "https://water.example.test/api", 9, opener=opener, sleep=lambda _: None
        )
        with self.assertRaises(UpstreamError) as captured:
            client.fetch(ACCOUNT_A, "customer_info")
        self.assertEqual(captured.exception.kind, "http_error")
        self.assertEqual(attempts, ["GET"])

    def test_config_fails_closed_for_plain_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "options.json"
            with self.assertRaisesRegex(ValueError, "plain HTTP is blocked"):
                write_options(path, allow_insecure_http=False)
            config = write_options(
                path,
                base_url="https://water.example.test/api",
                allow_insecure_http=False,
            )
        self.assertEqual(len(config.accounts), 2)
        self.assertEqual(config.accounts[0].masked_customer_no, "****0001")

    def test_config_rejects_duplicate_alias_or_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "options.json"
            with self.assertRaises(ValueError):
                write_options(
                    path,
                    accounts=[
                        {"id": "home", "customer_no": ACCOUNT_A},
                        {"id": "home", "customer_no": ACCOUNT_B},
                    ],
                )
            with self.assertRaises(ValueError):
                write_options(
                    path,
                    accounts=[
                        {"id": "home", "customer_no": ACCOUNT_A},
                        {"id": "studio", "customer_no": ACCOUNT_A},
                    ],
                )

    def test_normalization_keeps_address_and_multiple_meters(self) -> None:
        result = normalize_response("customer_info", response_for("customer_info"))
        self.assertEqual(result.data["customer"]["address"], "Synthetic address A")
        self.assertEqual(result.data["water"]["remaining"], 12.5)
        self.assertEqual(result.data["water"]["arrears"], 0.0)
        self.assertEqual(len(result.data["meters"]), 2)
        self.assertEqual(result.data["meters"][0]["latest_reading"], 101.25)
        serialized = json.dumps(result.data)
        self.assertNotIn(ACCOUNT_A, serialized)
        self.assertNotIn("not-retained", serialized)

    def test_empty_records_are_not_errors_and_bad_envelope_is(self) -> None:
        result = normalize_response("water_records", envelope([]))
        self.assertTrue(result.empty)
        self.assertEqual(result.data, [])
        with self.assertRaises(ContractError):
            normalize_response(
                "water_records",
                {"success": False, "resultCode": "E", "result": []},
            )

    def test_invalid_numeric_field_becomes_null_with_contract_issue(self) -> None:
        payload = response_for("payment_summary")
        payload["result"]["remaining"] = "not-a-number"
        result = normalize_response("payment_summary", payload)
        self.assertIsNone(result.data["remaining"])
        self.assertIn("payment_summary.remaining:invalid_number", result.issues)

    def test_history_is_bounded_with_visible_contract_issue(self) -> None:
        item = response_for("water_records")["result"][0]
        result = normalize_response("water_records", envelope([item] * 501))
        self.assertEqual(len(result.data), 500)
        self.assertIn("water_records:truncated_to_500", result.issues)

    def test_cache_omits_customer_numbers_and_partial_failure_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            config = write_options(directory / "options.json")
            cache = CacheStore(directory / "state.json", directory / "cache.key")
            client = FakeClient()
            service = WaterService(config, client, cache)
            service.run_once()
            client.failures.add((ACCOUNT_A, "water_records"))
            service.run_once()
            home = service.account_snapshot("home")
            studio = service.account_snapshot("studio")
            state_text = (directory / "state.json").read_text(encoding="utf-8")
            reloaded = WaterService(
                config,
                FakeClient(),
                CacheStore(directory / "state.json", directory / "cache.key"),
            ).account_snapshot("home")
        self.assertEqual(home["status"], "degraded")
        self.assertEqual(home["endpoints"]["water_records"]["status"], "stale")
        self.assertEqual(studio["status"], "good")
        self.assertEqual(reloaded["summary"]["address"], "Synthetic address A")
        self.assertIn(reloaded["endpoints"]["water_records"]["status"], {"cached", "stale"})
        self.assertNotIn(ACCOUNT_A, state_text)
        self.assertNotIn(ACCOUNT_B, state_text)

    def test_ingress_api_is_bounded_and_health_has_no_personal_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            config = write_options(directory / "options.json")
            service = WaterService(
                config,
                FakeClient(),
                CacheStore(directory / "state.json", directory / "cache.key"),
            )
            service.run_once()
            server = create_server("127.0.0.1", 0, service)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urlopen(base + "/health") as response:
                    health_text = response.read().decode()
                with urlopen(base + "/api/v1/accounts/home") as response:
                    account = json.loads(response.read())
            finally:
                server.shutdown()
                server.server_close()
        self.assertNotIn("Synthetic", health_text)
        self.assertNotIn(ACCOUNT_A, health_text)
        self.assertEqual(account["summary"]["address"], "Synthetic address A")
        self.assertEqual(len(account["endpoints"]), 5)

    def test_dashboard_uses_relative_api_and_safe_text_nodes(self) -> None:
        self.assertIn("用水地址", DASHBOARD_HTML)
        self.assertIn("api/v1/accounts", DASHBOARD_HTML)
        self.assertNotIn("fetch('/api/", DASHBOARD_HTML)
        self.assertIn("textContent", DASHBOARD_HTML)
        self.assertIn("X-Huaxin-Action", DASHBOARD_HTML)
        self.assertNotIn("document.write", DASHBOARD_HTML)

    def test_runtime_script_does_not_echo_options(self) -> None:
        script = (ADDON / "run.sh").read_text(encoding="utf-8")
        self.assertNotIn("set -x", script)
        self.assertNotIn("cat \"$OPTIONS_FILE\"", script)
        self.assertNotIn("customer_no", script)


if __name__ == "__main__":
    unittest.main()
